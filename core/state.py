"""
基于 Redis 的状态管理

用 Redis 替代原来的字典全局变量。
"""
import json
import logging
from typing import Any, Dict, Optional

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


async def try_lock_repo(repo_id: int) -> bool:
    if not repo_id:
        return False
    r = get_redis()
    success = await r.set(f"repo_lock:{repo_id}", "1", nx=True, ex=3600)
    if success:
        logger.info(f"try_lock_repo succeeded repo_id={repo_id}")
    return bool(success)


async def unlock_repo(repo_id: int) -> None:
    if not repo_id:
        return
    r = get_redis()
    logger.info(f"unlocking repo_id={repo_id}")
    await r.delete(f"repo_lock:{repo_id}")


async def try_lock_sonar(lock_scope: str, ttl: int = 1800) -> bool:
    """尝试获取独立的 Sonar 扫描并发锁（与发版锁 repo_lock 完全隔离）"""
    if not lock_scope:
        return False
    r = get_redis()
    success = await r.set(f"sonar_lock:{lock_scope}", "1", nx=True, ex=ttl)
    if success:
        logger.info("try_lock_sonar succeeded scope=%s", lock_scope)
    return bool(success)


async def unlock_sonar(lock_scope: str) -> None:
    if not lock_scope:
        return
    r = get_redis()
    logger.info("unlocking sonar scope=%s", lock_scope)
    await r.delete(f"sonar_lock:{lock_scope}")


async def try_lock_wiki(lock_scope: str, ttl: int = 1800) -> bool:
    """尝试获取独立的 RepoWiki 生成并发锁（与发版锁、Sonar 锁完全隔离）"""
    if not lock_scope:
        return False
    r = get_redis()
    success = await r.set(f"wiki_lock:{lock_scope}", "1", nx=True, ex=ttl)
    if success:
        logger.info("try_lock_wiki succeeded scope=%s", lock_scope)
    return bool(success)


async def unlock_wiki(lock_scope: str) -> None:
    if not lock_scope:
        return
    r = get_redis()
    logger.info("unlocking wiki scope=%s", lock_scope)
    await r.delete(f"wiki_lock:{lock_scope}")


async def is_wiki_locked(lock_scope: str) -> bool:
    if not lock_scope:
        return False
    r = get_redis()
    if not r:
        return False
    try:
        return bool(await r.get(f"wiki_lock:{lock_scope}"))
    except Exception as e:
        logger.warning("is_wiki_locked failed scope=%s err=%s", lock_scope, e)
        return False


async def request_wiki_cancel(lock_scope: str, info: Optional[Dict[str, Any]] = None, ttl: int = 300) -> None:
    if not lock_scope:
        return
    r = get_redis()
    if not r:
        return
    payload = {"canceled": True, **(info or {})}
    try:
        await r.setex(f"wiki_cancel:{lock_scope}", ttl, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("request_wiki_cancel failed scope=%s err=%s", lock_scope, e)


async def get_wiki_cancel_info(lock_scope: str) -> Optional[Dict[str, Any]]:
    if not lock_scope:
        return None
    r = get_redis()
    if not r:
        return None
    try:
        raw = await r.get(f"wiki_cancel:{lock_scope}")
        if not raw:
            return None
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
        if isinstance(parsed, dict) and parsed.get("canceled") is True:
            return parsed
    except Exception as e:
        logger.warning("get_wiki_cancel_info failed scope=%s err=%s", lock_scope, e)
    return None


async def clear_wiki_cancel(lock_scope: str) -> None:
    if not lock_scope:
        return
    r = get_redis()
    if not r:
        return
    try:
        await r.delete(f"wiki_cancel:{lock_scope}")
    except Exception as e:
        logger.warning("clear_wiki_cancel failed scope=%s err=%s", lock_scope, e)
