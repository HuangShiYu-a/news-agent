"""FastAPI 入口：启动幂等建表、清扫遗留任务并启动定时调度。

注意：静态目录挂载必须放在最后，否则会吞掉 /api 与 /health 路由。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config, scheduler
from app.api.routes import router as api_router
from app.db import init_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start_scheduler()
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="AI 新闻简报 Agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(api_router)
app.mount("/", StaticFiles(directory=config.BASE_DIR / "web", html=True),
          name="web")
