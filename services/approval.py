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
    
    approval_card = build_approval_card(state, operator_open_id, approval_id)
    try:
        resp = await feishu_client.send_card(open_chat_id, approval_card)
        record["approval_message_id"] = resp.get("data", {}).get("message_id")
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
        record["status"] = "approved"
        from services.pipeline import background_run_pipeline
        asyncio.create_task(background_run_pipeline(feishu_client, gitlab, cfg, state, open_message_id, operator_open_id, open_chat_id))
        try: await feishu_client.send_text(open_chat_id, f"✅ 审批已通过！正在触发 [{state['project']}/{state['repo']}]...")
        except Exception: pass
        msg = "已批准，流水线即将触发"
    else:
        record["status"] = "rejected"
        try: await feishu_client.send_text(open_chat_id, f"❌ 发版审批已驳回 [{state['project']}/{state['repo']}]")
        except Exception: pass
        msg = "已驳回"

    r = get_redis()
    await r.setex(f"approval:{approval_id}", 7 * 24 * 3600, json.dumps(record, ensure_ascii=False))
    return {"ok": True, "msg": msg}
