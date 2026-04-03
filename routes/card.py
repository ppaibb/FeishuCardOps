import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.card_builder import build_card, build_history_card, normalize_selection
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.permissions import check_approval_required, check_permission
from core.state import (
    check_action_dedup,
    get_card_state,
    is_repo_locked,
    try_lock_repo,
    lock_repo,
    save_card_state,
)
from services.approval import create_approval, get_approval, resolve_approval
from services.history import get_history
from services.pipeline import background_run_pipeline, delayed_update_card, next_card_version

logger = logging.getLogger("feishu_gitlab_card_http")

router = APIRouter()


@router.post("/feishu/card")
async def feishu_card(request: Request):
    payload = await request.json()
    logger.info("/feishu/card payload=%s", json.dumps(payload, ensure_ascii=False)[:3000])

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    event = payload.get("event", {})
    action = event.get("action", {}) or {}
    raw_value = action.get("value") or {}
    form_value = action.get("form_value") or {}

    def pick_value(key: str) -> Optional[str]:
        if isinstance(raw_value, dict):
            direct = raw_value.get(key)
            if isinstance(direct, str) and direct:
                return direct
        form_direct = form_value.get(key)
        if isinstance(form_direct, str) and form_direct:
            return form_direct
        if isinstance(form_direct, dict):
            for candidate_key in ("value", "key", key):
                candidate = form_direct.get(candidate_key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        option = action.get("option")
        if action.get("name") == key and isinstance(option, str) and option:
            return option
        return None

    cfg = load_config()
    context = event.get("context", {}) or {}
    open_message_id = context.get("open_message_id") or ""
    open_chat_id = context.get("open_chat_id") or context.get("chat_id") or ""
    operator = event.get("operator", {}) or {}
    operator_open_id = operator.get("open_id") or ""

    stored_state = await get_card_state(open_message_id)

    logger.info("CARD_DEBUG msg_id=%s tag=%s name=%s option=%s current_field=%s raw_value=%s",
                open_message_id, action.get("tag"), action.get("name"), action.get("option"),
                raw_value.get("current_field") if isinstance(raw_value, dict) else None,
                json.dumps(raw_value, ensure_ascii=False)[:500] if isinstance(raw_value, dict) else raw_value)

    action_name = None
    if isinstance(raw_value, dict):
        action_name = raw_value.get("action")
    if not action_name and isinstance(form_value, dict):
        action_name = form_value.get("action")
    if not action_name and action.get("tag") == "button":
        text_obj = action.get("text") or {}
        button_text = text_obj.get("content", "") if isinstance(text_obj, dict) else ""
        if "执行" in button_text:
            action_name = "run"
        elif "刷新" in button_text:
            action_name = "refresh"
        elif "历史" in button_text:
            action_name = "history"
        elif "批准" in button_text:
            action_name = "approve"
        elif "驳回" in button_text:
            action_name = "reject"
    if not action_name:
        action_name = action.get("name")
    current_field = raw_value.get("current_field") if isinstance(raw_value, dict) else None
    option = action.get("option") if isinstance(action.get("option"), str) else None

    # ── 审批流处理 ──────────────────────────────────────────────
    if action_name in {"approve", "reject"}:
        approval_id = raw_value.get("approval_id") if isinstance(raw_value, dict) else None
        if not approval_id:
            return JSONResponse({"toast": {"type": "error", "content": "审批记录无效"}})
            
        # 审批去重，避免并发操作导致重复触发发版
        dedup_key = f"approval_lock:{approval_id}"
        if not await check_action_dedup(dedup_key, 2):
            logger.info("dedup hit approval %s by %s", approval_id, operator_open_id)
            return JSONResponse({"toast": {"type": "info", "content": "处理中，请稍候"}})
            
        result = await resolve_approval(approval_id, action_name, operator_open_id)
        toast_type = "success" if result["ok"] else "error"
        return JSONResponse({"toast": {"type": toast_type, "content": result["msg"]}})

    # Collect incoming config variables
    incoming_vars = {}
    if isinstance(raw_value, dict):
        for k, v in raw_value.items():
            if k.startswith("var_"):
                incoming_vars[k[4:]] = str(v)
                
    selected_vars = stored_state.get("variables", {}).copy() if stored_state else {}
    selected_vars.update(incoming_vars)

    # ── 常规卡片交互：解析选择项 ────────────────────────────────
    if current_field == "project" and option:
        projects = cfg.get("projects", [])
        project = next((p for p in projects if p["name"] == option), None)
        if project and project.get("repos"):
            pick_repo = project["repos"][0]["name"]
        else:
            pick_repo = None
        selected_project = option
        selected_repo = pick_repo
        selected_branch = None
        selected_env = stored_state.get("env")
    elif action.get("tag") == "select_static":
        selected_project = stored_state.get("project") or pick_value("project")
        selected_repo = stored_state.get("repo") or pick_value("repo")
        selected_branch = stored_state.get("branch") or pick_value("branch")
        selected_env = stored_state.get("env") or pick_value("env")
        if current_field == "repo" and option:
            selected_repo = option
            selected_branch = None
            selected_vars = {}
        elif current_field == "branch" and option:
            selected_branch = option
        elif current_field == "env" and option:
            selected_env = option
        elif current_field and current_field.startswith("var_") and option:
            selected_vars[current_field[4:]] = option
    elif action_name in {"run", "refresh", "history"} and stored_state:
        selected_project = stored_state.get("project")
        selected_repo = stored_state.get("repo")
        selected_branch = stored_state.get("branch")
        selected_env = stored_state.get("env")
    else:
        selected_project = pick_value("project") or stored_state.get("project")
        selected_repo = pick_value("repo") or stored_state.get("repo")
        selected_branch = pick_value("branch") or stored_state.get("branch")
        selected_env = pick_value("env") or stored_state.get("env")

    logger.info(
        "card action parsed open_message_id=%s action_name=%s current_field=%s option=%s stored_state=%s selected_project=%s selected_repo=%s selected_branch=%s selected_env=%s selected_vars=%s",
        open_message_id, action_name, current_field, option, stored_state,
        selected_project, selected_repo, selected_branch, selected_env, selected_vars,
    )

    gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])

    cached_branches = None
    if current_field not in ["project", "repo", "refresh"] and stored_state.get("repo") == selected_repo and stored_state.get("branches"):
        cached_branches = stored_state.get("branches")

    state = await normalize_selection(cfg, gitlab, selected_project, selected_repo, selected_branch, selected_env, cached_branches, selected_vars=selected_vars)

    if open_message_id:
        await save_card_state(open_message_id, state)
    logger.info("card action merged state=%s", state)

    # ── 去重 ───────────────────────────────────────────────────
    dedup_key = ""
    if action_name in {"run", "refresh", "history", "review"}:
        dedup_key = f"{open_message_id}:{action_name}:{state.get('project')}:{state.get('repo')}:{state.get('branch')}:{state.get('env')}"
        dedup_window = 4 if action_name in {"run", "review"} else 2
        if not await check_action_dedup(dedup_key, dedup_window):
            logger.info("dedup hit key=%s", dedup_key)
            return JSONResponse({})

    status = "就绪"
    latest_pipeline_text = "暂无"
    latest_result_text = "就绪"
    pipeline_id = None

    feishu_client = FeishuClient(app_id=cfg["feishu"]["app_id"], app_secret=cfg["feishu"]["app_secret"])

    # ── locked 按钮 ─────────────────────────────────────────────
    if action_name == "locked":
        return JSONResponse({"toast": {"type": "info", "content": "当前仓库正在发布排队中，请等待上一任务完成！"}})

    # ── 历史记录 ─────────────────────────────────────────────────
    if action_name == "history":
        records = await get_history(state["repo_id"])
        history_card = build_history_card(records, state)
        if open_message_id:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, history_card, 0.05))
        return JSONResponse({})

    # ── AI Code Review ────────────────────────────────────────────
    if action_name == "review":
        from services.code_review import run_code_review
        asyncio.create_task(run_code_review(feishu_client, gitlab, state, open_chat_id, operator_open_id))
        return JSONResponse({"toast": {"type": "info", "content": "🤖 AI 正在审查代码，结果稍后发送到群聊..."}})

    # ── 执行发版 ─────────────────────────────────────────────────
    if action_name == "run":
        if "⚠️" in state["branch"]:
            return JSONResponse({"toast": {"type": "error", "content": "远程分支读取失败，请重新切一次仓库刷出正常分支后再执行！"}})

        if await is_repo_locked(state["repo_id"]):
            logger.warning("Repo %s is locked, run rejected.", state["repo_id"])
            return JSONResponse({"toast": {"type": "error", "content": "该仓正在发布，请稍后"}})

        # ── 权限检查 ────────────────────────────────────────────
        allowed, reason = check_permission(cfg, state["project"], state["env"], operator_open_id)
        if not allowed:
            logger.warning("permission denied user=%s project=%s env=%s reason=%s", operator_open_id, state["project"], state["env"], reason)
            return JSONResponse({"toast": {"type": "error", "content": f"🔒 权限不足：{reason}"}})

        # ── 审批检查 ────────────────────────────────────────────
        approvers = check_approval_required(cfg, state["project"], state["env"])
        if approvers:
            approval_id = await create_approval(
                feishu_client, gitlab, cfg, state,
                open_message_id, operator_open_id, open_chat_id, approvers,
            )
            return JSONResponse({"toast": {"type": "info", "content": f"🔐 已发起审批请求 (#{approval_id})，请等待审批人操作"}})

        # ── 直接触发 ────────────────────────────────────────────
        if not await try_lock_repo(state["repo_id"]):
            logger.warning("Repo %s locked during try_lock, run rejected.", state["repo_id"])
            return JSONResponse({"toast": {"type": "error", "content": "该仓正在发布，被并发锁定，请稍后"}})
            
        asyncio.create_task(background_run_pipeline(feishu_client, gitlab, cfg, state, open_message_id, operator_open_id, open_chat_id))

        card = build_card(cfg, status="执行中", state=state, latest_pipeline_text="初始化中...", latest_result_text="已接收发版指令正在投递...", show_details=True, is_locked=True)
        if open_message_id:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, card, 1.0))

        return JSONResponse({"toast": {"type": "info", "content": "已触发发版！进度指示卡马上发出..."}})

    # ── 刷新 ─────────────────────────────────────────────────────
    elif action_name == "refresh":
        try:
            status = "刷新中"
            latest = await gitlab.latest_pipeline(project_id=state["repo_id"], ref=state["branch"])
            if latest:
                pipeline_id = latest.get("id")
                pipeline_status = latest.get("status")
                latest_pipeline_text = f"#{pipeline_id} / {pipeline_status}"
                latest_result_text = f"最近 Pipeline #{pipeline_id} 状态: {pipeline_status}"
                if str(pipeline_status).lower() == "success":
                    status = "就绪"
                elif str(pipeline_status).lower() in {"failed", "canceled"}:
                    status = "异常"
                else:
                    status = "处理中"
            else:
                status = "就绪"
                latest_result_text = f"分支 {state['branch']} 暂无 Pipeline"
                latest_pipeline_text = "暂无"
        except Exception as e:
            logger.exception("refresh pipeline failed")
            status = "异常"
            latest_result_text = f"刷新失败: {e}"

    card = build_card(
        cfg,
        status=status,
        state=state,
        latest_pipeline_text=latest_pipeline_text,
        latest_result_text=latest_result_text,
        show_details=(action_name in {"run", "refresh"} or action.get("tag") == "select_static"),
        is_locked=await is_repo_locked(state.get("repo_id", -1)),
    )

    response_body = {}
    if open_message_id:
        ver = next_card_version(open_message_id)
        delay = 0 if action.get("tag") == "select_static" else 0.05
        asyncio.create_task(delayed_update_card(feishu_client, open_message_id, card, delay, version=ver))

    logger.info("/feishu/card response=%s", json.dumps(response_body, ensure_ascii=False)[:3000])
    return JSONResponse(response_body)
