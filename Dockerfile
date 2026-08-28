FROM docker.1panel.live/library/python:3.10-slim

LABEL maintainer="DevOps Team"
LABEL description="飞书 GitLab 智能发版控制台"

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

# 拷贝应用代码
COPY app.py .
COPY run.py .
COPY core/ core/
COPY routes/ routes/
COPY services/ services/

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:55000/healthz', timeout=3); assert r.status_code == 200"

EXPOSE 55000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "55000", "--log-level", "info"]
