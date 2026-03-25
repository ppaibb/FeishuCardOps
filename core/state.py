"""
全局内存状态管理

CARD_STATE:    卡片消息ID -> 用户选择状态（project, repo, branch, env, module 等）
ACTION_DEDUP:  去重键 -> 上次触发时间戳
REPO_LOCKS:    仓库 Project ID -> 是否正在发版（并发锁）
"""
from typing import Any, Dict

CARD_STATE: Dict[str, Dict[str, Any]] = {}
ACTION_DEDUP: Dict[str, float] = {}
REPO_LOCKS: Dict[int, bool] = {}

RUN_DEDUP_SECONDS = 4.0
REFRESH_DEDUP_SECONDS = 1.5


def cleanup_action_dedup(now_ts: float) -> None:
    expired_keys = [k for k, v in ACTION_DEDUP.items() if now_ts - v > 30]
    for k in expired_keys:
        ACTION_DEDUP.pop(k, None)
