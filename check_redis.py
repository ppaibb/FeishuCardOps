import asyncio
from core.redis_client import get_redis

async def main():
    r = get_redis()
    data = await r.get('approval:c0308567')
    print("Redis data:", data)

if __name__ == "__main__":
    asyncio.run(main())
