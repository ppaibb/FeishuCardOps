import asyncio
import logging
from typing import Dict, Any

from core.feishu_client import FeishuClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

CURRENT_VERSION = "v1.13.0"

RELEASE_NOTE_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🚀 FeishuCardOps 更新啦 ({CURRENT_VERSION})"}},
    "elements": [
        {
            "tag": "markdown",
            "content": (
                "1. 📚 **RepoWiki 深度扩容 & MR 协同写回**：支持按仓库模块规模自适应规划专题数量（默认不设死板 4 篇上限，安全上限 24 篇并支持 3 路并发撰写），智能识别当前分支活跃 MR 并直接写回源分支随代码评审合入；卡片实时展示耗时并支持一键 **🛑 取消生成**。\n"
                "2. 🛡️ **局部降级保护 & 断点续跑**：针对思考链模型输出配额与限流/超时波动全面加固，个别专题或卡片重试仍失败时自动生成占位页保底交付、其余专题正常写回；重新生成可自动复用健康断点缓存仅补全异常页。\n"
                "3. 🔐 **RepoWiki 管理员权限管控 & 常用能力**：Wiki 触发、取消及群聊文本指令均已对齐项目管理员权限校验；同时保留 **🛡️ Sonar 代码扫描**、**👌 发版确认闭环** 与 **🌐 应用直达** 能力。"
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
