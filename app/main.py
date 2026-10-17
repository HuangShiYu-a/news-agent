"""FastAPI 入口：启动时幂等建表并清扫遗留任务（阶段一最小版）。

阶段二接入业务路由与静态页面，阶段三接入定时调度；
均只扩展本模块，不改变 init_db 只在启动时执行一次的约定。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI 新闻简报 Agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}
