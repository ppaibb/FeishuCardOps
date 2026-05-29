"""
FeishuCardOps — 飞书卡片驱动的 GitLab CI/CD 智能发版控制台

入口文件：注册路由、健康检查
启动命令：uvicorn app:app --host 0.0.0.0 --port 55000
"""
import logging

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from core.config import load_config
from services.project_manager import get_all_projects
from routes.card import router as card_router
from routes.event import router as event_router

# 确保指标模块在应用启动时初始化
import core.metrics  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return not (record.args and len(record.args) >= 3 and record.args[2] == "/healthz")

logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="feishu-gitlab-card-http")

app.include_router(event_router)
app.include_router(card_router)

# 挂载 Prometheus /metrics 端点
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/")
async def health():
    cfg = load_config()
    all_projects = await get_all_projects()
    return {
        "ok": True,
        "service": "feishu-gitlab-card-http",
        "projects": [p.get("name") for p in all_projects],
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}
