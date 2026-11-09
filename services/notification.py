import asyncio
import logging
from typing import Dict, Any

from core.feishu_client import FeishuClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

CURRENT_VERSION = "v1.11.0"

RELEASE_NOTE_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🚀 FeishuCardOps 更新啦 ({CURRENT_VERSION})"}},
    "elements": [
        {
            "tag": "markdown",
            "content": (
                "1. 🛡️ **SonarQube 代码扫描 & 报告卡片**：主控制台新增「🛡️ Sonar 扫描」按钮，扫描独立加锁不阻塞正常发版！扫描完成后自动汇总质量门禁明细、全量代码指标评级、Top 重点问题、安全热点及报告直达链接，门禁未通过或含高危问题时自动呈现「收到/排查」闭环确认。\n"
                "2. 👌 **发版确认闭环（收到/已阅）**：发版结束后卡片底部提供责任人专属确认按钮，杜绝替点并同步沉淀知晓记录。\n"
                "3. 🎛️ **构建参数与网页直通**：进度卡片全面透出所选微服务与自定义变量，发版成功后自动附带应用直连按钮与环境提示。"
            ),
        }
    ],
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
