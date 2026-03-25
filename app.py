"""
FeishuCardOps — 飞书卡片驱动的 GitLab CI/CD 智能发版控制台

入口文件：注册路由、健康检查
启动命令：uvicorn app:app --host 0.0.0.0 --port 55000
"""
import logging

from fastapi import FastAPI

from core.config import load_config
from routes.card import router as card_router
from routes.event import router as event_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="feishu-gitlab-card-http")

app.include_router(event_router)
app.include_router(card_router)


@app.get("/")
async def health():
    cfg = load_config()
    return {
        "ok": True,
        "service": "feishu-gitlab-card-http",
        "projects": [p.get("name") for p in cfg.get("projects", [])],
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}
