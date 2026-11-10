import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.card_builder import (
    build_card,
    build_history_card,
    build_project_management_card,
    build_sonar_card,
    build_sub_card,
    build_wiki_card,
    normalize_selection,
)
from services.project_manager import get_all_projects, get_dynamic_project_names, DYNAMIC_PROJECTS_KEY
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient, build_gitlab_client
from core.permissions import check_approval_required, check_permission, is_admin
from core.redis_client import get_redis
from core.state import (
    check_action_dedup,
    get_card_state,
    is_repo_locked,
    try_lock_repo,
    try_lock_sonar,
    try_lock_wiki,
    lock_repo,
    save_card_state,
)
from services.approval import create_approval, get_approval, resolve_approval
from services.history import get_history
from services.pipeline import background_run_pipeline, delayed_update_card, next_card_version
from services.repo_wiki import background_run_wiki, build_wiki_lock_scope, cancel_running_wiki
from services.sonar_scan import background_run_sonar, build_sonar_lock_scope
from core.metrics import (
    CARD_INTERACTION, APPROVAL_RESOLVED, PIPELINE_TRIGGERED,
    AI_REVIEW_TRIGGERED, PROJECT_DELETED, APPROVAL_TRIGGERED, ACTIVE_LOCKS,
    SONAR_SCAN_TRIGGERED, WIKI_GEN_TRIGGERED,
)

logger = logging.getLogger("feishu_gitlab_card_http")

router = APIRouter()


@router.post("/feishu/card")
async def feishu_card(request: Request):
    payload = await request.json()
    event = payload.get("event", {})
    action = event.get("action", {}) or {}
    logger.info("/feishu/card received action_tag=%s action_value=%s", action.get("tag", "unknown"), action.get("value", {}))

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    event = payload.get("event", {})
    action = event.get("action", {}) or {}
    operator_open_id = event.get("operator", {}).get("operator_id", {}).get("open_id")
    
    if operator_open_id:
        from services.notification import check_and_send_release_note
        asyncio.create_task(check_and_send_release_note(load_config(), operator_open_id))

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
    cfg["projects"] = await get_all_projects()
    context = event.get("context", {}) or {}
    open_message_id = context.get("open_message_id") or ""
    open_chat_id = context.get("open_chat_id") or context.get("chat_id") or ""
    operator = event.get("operator", {}) or {}
    operator_open_id = operator.get("open_id") or (operator.get("operator_id") or {}).get("open_id") or ""

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
        if "取消生成" in button_text:
            action_name = "cancel_wiki"
        elif "执行" in button_text:
            action_name = "run"
        elif "刷新" in button_text:
            action_name = "refresh"
        elif "历史" in button_text:
            action_name = "history"
        elif "Sonar" in button_text:
            action_name = "sonar_scan"
        elif "Wiki" in button_text or "wiki" in button_text:
            action_name = "repo_wiki"
        elif "批准" in button_text:
            action_name = "approve"
        elif "驳回" in button_text:
            action_name = "reject"
    if not action_name:
        action_name = action.get("name")
    current_field = raw_value.get("current_field") if isinstance(raw_value, dict) else None
    option = action.get("option") if isinstance(action.get("option"), str) else None

    # ── 指标：记录所有卡片交互 ─────────────────────────────────
    if action_name:
        CARD_INTERACTION.labels(action=action_name).inc()

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
            
        APPROVAL_RESOLVED.labels(action=action_name).inc()
        result = await resolve_approval(approval_id, action_name, operator_open_id)
        toast_type = "success" if result["ok"] else "error"
        return JSONResponse({"toast": {"type": toast_type, "content": result["msg"]}})

    # ── 发版结果确认闭环处理（收到/排查） ─────────────────────────
    if action_name == "acknowledge_sub_card":
        target_open_id = raw_value.get("operator_open_id") if isinstance(raw_value, dict) else None
        pipeline_id = raw_value.get("pipeline_id") if isinstance(raw_value, dict) else None

        # 1. 权限强校验：仅限被艾特的发版责任人确认
        if not operator_open_id or (target_open_id and operator_open_id != target_open_id):
            return JSONResponse({"toast": {"type": "warning", "content": "⚠️ 仅限被艾特的发版责任人确认哦！"}})

        if not pipeline_id:
            return JSONResponse({"toast": {"type": "error", "content": "参数缺失：未找到流水线 ID"}})

        # 2. 幂等拦截与防刷
        redis = get_redis()
        ack_key = f"cardops:pipeline_ack:{pipeline_id}"
        if redis:
            existing_ack = await redis.get(ack_key)
            if existing_ack:
                return JSONResponse({"toast": {"type": "info", "content": "您之前已确认过了，无需重复操作"}})

        # 3. 记录确认状态
        feishu_client = FeishuClient(app_id=cfg["feishu"]["app_id"], app_secret=cfg["feishu"]["app_secret"])
        try:
            operator_name = await feishu_client.get_user_name(operator_open_id)
        except Exception:
            operator_name = operator_open_id

        from datetime import datetime
        ack_info = {
            "ack_by": operator_open_id,
            "ack_by_name": operator_name,
            "ack_at": datetime.now().strftime("%H:%M"),
        }

        if redis:
            await redis.set(ack_key, json.dumps(ack_info, ensure_ascii=False), ex=7 * 86400)

        # 4. 读取卡片元数据重新渲染卡片（支持发版副卡与 Sonar 扫描结果卡）
        card_kind = raw_value.get("card_kind") if isinstance(raw_value, dict) else None
        sonar_card_meta = None
        if card_kind == "sonar" and redis:
            raw_sonar_meta = await redis.get(f"cardops:sonar_card_meta:{pipeline_id}")
            if raw_sonar_meta:
                try:
                    sonar_card_meta = json.loads(
                        raw_sonar_meta.decode("utf-8") if isinstance(raw_sonar_meta, bytes) else str(raw_sonar_meta)
                    )
                except Exception as e:
                    logger.warning("failed to parse sonar_card_meta for %s: %s", pipeline_id, e)

        if card_kind == "sonar":
            if sonar_card_meta:
                updated_card = build_sonar_card(
                    state=sonar_card_meta.get("state", {}),
                    operator_open_id=sonar_card_meta.get("operator_open_id", operator_open_id),
                    pipeline_id=pipeline_id,
                    p_status=sonar_card_meta.get("p_status", "success"),
                    active_job_name=sonar_card_meta.get("active_job_name", ""),
                    report=sonar_card_meta.get("report"),
                    failed_job_info=sonar_card_meta.get("failed_job_info", ""),
                    commit_info=sonar_card_meta.get("commit_info", ""),
                    pipeline_url=sonar_card_meta.get("pipeline_url", ""),
                    ack_info=ack_info,
                )
            else:
                p_status = raw_value.get("p_status", "failed") if isinstance(raw_value, dict) else "failed"
                dummy_state = stored_state or {"project": "流水线", "repo": f"#{pipeline_id}", "branch": "-", "env": "-"}
                updated_card = build_sonar_card(
                    state=dummy_state,
                    operator_open_id=operator_open_id,
                    pipeline_id=pipeline_id,
                    p_status=p_status,
                    ack_info=ack_info,
                )
        else:
            sub_card_meta = None
            if redis:
                raw_meta = await redis.get(f"cardops:sub_card_meta:{pipeline_id}")
                if raw_meta:
                    try:
                        sub_card_meta = json.loads(raw_meta.decode("utf-8") if isinstance(raw_meta, bytes) else str(raw_meta))
                    except Exception as e:
                        logger.warning("failed to parse sub_card_meta for %s: %s", pipeline_id, e)

            if sub_card_meta:
                updated_card = build_sub_card(
                    state=sub_card_meta.get("state", {}),
                    operator_open_id=sub_card_meta.get("operator_open_id", operator_open_id),
                    pipeline_id=pipeline_id,
                    p_status=sub_card_meta.get("p_status", "success"),
                    active_job_name=sub_card_meta.get("active_job_name", ""),
                    failed_job_info=sub_card_meta.get("failed_job_info", ""),
                    commit_info=sub_card_meta.get("commit_info", ""),
                    ack_info=ack_info,
                )
            else:
                p_status = raw_value.get("p_status", "success") if isinstance(raw_value, dict) else "success"
                dummy_state = stored_state or {"project": "流水线", "repo": f"#{pipeline_id}", "branch": "-", "env": "-"}
                updated_card = build_sub_card(
                    state=dummy_state,
                    operator_open_id=operator_open_id,
                    pipeline_id=pipeline_id,
                    p_status=p_status,
                    active_job_name="",
                    ack_info=ack_info,
                )

        target_message_ids = []
        if open_message_id:
            target_message_ids.append(open_message_id)
        if isinstance(sonar_card_meta, dict):
            for sid in sonar_card_meta.get("sub_message_ids") or []:
                if sid and sid not in target_message_ids:
                    target_message_ids.append(sid)

        for target_msg_id in target_message_ids:
            try:
                await feishu_client.update_card(target_msg_id, updated_card)
            except Exception as e:
                logger.error("failed to update sub_card after ack message_id=%s error=%s", target_msg_id, e)

        return JSONResponse({"toast": {"type": "success", "content": "✅ 已确认收到！"}})

    # ── 取消 RepoWiki 生成 ────────────────────────────────────────
    if action_name == "cancel_wiki":
        if not isinstance(raw_value, dict):
            return JSONResponse({"toast": {"type": "error", "content": "取消参数无效"}})

        state_dict = stored_state or {}
        lock_scope = str(raw_value.get("lock_scope") or "").strip()
        if not lock_scope:
            repo_id = raw_value.get("repo_id") or state_dict.get("repo_id")
            branch = raw_value.get("branch") or state_dict.get("branch") or "main"
            if repo_id:
                lock_scope = f"{repo_id}:{branch}:all"
        if not lock_scope:
            return JSONResponse({"toast": {"type": "error", "content": "参数缺失：未找到 RepoWiki 任务标识"}})

        dedup_key = f"cancel_wiki:{lock_scope}"
        if not await check_action_dedup(dedup_key, 2):
            return JSONResponse({"toast": {"type": "info", "content": "正在处理取消请求，请稍候"}})

        feishu_client = FeishuClient(app_id=cfg["feishu"]["app_id"], app_secret=cfg["feishu"]["app_secret"])
        canceler_name = operator_open_id
        if operator_open_id:
            try:
                canceler_name = await feishu_client.get_user_name(operator_open_id)
            except Exception:
                canceler_name = operator_open_id

        res = await cancel_running_wiki(
            lock_scope,
            canceler_open_id=operator_open_id,
            canceler_name=canceler_name,
        )
        if res.get("stale_unlocked") and open_message_id:
            card_state = {
                "project": raw_value.get("project") or state_dict.get("project") or "-",
                "repo": raw_value.get("repo") or state_dict.get("repo") or "-",
                "repo_id": raw_value.get("repo_id") or state_dict.get("repo_id") or 0,
                "branch": raw_value.get("branch") or state_dict.get("branch") or "-",
                "env": raw_value.get("env") or state_dict.get("env") or "",
            }
            canceled_card = build_wiki_card(
                state=card_state,
                operator_open_id=str(raw_value.get("operator_open_id") or operator_open_id),
                w_status="canceled",
                stage_text="后台任务已离线（并发锁已清理）",
                result_info={
                    "canceled_by": operator_open_id,
                    "canceled_by_name": canceler_name,
                },
                lock_scope=lock_scope,
            )
            try:
                await feishu_client.update_card(open_message_id, canceled_card)
            except Exception as e:
                logger.warning("failed updating stale canceled wiki card msg_id=%s err=%s", open_message_id, e)

        return JSONResponse({"toast": {"type": "success" if res.get("ok") else "warning", "content": res.get("msg", "")}})

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
    elif action_name in {"run", "refresh", "history", "sonar_scan", "repo_wiki", "manage_projects", "delete_project", "locked", "cancel_pipeline"} and stored_state:
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

    gitlab = build_gitlab_client(cfg) or GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])

    cached_branches = None
    if current_field not in ["project", "repo", "refresh"] and stored_state.get("repo") == selected_repo and stored_state.get("branches"):
        cached_branches = stored_state.get("branches")

    force_refresh_branches = (action_name == "refresh" or current_field in ["project", "repo"])
    state = await normalize_selection(
        cfg, gitlab, selected_project, selected_repo, selected_branch, selected_env, 
        cached_branches, selected_vars=selected_vars, force_refresh_branches=force_refresh_branches
    )

    # 依据解析出的仓库所绑定的 GitLab 实例，重建后续操作使用的客户端
    gitlab = build_gitlab_client(cfg, instance_name=state.get("gitlab_instance")) or gitlab

    if open_message_id:
        await save_card_state(open_message_id, state)
    logger.info("card action merged state=%s", state)

    dedup_key = ""
    if action_name in {"run", "refresh", "history", "review", "sonar_scan", "repo_wiki", "manage_projects", "delete_project", "cancel_pipeline"}:
        dedup_key = f"{open_message_id}:{action_name}:{state.get('project')}:{state.get('repo')}:{state.get('branch')}:{state.get('env')}"
        dedup_window = 4 if action_name in {"run", "review", "sonar_scan", "repo_wiki"} else 2
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
        return JSONResponse({"toast": {"type": "info", "content": "发版中"}})

    # ── 停止发版 ────────────────────────────────────────────────
    if action_name == "cancel_pipeline":
        if not await is_repo_locked(state["repo_id"]):
            return JSONResponse({"toast": {"type": "warning", "content": "当前没有正在执行的流水线！"}})
        try:
            latest = await gitlab.latest_pipeline(project_id=state["repo_id"], ref=state["branch"])
            if latest and latest.get("status") in {"running", "pending", "created"}:
                pipeline_id = latest["id"]
                await gitlab.cancel_pipeline(project_id=state["repo_id"], pipeline_id=pipeline_id)
                return JSONResponse({"toast": {"type": "success", "content": f"流水线 #{pipeline_id} 取消信号已发送！请等待卡片状态更新"}})
            else:
                return JSONResponse({"toast": {"type": "warning", "content": "当前流水线不在运行状态中，无法取消"}})
        except Exception as e:
            logger.exception("cancel pipeline failed")
            return JSONResponse({"toast": {"type": "error", "content": f"取消流水线失败: {e}"}})

    # ── 历史记录 ─────────────────────────────────────────────────
    if action_name == "history":
        records = await get_history(state["repo_id"])
        history_card = build_history_card(records, state)
        if open_message_id:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, history_card, 0.05))
        return JSONResponse({})

    # ── 项目管理 ─────────────────────────────────────────────────
    if action_name == "manage_projects":
        if not is_admin(cfg, operator_open_id):
            return JSONResponse({"toast": {"type": "error", "content": "权限不足：您不是卡片管理员"}})
            
        all_projects = await get_all_projects()
        dynamic_names = await get_dynamic_project_names()
        pm_card = build_project_management_card(all_projects, dynamic_names, state)
        if open_message_id:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, pm_card, 0.05))
        return JSONResponse({})

    # ── 移除项目 ─────────────────────────────────────────────────
    if action_name == "delete_project":
        if not is_admin(cfg, operator_open_id):
            return JSONResponse({"toast": {"type": "error", "content": "权限不足：您不是卡片管理员"}})
        
        delete_name = raw_value.get("delete_name")
        if not delete_name:
            return JSONResponse({"toast": {"type": "error", "content": "未能获取到要删除的项目名称"}})

        redis = get_redis()
        if redis:
            deleted_count = await redis.hdel(DYNAMIC_PROJECTS_KEY, delete_name)
            if deleted_count > 0:
                PROJECT_DELETED.inc()
            else:
                return JSONResponse({"toast": {"type": "warning", "content": "该项目已不存在或被他人删除"}})
            
        # 刷新项目管理卡片
        all_projects = await get_all_projects()
        dynamic_names = await get_dynamic_project_names()
        pm_card = build_project_management_card(all_projects, dynamic_names, state)
        if open_message_id:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, pm_card, 0.05))
        return JSONResponse({"toast": {"type": "success", "content": f"已成功移除项目 {delete_name}"}})

    # ── 导出动态项目 ─────────────────────────────────────────────────
    if action_name == "export_yaml":
        if not is_admin(cfg, operator_open_id):
            return JSONResponse({"toast": {"type": "error", "content": "权限不足：您不是卡片管理员"}})
            
        dynamic_names = await get_dynamic_project_names()
        all_projects = await get_all_projects()
        
        dyn_projects_data = [p for p in all_projects if p.get("name") in dynamic_names]
        
        if not dyn_projects_data:
            return JSONResponse({"toast": {"type": "info", "content": "当前没有任何动态项目可以导出哦"}})
            
        import yaml
        export_data = {"projects": dyn_projects_data}
        yaml_str = yaml.dump(export_data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        
        msg_content = f"👇 **动态配置已导出**\n你可以将其直接复制并追加到 `config.yaml` 的末尾（注意缩进对齐）：\n\n```yaml\n{yaml_str}```\n\n💡 固化到配置后，记得在管理面板里将动态配置彻底删除哦。"
        
        if open_chat_id:
            asyncio.create_task(feishu_client.send_text(open_chat_id, msg_content))
            
        return JSONResponse({"toast": {"type": "success", "content": "导出成功，已通过聊天发送给你！"}})

    # ── AI Code Review ────────────────────────────────────────────
    if action_name == "review":
        from services.code_review import run_code_review
        AI_REVIEW_TRIGGERED.labels(project=state["project"], repo=state["repo"]).inc()
        asyncio.create_task(run_code_review(feishu_client, gitlab, state, open_chat_id, operator_open_id))
        return JSONResponse({"toast": {"type": "info", "content": "🤖 AI 正在审查代码，结果稍后发送到群聊..."}})

    # ── SonarQube 代码扫描 ─────────────────────────────────────────
    if action_name == "sonar_scan":
        if "⚠️" in state["branch"]:
            return JSONResponse({"toast": {"type": "error", "content": "远程分支读取失败，请重新切一次仓库刷出正常分支后再执行！"}})

        lock_scope = build_sonar_lock_scope(state)
        if not await try_lock_sonar(lock_scope):
            return JSONResponse({"toast": {"type": "warning", "content": "🛡️ 该目标正在执行 Sonar 扫描，请稍候..."}})

        SONAR_SCAN_TRIGGERED.labels(project=state["project"], repo=state["repo"]).inc()
        asyncio.create_task(
            background_run_sonar(feishu_client, gitlab, cfg, state, operator_open_id, open_chat_id, lock_scope)
        )
        return JSONResponse({"toast": {"type": "info", "content": "🛡️ 已触发 SonarQube 代码扫描！结果卡片稍后发出..."}})

    # ── RepoWiki 代码知识库生成 ────────────────────────────────────
    if action_name == "repo_wiki":
        if "⚠️" in state["branch"]:
            return JSONResponse({"toast": {"type": "error", "content": "远程分支读取失败，请重新切一次仓库刷出正常分支后再执行！"}})

        ai_cfg = cfg.get("ai") or {}
        if not ai_cfg.get("api_key") or not ai_cfg.get("model"):
            return JSONResponse({"toast": {"type": "error", "content": "未配置 AI 模型参数，请先在 config.yaml 添加 ai 段"}})

        lock_scope = build_wiki_lock_scope(state, cfg=cfg)
        if not await try_lock_wiki(lock_scope):
            return JSONResponse({"toast": {"type": "warning", "content": "📚 该目标正在生成 RepoWiki，如需中止可点击进度卡上的「🛑 取消生成」"}})

        WIKI_GEN_TRIGGERED.labels(project=state["project"], repo=state["repo"]).inc()
        asyncio.create_task(
            background_run_wiki(feishu_client, gitlab, cfg, state, operator_open_id, open_chat_id, lock_scope)
        )
        return JSONResponse({
            "toast": {
                "type": "info",
                "content": "📚 已启动 RepoWiki 生成！若当前分支有活跃 MR 将写回 MR 源分支协同评审，进度卡片稍后发出...",
            }
        })

    # ── 执行发版 ─────────────────────────────────────────────────
    if action_name == "run":
        if "⚠️" in state["branch"]:
            return JSONResponse({"toast": {"type": "error", "content": "远程分支读取失败，请重新切一次仓库刷出正常分支后再执行！"}})

        if await is_repo_locked(state["repo_id"]):
            logger.warning("Repo %s is locked, run rejected.", state["repo_id"])
            return JSONResponse({"toast": {"type": "error", "content": "该仓正在发布，请稍后"}})

        # ── 审批检查 ────────────────────────────────────────────
        approvers_data = check_approval_required(cfg, state["project"], state["env"], state["repo"])
        if approvers_data:
            APPROVAL_TRIGGERED.labels(project=state["project"], env=state["env"]).inc()
            approval_id = await create_approval(
                feishu_client, gitlab, cfg, state,
                open_message_id, operator_open_id, open_chat_id, approvers_data,
            )
            return JSONResponse({"toast": {"type": "info", "content": f"🔐 已发起审批请求 (#{approval_id})，请等待审批人操作"}})

        # ── 权限检查 ────────────────────────────────────────────
        allowed, reason = check_permission(cfg, state["project"], state["env"], state["repo"], operator_open_id)
        if not allowed:
            logger.warning("permission denied user=%s project=%s env=%s repo=%s reason=%s", operator_open_id, state["project"], state["env"], state["repo"], reason)
            return JSONResponse({"toast": {"type": "error", "content": f"🔒 权限不足：{reason}"}})

        # ── 直接触发 ────────────────────────────────────────────
        if not await try_lock_repo(state["repo_id"]):
            logger.warning("Repo %s locked during try_lock, run rejected.", state["repo_id"])
            return JSONResponse({"toast": {"type": "error", "content": "该仓正在发布，被并发锁定，请稍后"}})

        PIPELINE_TRIGGERED.labels(project=state["project"], repo=state["repo"], env=state["env"]).inc()
        ACTIVE_LOCKS.inc()
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
