import asyncio
import sys
import uvicorn

if sys.platform == "win32":
    # 彻底解决 Windows 下 Python 3.8+ asyncio + httpx 引起的 [WinError 64] 闪退问题
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=55000)
