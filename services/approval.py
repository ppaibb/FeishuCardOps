"""
审批流服务 (Redis 持久化)

创建审批请求，并把状态存在 Redis 中 (7 天失效)。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from core.card_builder import build_approval_card
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")


async def create_approval(feishu_client: FeishuClient, gitlab: Any, cfg: Dict[str, Any], state: Dict[str, Any],
                          open_message_id: str, operator_open_id: str, open_chat_id: str, approvers: list) -> str:
    approval_id = str(uuid.uuid4())[:8]
    record = {
        "approval_id": approval_id, "state": dict(state), "operator_open_id": operator_open_id,
        "open_message_id": open_message_id, "open_chat_id": open_chat_id, "approvers": approvers, "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "resolved_by": None, "resolved_at": None,
        "approval_message_id": None,
    }
    
    approval_card = build_approval_card(state, operator_open_id, approval_id, approvers=approvers)
    
    # 优先将审批请求发送给公共审计群组，避免私聊发版时审批人无法收到通知
    audit_chat_id = cfg.get("feishu", {}).get("audit_chat_id")
    target_chat_id = audit_chat_id if audit_chat_id else open_chat_id
    
    try:
        resp = await feishu_client.send_card(target_chat_id, approval_card)
        record["approval_message_id"] = resp.get("data", {}).get("message_id")
        
        # 如果发到了群里但触发点是私聊，给触发人发送一条文字提示
        if audit_chat_id and audit_chat_id != open_chat_id:
            await feishu_client.send_text(open_chat_id, f"📝 已将发版审批请求（#{approval_id}）发送至发版群，请提醒对应审批人处理。")
    except Exception as e:
        logger.error("failed to send approval card: %s", e)

    r = get_redis()
    await r.setex(f"approval:{approval_id}", 7 * 24 * 3600, json.dumps(record, ensure_ascii=False))
    return approval_id


async def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    r = get_redis()
    data = await r.get(f"approval:{approval_id}")
    return json.loads(str(data)) if data else None


async def resolve_approval(approval_id: str, action: str, resolver_open_id: str) -> Dict[str, Any]:
    record = await get_approval(approval_id)
    if not record: return {"ok": False, "msg": "审批记录不存在或已过期"}
    if record["status"] != "pending": return {"ok": False, "msg": f"该审批已处理（{record['status']}）"}
    if resolver_open_id not in record["approvers"]: return {"ok": False, "msg": "您不是授权审批人"}

    record["resolved_by"] = resolver_open_id
    record["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cfg = load_config()
    feishu_client = FeishuClient(app_id=cfg["feishu"]["app_id"], app_secret=cfg["feishu"]["app_secret"])
    gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])
    state, open_message_id, operator_open_id, open_chat_id = record["state"], record["open_message_id"], record["operator_open_id"], record["open_chat_id"]

    if action == "approve":
        from core.state import try_lock_repo
        if not await try_lock_repo(state["repo_id"]):
            return {"ok": False, "msg": "该仓正在发布中，无法进行批准"}

        record["status"] = "approved"
        from services.pipeline import background_run_pipeline, delayed_update_card, next_card_version
        from core.metrics import PIPELINE_TRIGGERED, ACTIVE_LOCKS
        from core.card_builder import build_card, build_approval_card
        
        PIPELINE_TRIGGERED.labels(project=state.get("project", ""), repo=state.get("repo", ""), env=state.get("env", "")).inc()
        ACTIVE_LOCKS.inc()
        asyncio.create_task(background_run_pipeline(feishu_client, gitlab, cfg, state, open_message_id, operator_open_id, open_chat_id))
        
        if open_message_id:
            card = build_card(cfg, status="执行中", state=state, latest_pipeline_text="初始化中...", latest_result_text="已接收审批发版指令正在投递...", show_details=True, is_locked=True)
            ver = next_card_version(open_message_id)
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, card, 1.0, version=ver))
            
        try: await feishu_client.send_text(open_chat_id, f"✅ 审批已通过！正在触发 [{state['project']}/{state['repo']}]...")
        except Exception: pass
        msg = "已批准，流水线即将触发"
    else:
        from core.card_builder import build_approval_card
        record["status"] = "rejected"
        try: await feishu_client.send_text(open_chat_id, f"❌ 发版审批已驳回 [{state['project']}/{state['repo']}]")
        except Exception: pass
        msg = "已驳回"

    # 更新审批卡片为已处理状态
    try:
        if record.get("approval_message_id"):
            updated_approval_card = build_approval_card(
                state=state,
                requester_open_id=operator_open_id,
                approval_id=approval_id,
                approvers=record.get("approvers", []),
                resolved_by=resolver_open_id,
                status=record["status"]
            )
            await feishu_client.update_card(record["approval_message_id"], updated_approval_card)
    except Exception as e:
        logger.error("failed to update approval card: %s", e)

    r = get_redis()
    await r.setex(f"approval:{approval_id}", 7 * 24 * 3600, json.dumps(record, ensure_ascii=False))
    return {"ok": True, "msg": msg}
