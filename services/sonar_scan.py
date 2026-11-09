"""
SonarQube 扫描调度与飞书结果卡片服务

职责：
1. 独立于发版流程触发 GitLab CI 中的 SonarQube 扫描任务 (ACTION=sonar)
2. 后台轮询流水线进度并实时更新追踪卡片（不占用发版锁 repo_lock，不写入发版历史）
3. 扫描完成后优先调用 SonarQube Web API，若未配置令牌或 401/403 则降级解析 sonar-report.md 与 Job 日志
4. 将质量门禁、代码指标、Top 问题及安全热点渲染至飞书结果卡片并支持审计群同步与责任人闭环确认
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.card_builder import build_sonar_card
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.metrics import AI_DIAGNOSIS_TRIGGERED, SONAR_SCAN_COMPLETED, SONAR_SCAN_DURATION
from core.redis_client import get_redis
from core.sonar_client import (
    SonarClient,
    parse_sonar_context_from_trace,
    parse_sonar_report_from_text,
    resolve_sonar_config,
)
from core.state import unlock_sonar

logger = logging.getLogger("feishu_gitlab_card_http")


def build_sonar_lock_scope(state: Dict[str, Any]) -> str:
    """生成独立的 Sonar 扫描并发锁键（按仓库 + 分支 + 模块隔离，不影响发版锁）"""
    repo_id = state.get("repo_id", 0)
    branch = state.get("branch", "main")
    variables = state.get("variables") or {}
    target = (
        variables.get("TARGET_MODULE")
        or state.get("module")
        or variables.get("TARGET_PROFILE")
        or "default"
    )
    return f"{repo_id}:{branch}:{target}"


def _find_repo_conf(cfg: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    target_id = state.get("repo_id")
    target_name = state.get("repo")
    for proj in cfg.get("projects") or []:
        for r in proj.get("repos") or []:
            if target_id and r.get("id") == target_id:
                return r
            if target_name and r.get("name") == target_name:
                return r
    return None


def _pick_sonar_job(jobs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not jobs:
        return None
    sonar_jobs = [
        j for j in jobs
        if str(j.get("stage") or "").lower() == "sonar"
        or "sonar" in str(j.get("name") or "").lower()
    ]
    candidates = sonar_jobs or jobs
    # 优先选择已经开始/结束的任务（列表通常按创建先后，取最后一个更贴近重试后的真实执行）
    for j in reversed(candidates):
        if j.get("status") in {"success", "failed", "canceled", "running"}:
            return j
    return candidates[-1]


async def _collect_sonar_report(
    cfg: Dict[str, Any],
    gitlab_client: GitLabClient,
    project_id: int,
    jobs: List[Dict[str, Any]],
    state: Dict[str, Any],
    p_status: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    综合 GitLab Job 日志、SonarQube Web API 与 sonar-report.md artifact 收集报告，
    若前置编译即失败且无 Sonar 报告则返回 AI 故障诊断信息。
    """
    repo_conf = _find_repo_conf(cfg, state)
    sonar_cfg = resolve_sonar_config(cfg, repo=repo_conf)
    top_issues = sonar_cfg["top_issues"]

    sonar_job = _pick_sonar_job(jobs)
    trace = ""
    if sonar_job and sonar_job.get("id"):
        trace = await gitlab_client.get_job_trace(project_id, sonar_job["id"])

    ctx = parse_sonar_context_from_trace(trace)
    server_url = sonar_cfg["base_url"] or ctx.get("server_url", "")
    project_key = sonar_cfg["project_key"] or state.get("sonar_project_key") or ctx.get("project_key", "")
    ce_task_id = ctx.get("ce_task_id", "")
    dashboard_url = ctx.get("dashboard_url", "")
    if not dashboard_url and server_url and project_key:
        dashboard_url = f"{server_url}/dashboard?id={project_key}"

    api_report: Optional[Dict[str, Any]] = None
    if server_url and (project_key or ce_task_id):
        try:
            client = SonarClient(base_url=server_url, token=sonar_cfg["token"])
            api_report = await client.fetch_report(
                project_key=project_key,
                ce_task_id=ce_task_id,
                dashboard_url=dashboard_url,
                project_name=ctx.get("project_name", ""),
                scan_version=ctx.get("scan_version", ""),
                top_issues=top_issues,
            )
        except Exception as e:
            logger.warning("SonarQube API fetch failed server=%s err=%s", server_url, e)
            api_report = {
                "source": "api",
                "project_key": project_key,
                "project_name": ctx.get("project_name", ""),
                "scan_version": ctx.get("scan_version", ""),
                "dashboard_url": dashboard_url,
                "qg_status": ctx.get("qg_status") or "UNKNOWN",
                "qg_conditions": [],
                "measures": {},
                "issue_total": -1,
                "issues": [],
                "hotspot_total": -1,
                "hotspots": [],
                "auth_denied": False,
                "error": str(e),
            }

    # 若 API 获取到完整结构化数据（有质量门禁状态或指标），直接使用
    if api_report and not api_report.get("auth_denied") and not api_report.get("error"):
        if api_report.get("measures") or api_report.get("qg_conditions") or api_report.get("qg_status") not in {"", "UNKNOWN"}:
            return api_report, ""

    # 降级 1：尝试读取 CI Job 归档文件 sonar-report.md（支持零 Token 开箱即用）
    artifact_report: Optional[Dict[str, Any]] = None
    if sonar_job and sonar_job.get("id"):
        artifact_md = await gitlab_client.get_job_artifact_file(project_id, sonar_job["id"], "sonar-report.md")
        if artifact_md:
            artifact_report = parse_sonar_report_from_text(artifact_md, top_issues=top_issues)
            if artifact_report and not artifact_report.get("auth_denied") and (
                artifact_report.get("measures") or artifact_report.get("qg_conditions") or artifact_report.get("qg_status") != "UNKNOWN"
            ):
                if not artifact_report.get("dashboard_url") and dashboard_url:
                    artifact_report["dashboard_url"] = dashboard_url
                return artifact_report, ""

    # 降级 2：解析 Job 日志中 common/sonar.yml 打印的报告汇总块
    trace_report = parse_sonar_report_from_text(trace, top_issues=top_issues) if trace else None
    if trace_report and not trace_report.get("auth_denied") and (
        trace_report.get("measures") or trace_report.get("qg_conditions") or trace_report.get("qg_status") != "UNKNOWN"
    ):
        if not trace_report.get("dashboard_url") and dashboard_url:
            trace_report["dashboard_url"] = dashboard_url
        return trace_report, ""

    # 降级 3：若扫描确实产生过 Sonar 上下文/门禁状态，保留至少门禁与直链信息（含 401/403 提示）
    best_partial = api_report or artifact_report or trace_report
    if best_partial and (
        best_partial.get("dashboard_url")
        or best_partial.get("project_key")
        or best_partial.get("qg_status") not in {"", "UNKNOWN"}
    ):
        if best_partial.get("qg_status") in {"", "UNKNOWN"} and ctx.get("qg_status"):
            best_partial["qg_status"] = ctx["qg_status"]
        return best_partial, ""

    if ctx.get("qg_status") or ctx.get("dashboard_url"):
        return {
            "source": "trace",
            "project_key": project_key,
            "project_name": ctx.get("project_name", ""),
            "scan_version": ctx.get("scan_version", ""),
            "dashboard_url": dashboard_url,
            "qg_status": ctx.get("qg_status") or "UNKNOWN",
            "qg_conditions": [],
            "measures": {},
            "issue_total": -1,
            "issues": [],
            "hotspot_total": -1,
            "hotspots": [],
            "auth_denied": False,
            "error": "",
        }, ""

    # 兜底：若流水线失败且完全没产生 Sonar 报告（如前置 Maven 编译出错或 Runner 异常），执行日志诊断
    failed_job_info = ""
    if p_status in {"failed", "canceled"}:
        failed_jobs = [j for j in jobs if j.get("status") in {"failed", "canceled"}]
        fj = failed_jobs[0] if failed_jobs else sonar_job
        if fj:
            fj_name = fj.get("name", "unknown")
            fj_url = fj.get("web_url", "")
            failed_job_info = f"环节 **{fj_name}** 执行异常"
            if fj_url:
                failed_job_info += f"  —  [查看报错日志]({fj_url})"
            fj_trace = trace if (sonar_job and fj.get("id") == sonar_job.get("id")) else await gitlab_client.get_job_trace(project_id, fj["id"])
            if fj_trace:
                from services.code_review import diagnose_job_log
                AI_DIAGNOSIS_TRIGGERED.labels(project=state.get("project", ""), repo=state.get("repo", "")).inc()
                ai_cause = await diagnose_job_log(cfg, fj_name, fj_trace[-3000:])
                failed_job_info += f"\n\n**🤖 AI 故障诊断**：{ai_cause}"

    return None, failed_job_info


async def poll_sonar_pipeline_status(
    feishu_client: FeishuClient,
    gitlab_client: GitLabClient,
    cfg: Dict[str, Any],
    project_id: int,
    pipeline_id: int,
    sub_message_ids: list,
    state: Dict[str, Any],
    lock_scope: str,
    operator_open_id: str = "",
) -> None:
    """异步轮询 Sonar 扫描流水线直到终态，拉取报告并更新飞书结果卡片"""
    final_status = "unknown"
    final_qg = "unknown"
    start_time = asyncio.get_event_loop().time()
    last_active_job = ""

    try:
        for _ in range(360):  # 最多轮询 30 分钟 (360 * 5s)
            await asyncio.sleep(5)
            try:
                pipeline = await gitlab_client.get_pipeline(project_id, pipeline_id)
                if not pipeline:
                    continue

                p_status = pipeline.get("status", "created")
                pipeline_url = pipeline.get("web_url") or ""
                jobs = await gitlab_client.get_pipeline_jobs(project_id, pipeline_id)

                active_jobs = [j for j in jobs if j.get("status") == "running"]
                if not active_jobs:
                    active_jobs = [j for j in jobs if j.get("status") == "pending"]
                active_job_name = active_jobs[0]["name"] if active_jobs else "阶段衔接中"

                commit_sha = pipeline.get("sha") or ""
                commit_info = f"[{commit_sha[:8]}]({pipeline_url})" if pipeline_url and commit_sha else commit_sha[:8]
                if commit_sha:
                    commit_data = await gitlab_client.get_commit(project_id, commit_sha)
                    if commit_data:
                        author = commit_data.get("author_name", "")
                        title = commit_data.get("title", "")
                        if author and title:
                            commit_info += f" - {title} ({author})"

                is_finished = p_status in {"success", "failed", "canceled"}
                if not is_finished:
                    # 仅当进度变化时更新追踪卡片，避免频繁刷新限流
                    if active_job_name != last_active_job:
                        last_active_job = active_job_name
                        tracking_card = build_sonar_card(
                            state=state,
                            operator_open_id=operator_open_id,
                            pipeline_id=pipeline_id,
                            p_status=p_status,
                            active_job_name=active_job_name,
                            commit_info=commit_info,
                            pipeline_url=pipeline_url,
                        )
                        for sid in sub_message_ids:
                            if sid:
                                await feishu_client.update_card(sid, tracking_card)
                    continue

                # 终态：收集 SonarQube 报告
                final_status = p_status
                report, failed_job_info = await _collect_sonar_report(
                    cfg, gitlab_client, project_id, jobs, state, p_status
                )
                if report and report.get("qg_status"):
                    final_qg = str(report["qg_status"]).lower()
                else:
                    final_qg = final_status

                result_card = build_sonar_card(
                    state=state,
                    operator_open_id=operator_open_id,
                    pipeline_id=pipeline_id,
                    p_status=p_status,
                    active_job_name=active_job_name,
                    report=report,
                    failed_job_info=failed_job_info,
                    commit_info=commit_info,
                    pipeline_url=pipeline_url,
                )
                for sid in sub_message_ids:
                    if sid:
                        await feishu_client.update_card(sid, result_card)

                # 缓存元数据供责任人点击「已阅/排查」按钮时重绘卡片
                try:
                    redis = get_redis()
                    if redis:
                        meta_data = {
                            "card_kind": "sonar",
                            "state": state,
                            "operator_open_id": operator_open_id,
                            "pipeline_id": pipeline_id,
                            "p_status": p_status,
                            "active_job_name": active_job_name,
                            "report": report,
                            "failed_job_info": failed_job_info,
                            "commit_info": commit_info,
                            "pipeline_url": pipeline_url,
                            "sub_message_ids": sub_message_ids,
                        }
                        await redis.set(
                            f"cardops:sonar_card_meta:{pipeline_id}",
                            json.dumps(meta_data, ensure_ascii=False),
                            ex=7 * 86400,
                        )
                except Exception as e:
                    logger.warning("failed to cache sonar_card_meta pipeline=%s err=%s", pipeline_id, e)
                break
            except Exception as e:
                logger.error("poll sonar pipeline error pipeline=%s err=%s", pipeline_id, e)
    finally:
        await unlock_sonar(lock_scope)
        elapsed = asyncio.get_event_loop().time() - start_time
        SONAR_SCAN_COMPLETED.labels(
            project=state.get("project", ""),
            repo=state.get("repo", ""),
            status=final_qg if final_qg != "unknown" else final_status,
        ).inc()
        SONAR_SCAN_DURATION.labels(
            project=state.get("project", ""),
            repo=state.get("repo", ""),
        ).observe(elapsed)


async def background_run_sonar(
    feishu_client: FeishuClient,
    gitlab_client: GitLabClient,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    operator_open_id: str,
    open_chat_id: str,
    lock_scope: str,
) -> None:
    """后台触发 GitLab SonarQube 扫描流水线并创建飞书追踪卡片"""
    lock_handed_off = False
    try:
        variables: Dict[str, str] = {
            "ENV": state["env"],
            "DEPLOY_ENV": state["env"],
            "ACTION": "sonar",
        }
        if state.get("module") and "TARGET_MODULE" not in (state.get("variables") or {}):
            variables["TARGET_MODULE"] = state["module"]
        if state.get("variables"):
            for k, v in state["variables"].items():
                variables[k] = str(v)
        # 确保 ACTION 强制为 sonar
        variables["ACTION"] = "sonar"

        try:
            operator_name = await feishu_client.get_user_name(operator_open_id)
        except Exception:
            operator_name = operator_open_id

        variables["OPERATOR_OPEN_ID"] = operator_open_id
        variables["OPERATOR_NAME"] = operator_name

        pipeline = await gitlab_client.trigger_pipeline(
            project_id=state["repo_id"],
            ref=state["branch"],
            variables=variables,
        )
        pipeline_id = pipeline.get("id")
        pipeline_url = pipeline.get("web_url") or ""
        commit_sha = pipeline.get("sha") or ""
        commit_info = f"[{commit_sha[:8]}]({pipeline_url})" if pipeline_url and commit_sha else commit_sha[:8]
        if commit_sha:
            commit_data = await gitlab_client.get_commit(state["repo_id"], commit_sha)
            if commit_data:
                author = commit_data.get("author_name", "")
                title = commit_data.get("title", "")
                if author and title:
                    commit_info += f" - {title} ({author})"

        initial_card = build_sonar_card(
            state=state,
            operator_open_id=operator_open_id or "",
            pipeline_id=pipeline_id,
            p_status="created",
            active_job_name="SonarQube 扫描流水线初始化",
            commit_info=commit_info,
            pipeline_url=pipeline_url,
        )

        sub_message_ids: List[str] = []
        if open_chat_id:
            resp = await feishu_client.send_card(open_chat_id, initial_card)
            msg_id = resp.get("data", {}).get("message_id")
            if msg_id:
                sub_message_ids.append(msg_id)

        audit_chat_id = cfg.get("feishu", {}).get("audit_chat_id")
        if audit_chat_id and audit_chat_id != open_chat_id:
            audit_resp = await feishu_client.send_card(audit_chat_id, initial_card)
            audit_msg_id = audit_resp.get("data", {}).get("message_id")
            if audit_msg_id:
                sub_message_ids.append(audit_msg_id)

        if sub_message_ids:
            lock_handed_off = True
            asyncio.create_task(
                poll_sonar_pipeline_status(
                    feishu_client=feishu_client,
                    gitlab_client=gitlab_client,
                    cfg=cfg,
                    project_id=state["repo_id"],
                    pipeline_id=pipeline_id,
                    sub_message_ids=sub_message_ids,
                    state=state,
                    lock_scope=lock_scope,
                    operator_open_id=operator_open_id,
                )
            )
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text[:300]
        except Exception:
            detail = str(e)
        logger.error("trigger sonar pipeline http error status=%s detail=%s", e.response.status_code, detail)
        hint = "（请确认该仓库的 `.gitlab-ci.yml` 已接入 `ACTION == \"sonar\"` 扫描任务）" if e.response.status_code == 400 else ""
        err_card = build_sonar_card(
            state=state,
            operator_open_id=operator_open_id or "",
            pipeline_id=0,
            p_status="failed",
            failed_job_info=f"触发 Sonar 扫描流水线失败 (HTTP {e.response.status_code}) {hint}\n详情：`{detail}`",
        )
        if open_chat_id:
            try:
                await feishu_client.send_card(open_chat_id, err_card)
            except Exception:
                pass
    except Exception as e:
        logger.exception("background_run_sonar failed: %s", e)
        err_card = build_sonar_card(
            state=state,
            operator_open_id=operator_open_id or "",
            pipeline_id=0,
            p_status="failed",
            failed_job_info=f"触发 Sonar 扫描失败：{e}",
        )
        if open_chat_id:
            try:
                await feishu_client.send_card(open_chat_id, err_card)
            except Exception:
                pass
    finally:
        if not lock_handed_off:
            await unlock_sonar(lock_scope)
