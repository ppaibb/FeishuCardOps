import asyncio
import logging
from typing import Dict, Any

from core.feishu_client import FeishuClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

CURRENT_VERSION = "v1.6.0"

RELEASE_NOTE_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🚀 FeishuCardOps 更新啦 ({CURRENT_VERSION})"}},
    "elements": [
        {
            "tag": "markdown",
            "content": "1. 🛑 **新增停止发版功能**：排队/运行中可随时手动停止。\n2. 🤖 **自然语言一键发版**：在群里对机器人说“项目/仓库/环境”，AI为你预填所有参数！\n3. 🧹 **底层日志优化**：重构请求拦截，大幅增强日志可观测性。\n4. 📢 **静默更新触达**：现在你只会看一次这个弹窗，不用全群广播打扰别人啦！"
        }
    ]
}

async def check_and_send_release_note(cfg: Dict[str, Any], open_id: str):
    """
    检查用户是否已读最新版本日志，如果未读则通过飞书私聊发送，并记录已读。
    """
    if not open_id:
        return
        
    redis = get_redis()
    if not redis:
        return
        
    cache_key = f"cardops:seen_version:{open_id}"
    try:
        seen = await redis.get(cache_key)
        if seen:
            seen_str = seen.decode("utf-8") if isinstance(seen, bytes) else str(seen)
            if seen_str == CURRENT_VERSION:
                return
                
        # 没有看过，发送推送给该用户私信
        feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
        await feishu.send_card(open_id, RELEASE_NOTE_CARD, receive_id_type="open_id")
        
        # 标记为已读
        await redis.set(cache_key, CURRENT_VERSION)
        logger.info(f"Release note {CURRENT_VERSION} sent to open_id={open_id}")
    except Exception as e:
        logger.error(f"Failed to check/send release note to {open_id}: {e}")
