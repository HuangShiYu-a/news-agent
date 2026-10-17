"""FastAPI 入口：启动幂等建表并清扫遗留任务；挂载 API 路由与静态页面。

注意：静态目录挂载必须放在最后，否则会吞掉 /api 与 /health 路由。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.api.routes import router as api_router
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI 新闻简报 Agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(api_router)
app.mount("/", StaticFiles(directory=config.BASE_DIR / "web", html=True),
          name="web")
