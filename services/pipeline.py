import asyncio
import json
import logging
from typing import Any, Dict

from core.card_builder import build_card, build_sub_card
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.state import unlock_repo
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
    sub_message_id: str,
    state: Dict[str, Any],
    operator_open_id: str = "",
    open_chat_id: str = "",
    open_message_id: str = "",
) -> None:
    final_status = "unknown"
    for i in range(120):  # poll up to 10 minutes
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
            if p_status == "failed":
                failed_jobs = [j for j in jobs if j.get("status") == "failed"]
                if failed_jobs:
                    fj = failed_jobs[0]
                    fj_name = fj.get("name", "unknown")
                    fj_url = fj.get("web_url", "")
                    failed_job_info = f"环节 **{fj_name}** 执行失败"
                    if fj_url:
                        failed_job_info += f"  —  [查看报错日志]({fj_url})"

            commit_sha = (pipeline.get("sha") or "")[:8]
            commit_url = pipeline.get("web_url") or ""
            commit_info = f"[{commit_sha}]({commit_url})" if commit_url else commit_sha

            sub_card = build_sub_card(state, operator_open_id, pipeline_id, p_status, active_job_name, failed_job_info, commit_info)
            await feishu_client.update_card(sub_message_id, sub_card)

            if p_status in {"success", "failed", "canceled"}:
                final_status = p_status
                break
        except Exception as e:
            logger.error("poll pipeline error %s", e)

    # 更新历史记录状态
    repo_id = state.get("repo_id")
    if repo_id:
        await update_record_status(repo_id, pipeline_id, final_status)
        await unlock_repo(repo_id)

    try:
        unlocked_card = build_card(
            cfg,
            status="就绪",
            state=state,
            latest_pipeline_text="暂无",
            latest_result_text="上一任务已结束，锁已释放",
            show_details=True,
        )
        if open_message_id:
            await feishu_client.update_card(open_message_id, unlocked_card)
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
        if state.get("module"):
            variables["TARGET_MODULE"] = state["module"]

        try:
            operator_name = await feishu_client.get_user_name(operator_open_id)
        except Exception:
            operator_name = operator_open_id

        variables["OPERATOR_OPEN_ID"] = operator_open_id
        variables["OPERATOR_NAME"] = operator_name

        pipeline = await gitlab_client.trigger_pipeline(project_id=state["repo_id"], ref=state["branch"], variables=variables)
        pipeline_id = pipeline.get("id")

        await add_record(
            repo_id=state["repo_id"], pipeline_id=pipeline_id, project_name=state["project"], repo_name=state["repo"],
            branch=state["branch"], env=state["env"], operator_open_id=operator_open_id, operator_name=operator_name,
            module=state.get("module"), status="running"
        )

        if open_chat_id:
            commit_sha = (pipeline.get("sha") or "")[:8]
            commit_url = pipeline.get("web_url") or ""
            commit_info = f"[{commit_sha}]({commit_url})" if commit_url else commit_sha
            initial_sub_card = build_sub_card(state, operator_open_id or "", pipeline_id, "created", "流水线初始化", "", commit_info)
            resp = await feishu_client.send_card(open_chat_id, initial_sub_card)
            sub_msg_id = resp.get("data", {}).get("message_id")
            if sub_msg_id:
                asyncio.create_task(poll_pipeline_status(feishu_client, gitlab_client, cfg, state["repo_id"], pipeline_id, sub_msg_id, state, operator_open_id, open_chat_id, open_message_id))
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
