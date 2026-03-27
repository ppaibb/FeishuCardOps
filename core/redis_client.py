import logging
from typing import Optional

from redis.asyncio import ConnectionPool, Redis

from core.config import load_config

logger = logging.getLogger("feishu_gitlab_card_http")

_redis_client: Optional[Redis] = None


def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        cfg = load_config()
        redis_url = cfg.get("redis", {}).get("url", "redis://localhost:6379/0")
        pool = ConnectionPool.from_url(redis_url, decode_responses=True)
        _redis_client = Redis(connection_pool=pool)
        logger.info(f"Initialized async Redis client: {redis_url}")
    return _redis_client
