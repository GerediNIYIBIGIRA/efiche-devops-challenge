import os
import redis.asyncio as aioredis

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def get_redis():
    return _get_redis()