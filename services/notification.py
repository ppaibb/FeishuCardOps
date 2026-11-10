import asyncio
import logging
from typing import Dict, Any

from core.feishu_client import FeishuClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

CURRENT_VERSION = "v1.12.0"

RELEASE_NOTE_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🚀 FeishuCardOps 更新啦 ({CURRENT_VERSION})"}},
    "elements": [
        {
            "tag": "markdown",
            "content": (
                "1. 📚 **AI RepoWiki 代码知识库（零 CI 侵入）**：主控制台新增「📚 生成 Wiki」按钮，也支持在群聊直接发送 `项目/仓库 生成wiki`（加「`强制`」跳过基线重建）。机器人直接拉取 GitLab 快照并生成证据级 Markdown 专题与知识卡片写回 `docs/repowiki/`；遇保护分支自动切分支提交 MR，代码无新提交自动跳过省 Token。\n"
                "2. 🛡️ **SonarQube 代码扫描 & 报告卡片**：主控制台「🛡️ Sonar 扫描」独立加锁不阻塞发版，自动汇总质量门禁、全量指标评级、Top 重点问题及安全热点并支持「收到/排查」闭环确认。\n"
                "3. 👌 **发版确认闭环 & 网页直通**：发版结束自动附带环境访问直连按钮与责任人确认闭环，透出构建参数与日志 AI 诊断。"
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
