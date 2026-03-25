"""
审批流服务

当生产环境发版需要审批时，创建审批请求，
审批人通过卡片操作批准或驳回。
"""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from core.card_builder import build_approval_card
from core.feishu_client import FeishuClient

logger = logging.getLogger("feishu_gitlab_card_http")

# approval_id -> ApprovalRecord
_approval_store: Dict[str, Dict[str, Any]] = {}


async def create_approval(
    feishu_client: FeishuClient,
    gitlab_client: Any,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    open_message_id: str,
    operator_open_id: str,
    open_chat_id: str,
    approvers: list,
) -> str:
    """
    创建审批请求，向审批人发送审批卡片。

    Returns:
        approval_id
    """
    approval_id = str(uuid.uuid4())[:8]

    record = {
        "approval_id": approval_id,
        "state": dict(state),
        "operator_open_id": operator_open_id,
        "open_message_id": open_message_id,
        "open_chat_id": open_chat_id,
        "approvers": approvers,
        "status": "pending",  # pending / approved / rejected
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resolved_by": None,
        "resolved_at": None,
        # 保存 client 引用用于后续自动触发
        "_feishu_client": feishu_client,
        "_gitlab_client": gitlab_client,
        "_cfg": cfg,
    }
    _approval_store[approval_id] = record

    # 在群聊中发送审批卡片
    approval_card = build_approval_card(state, operator_open_id, approval_id)
    try:
        resp = await feishu_client.send_card(open_chat_id, approval_card)
        record["approval_message_id"] = resp.get("data", {}).get("message_id")
        logger.info("approval card sent approval_id=%s chat_id=%s", approval_id, open_chat_id)
    except Exception as e:
        logger.error("failed to send approval card: %s", e)

    return approval_id


def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    """获取审批记录"""
    return _approval_store.get(approval_id)


async def resolve_approval(
    approval_id: str,
    action: str,  # "approve" or "reject"
    resolver_open_id: str,
) -> Dict[str, Any]:
    """
    处理审批决定。

    Returns:
        {"ok": bool, "msg": str}
    """
    record = _approval_store.get(approval_id)
    if not record:
        return {"ok": False, "msg": "审批记录不存在或已过期"}

    if record["status"] != "pending":
        return {"ok": False, "msg": f"该审批已处理（{record['status']}）"}

    # 检查是否是授权的审批人
    if resolver_open_id not in record["approvers"]:
        return {"ok": False, "msg": "您不是该审批的授权审批人"}

    record["resolved_by"] = resolver_open_id
    record["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if action == "approve":
        record["status"] = "approved"
        logger.info("approval approved id=%s by=%s", approval_id, resolver_open_id)

        # 自动触发流水线
        feishu_client = record["_feishu_client"]
        gitlab_client = record["_gitlab_client"]
        cfg = record["_cfg"]
        state = record["state"]
        open_message_id = record["open_message_id"]
        operator_open_id = record["operator_open_id"]
        open_chat_id = record["open_chat_id"]

        # 延迟导入避免循环依赖
        from services.pipeline import background_run_pipeline
        asyncio.create_task(
            background_run_pipeline(
                feishu_client, gitlab_client, cfg, state,
                open_message_id, operator_open_id, open_chat_id,
            )
        )

        # 通知群组
        try:
            await feishu_client.send_text(
                open_chat_id,
                f"✅ 审批已通过！正在触发 [{state['project']}/{state['repo']}] → {state['env']} 的发版流水线..."
            )
        except Exception as e:
            logger.error("failed to send approval notification: %s", e)

        return {"ok": True, "msg": "已批准，流水线即将触发"}

    else:  # reject
        record["status"] = "rejected"
        logger.info("approval rejected id=%s by=%s", approval_id, resolver_open_id)

        feishu_client = record["_feishu_client"]
        open_chat_id = record["open_chat_id"]
        state = record["state"]

        try:
            await feishu_client.send_text(
                open_chat_id,
                f"❌ 发版审批已驳回 [{state['project']}/{state['repo']}] → {state['env']}"
            )
        except Exception as e:
            logger.error("failed to send rejection notification: %s", e)

        return {"ok": True, "msg": "已驳回"}
