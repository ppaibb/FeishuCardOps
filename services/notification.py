import asyncio
import logging
from typing import Dict, Any

from core.feishu_client import FeishuClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

CURRENT_VERSION = "v1.9.0"

RELEASE_NOTE_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🚀 FeishuCardOps 更新啦 ({CURRENT_VERSION})"}},
    "elements": [
        {
            "tag": "markdown",
            "content": "1. 🎛️ **构建参数全面透出**：发版进度指示卡片（初始化/运行中/成功/异常）现已支持完整展示本次发版所选的自定义变量与微服务模块（如 `Module`、环境参数等），发版细节一目了然！\n2. 🌐 **流水线部署直通车**：流水线发版成功后，卡片将自动呈现对应的应用访问地址，并在底部提供 **`🌐 一键打开网页`** 直通按钮。\n3. 💡 **多环境适配提示**：支持按环境配置提示信息（如默认测试账号、回归注意项等），在发版成功卡片中贴心展示。"
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
