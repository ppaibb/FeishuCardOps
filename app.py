"""
FeishuCardOps — 飞书卡片驱动的 GitLab CI/CD 智能发版控制台

入口文件：注册路由、健康检查
启动命令：uvicorn app:app --host 0.0.0.0 --port 55000
"""
import os
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app

from core.config import load_config
from services.project_manager import get_all_projects
from routes.card import router as card_router
from routes.event import router as event_router
from routes.web import router as web_router

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

app = FastAPI(title="FeishuCardOps-Web-and-Card-Engine")

# 注册独立 Web 控制台 API 与飞书回调路由
app.include_router(web_router)
app.include_router(event_router)
app.include_router(card_router)

# 挂载 Prometheus /metrics 端点
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "FeishuCardOps-Standalone-Web"}



# 静态文件挂载：如果 static 目录存在，挂载至根路径，提供 Vercel 极简暗黑 Web 界面
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

