"""
基于 Redis 的状态管理

用 Redis 替代原来的字典全局变量。
"""
import json
import logging
from typing import Any, Dict

from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

RUN_DEDUP_SECONDS = 4
REFRESH_DEDUP_SECONDS = 2


async def get_card_state(open_message_id: str) -> Dict[str, Any]:
    if not open_message_id:
        return {}
    r = get_redis()
    data = await r.get(f"card_state:{open_message_id}")
    if data:
        try:
            return json.loads(str(data))
        except Exception:
            pass
    return {}


async def save_card_state(open_message_id: str, state: Dict[str, Any]) -> None:
    if not open_message_id:
        return
    r = get_redis()
    await r.setex(f"card_state:{open_message_id}", 7 * 24 * 3600, json.dumps(state, ensure_ascii=False))


async def check_action_dedup(dedup_key: str, window_seconds: int) -> bool:
    if not dedup_key:
        return True
    r = get_redis()
    result = await r.set(f"action_dedup:{dedup_key}", "1", nx=True, ex=window_seconds)
    return bool(result)


async def is_repo_locked(repo_id: int) -> bool:
    if not repo_id:
        return False
    r = get_redis()
    return bool(await r.get(f"repo_lock:{repo_id}"))


async def lock_repo(repo_id: int) -> None:
    if not repo_id:
        return
    r = get_redis()
    logger.info(f"locking repo_id={repo_id}")
    await r.setex(f"repo_lock:{repo_id}", 3600, "1")


async def unlock_repo(repo_id: int) -> None:
    if not repo_id:
        return
    r = get_redis()
    logger.info(f"unlocking repo_id={repo_id}")
    await r.delete(f"repo_lock:{repo_id}")
