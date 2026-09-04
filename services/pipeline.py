import asyncio
import json
import logging
from typing import Any, Dict

from core.card_builder import build_card, build_sub_card
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.redis_client import get_redis
from core.state import unlock_repo
from core.metrics import PIPELINE_COMPLETED, PIPELINE_DURATION, AI_DIAGNOSIS_TRIGGERED, ACTIVE_LOCKS
from services.history import add_record, update_record_status

logger = logging.getLogger("feishu_gitlab_card_http")


# 每个 message_id 的最新更新版本号，防止旧的异步推送覆盖新的
_card_update_versions: Dict[str, int] = {}
_version_counter = 0


def next_card_version(open_message_id: str) -> int:
    """为指定 message_id 分配新版本号，返回版本号"""
    global _version_counter
    _version_counter += 1
    _card_update_versions[open_message_id] = _version_counter
    return _version_counter


async def delayed_update_card(feishu_client: FeishuClient, open_message_id: str, card: Dict[str, Any], delay_seconds: float = 1.0, version: int = 0) -> None:
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        # 版本检查：如果有更新的版本已经注册，放弃本次推送
        if version and _card_update_versions.get(open_message_id, 0) != version:
            logger.info("skipped stale card update open_message_id=%s version=%s current=%s", open_message_id, version, _card_update_versions.get(open_message_id))
            return
        result = await feishu_client.update_card(open_message_id, card)
        logger.info("delayed updated current card open_message_id=%s result=%s", open_message_id, json.dumps(result, ensure_ascii=False)[:1000])
    except Exception:
        logger.exception("delayed update current card failed open_message_id=%s", open_message_id)


async def poll_pipeline_status(
    feishu_client: FeishuClient,
    gitlab_client: GitLabClient,
    cfg: Dict[str, Any],
    project_id: int,
    pipeline_id: int,
    sub_message_ids: list,
    state: Dict[str, Any],
    operator_open_id: str = "",
    open_message_id: str = "",
) -> None:
    final_status = "unknown"
    start_time = asyncio.get_event_loop().time()
    for i in range(720):  # poll up to 60 minutes (720 * 5s = 3600s)
        await asyncio.sleep(5)
        try:
            pipeline = await gitlab_client.get_pipeline(project_id, pipeline_id)
            if not pipeline:
                continue

            p_status = pipeline.get("status", "created")
            jobs = await gitlab_client.get_pipeline_jobs(project_id, pipeline_id)
            
            active_jobs = [j for j in jobs if j.get("status") == "running"]
            if not active_jobs:
                active_jobs = [j for j in jobs if j.get("status") == "pending"]
            active_job_name = active_jobs[0]["name"] if active_jobs else "阶段衔接中"

            failed_job_info = ""
            if p_status in {"failed", "canceled"}:
                failed_jobs = [j for j in jobs if j.get("status") in {"failed", "canceled"}]
                if failed_jobs:
                    fj = failed_jobs[0]
                    fj_name = fj.get("name", "unknown")
                    fj_url = fj.get("web_url", "")
                    failed_job_info = f"环节 **{fj_name}** 执行失败"
                    if fj_url:
                        failed_job_info += f"  —  [查看报错日志]({fj_url})"
                    
                    # 取最后 3000 字符让 AI 分析
                    trace = await gitlab_client.get_job_trace(project_id, fj["id"])
                    if trace:
                        trace_tail = trace[-3000:]
                        from services.code_review import diagnose_job_log
                        AI_DIAGNOSIS_TRIGGERED.labels(project=state.get("project", ""), repo=state.get("repo", "")).inc()
                        ai_cause = await diagnose_job_log(cfg, fj_name, trace_tail)
                        failed_job_info += f"\n\n**🤖 AI 故障诊断**：{ai_cause}"

            commit_sha = pipeline.get("sha") or ""
            commit_url = pipeline.get("web_url") or ""
            commit_info = f"[{commit_sha[:8]}]({commit_url})" if commit_url and commit_sha else commit_sha[:8]
            
            if commit_sha:
                commit_data = await gitlab_client.get_commit(project_id, commit_sha)
                if commit_data:
                    author = commit_data.get("author_name", "")
                    title = commit_data.get("title", "")
                    if author and title:
                        commit_info += f" - {title} ({author})"

            sub_card = build_sub_card(state, operator_open_id, pipeline_id, p_status, active_job_name, failed_job_info, commit_info)
            for sid in sub_message_ids:
                if sid:
                    await feishu_client.update_card(sid, sub_card)

            if p_status in {"success", "failed", "canceled"}:
                final_status = p_status
                # 缓存 sub_card 元数据，供责任人点击确认「收到/排查」时重新渲染卡片
                try:
                    redis = get_redis()
                    if redis:
                        meta_data = {
                            "state": state,
                            "operator_open_id": operator_open_id,
                            "pipeline_id": pipeline_id,
                            "p_status": p_status,
                            "active_job_name": active_job_name,
                            "failed_job_info": failed_job_info,
                            "commit_info": commit_info,
                        }
                        await redis.set(f"cardops:sub_card_meta:{pipeline_id}", json.dumps(meta_data, ensure_ascii=False), ex=7 * 86400)
                except Exception as e:
                    logger.warning("failed to cache sub_card_meta for pipeline %s: %s", pipeline_id, e)
                break
        except Exception as e:
            logger.error("poll pipeline error %s", e)

    # 更新历史记录状态及释放锁
    repo_id = state.get("repo_id")
    if repo_id:
        await update_record_status(repo_id, pipeline_id, final_status)
        await unlock_repo(repo_id)

    # ── 指标：记录发版结果与耗时 ─────────────────────────
    elapsed = asyncio.get_event_loop().time() - start_time
    PIPELINE_COMPLETED.labels(
        project=state.get("project", ""), repo=state.get("repo", ""),
        env=state.get("env", ""), status=final_status,
    ).inc()
    PIPELINE_DURATION.labels(
        project=state.get("project", ""), repo=state.get("repo", ""),
        env=state.get("env", ""),
    ).observe(elapsed)
    ACTIVE_LOCKS.dec()

    try:
        final_status_text = "就绪" if final_status == "success" else ("异常" if final_status in {"failed", "canceled"} else "处理中")
        unlocked_card = build_card(
            cfg,
            status=final_status_text,
            state=state,
            latest_pipeline_text=f"#{pipeline_id} / {final_status}",
            latest_result_text=f"最近 Pipeline #{pipeline_id} 状态: {final_status}",
            show_details=True,
        )
        if open_message_id:
            # 兼容旧并发卡片更新问题：获取新的 ver，推入延迟队列进行更新以防被冲掉
            ver = next_card_version(open_message_id)
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, unlocked_card, 0.5, version=ver))
    except Exception as e:
        logger.error("failed to unlock master card %s", e)


async def background_run_pipeline(
    feishu_client: FeishuClient,
    gitlab_client: GitLabClient,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    open_message_id: str,
    operator_open_id: str,
    open_chat_id: str,
) -> None:
    try:
        variables = {"ENV": state["env"], "DEPLOY_ENV": state["env"]}
        # 兼容老版，如果有 module 且没有 var_TARGET_MODULE，自动注入
        if state.get("module") and "TARGET_MODULE" not in state.get("variables", {}):
            variables["TARGET_MODULE"] = state["module"]
            
        # 自动注入所有配置的变量
        if state.get("variables"):
            for k, v in state["variables"].items():
                variables[k] = str(v)

        try:
            operator_name = await feishu_client.get_user_name(operator_open_id)
        except Exception:
            operator_name = operator_open_id

        variables["OPERATOR_OPEN_ID"] = operator_open_id
        variables["OPERATOR_NAME"] = operator_name

        pipeline = await gitlab_client.trigger_pipeline(project_id=state["repo_id"], ref=state["branch"], variables=variables)
        pipeline_id = pipeline.get("id")

        module_display = state.get("module")
        if not module_display and state.get("variables"):
            module_display = ", ".join([f"{k}={v}" for k, v in state["variables"].items()])

        await add_record(
            repo_id=state["repo_id"], pipeline_id=pipeline_id, project_name=state["project"], repo_name=state["repo"],
            branch=state["branch"], env=state["env"], operator_open_id=operator_open_id, operator_name=operator_name,
            module=module_display, status="running"
        )

        commit_sha = pipeline.get("sha") or ""
        commit_url = pipeline.get("web_url") or ""
        commit_info = f"[{commit_sha[:8]}]({commit_url})" if commit_url and commit_sha else commit_sha[:8]
        
        if commit_sha:
            commit_data = await gitlab_client.get_commit(state["repo_id"], commit_sha)
            if commit_data:
                author = commit_data.get("author_name", "")
                title = commit_data.get("title", "")
                if author and title:
                    commit_info += f" - {title} ({author})"
        initial_sub_card = build_sub_card(state, operator_open_id or "", pipeline_id, "created", "流水线初始化", "", commit_info)
        
        sub_message_ids = []
        if open_chat_id:
            resp = await feishu_client.send_card(open_chat_id, initial_sub_card)
            sub_msg_id = resp.get("data", {}).get("message_id")
            if sub_msg_id:
                sub_message_ids.append(sub_msg_id)
                
        # 审计群组同步逻辑
        audit_chat_id = cfg.get("feishu", {}).get("audit_chat_id")
        if audit_chat_id and audit_chat_id != open_chat_id:
            audit_resp = await feishu_client.send_card(audit_chat_id, initial_sub_card)
            audit_msg_id = audit_resp.get("data", {}).get("message_id")
            if audit_msg_id:
                sub_message_ids.append(audit_msg_id)

        if sub_message_ids:
            asyncio.create_task(poll_pipeline_status(feishu_client, gitlab_client, cfg, state["repo_id"], pipeline_id, sub_message_ids, state, operator_open_id, open_message_id))
    except Exception as e:
        logger.error("background run failed %s", e)
        if state.get("repo_id"):
            await unlock_repo(state["repo_id"])
        if open_message_id:
            card = build_card(cfg, status="异常", state=state, latest_pipeline_text="暂无", latest_result_text=f"触发失败: {e}", show_details=True)
            try:
                await feishu_client.update_card(open_message_id, card)
            except Exception:
                pass
