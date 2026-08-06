"""
OpsPilot Web Router API 单元/集成验证脚本
"""
import asyncio
import httpx
from app import app

async def run_async_tests():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 测试 /healthz
        res1 = await client.get("/healthz")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["ok"] is True
        assert data1["service"] == "FeishuCardOps-Standalone-Web"
        print("[PASS] /healthz ok:", data1)

        # 2. 测试 /api/v1/config
        res2 = await client.get("/api/v1/config")
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["ok"] is True
        assert data2["service"] == "FeishuCardOps-WebConsole"
        assert data2["mode"] == "standalone_web"
        print("[PASS] /api/v1/config ok:", data2)


        # 3. 测试 /api/v1/projects
        res3 = await client.get("/api/v1/projects")
        assert res3.status_code == 200
        data3 = res3.json()
        assert data3["ok"] is True
        print(f"[PASS] /api/v1/projects ok ({len(data3['projects'])} projects)")

        # 4. 测试 /api/v1/history
        res4 = await client.get("/api/v1/history")
        assert res4.status_code == 200
        data4 = res4.json()
        assert data4["ok"] is True
        print("[PASS] /api/v1/history ok")

if __name__ == "__main__":
    asyncio.run(run_async_tests())
    print("\nALL WEB REST API TESTS PASSED!")
