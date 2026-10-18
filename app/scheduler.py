"""每日定时调度（design.md §8 / §10.4 / D2）。

APScheduler BackgroundScheduler（线程型），显式 Asia/Shanghai 时区；
job 设 max_instances=1、coalesce=True、misfire_grace_time=300s。
触发时走与手动生成完全相同的业务入口 briefing.start_generate()：
1. 防重：今日（Asia/Shanghai）已有成功的 scheduled run 则跳过（§8）；
2. 忙（BusyError，有任务在执行）则记日志跳过，不补跑；
3. scheduled 任务的 is_read=0 即站内未读通知（§8 / D16），页面轮询可见。
应用未运行时到点不补跑（demo 承诺边界，README 如实说明）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config
from app.db import get_conn
from app.services import briefing

logger = logging.getLogger("news-agent.scheduler")

_scheduler: BackgroundScheduler | None = None


def _today() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def has_scheduled_success_today() -> bool:
    """§8 防重：当日是否已有成功的定时任务（单用户，无用户维度）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE trigger_type = 'scheduled' "
            "AND status = 'succeeded' AND brief_date = ? LIMIT 1",
            (_today(),)).fetchone()
    return row is not None


def run_scheduled_briefing() -> None:
    """定时任务入口（job 函数）：防重检查 + 同一业务入口。"""
    if has_scheduled_success_today():
        logger.info("今日已有成功的定时简报，跳过触发")
        return
    try:
        run_id = briefing.start_generate("scheduled")
        logger.info("定时简报任务 #%s 已启动", run_id)
    except briefing.BusyError as e:
        logger.warning("定时触发时已有任务在执行中，本次跳过（不补跑）：%s", e)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    hh, mm = config.settings.daily_run_at.split(":")
    sched = BackgroundScheduler(timezone=config.TIMEZONE)
    sched.add_job(
        run_scheduled_briefing,
        CronTrigger(hour=int(hh), minute=int(mm), timezone=config.TIMEZONE),
        id="daily-briefing",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sched.start()
    _scheduler = sched
    logger.info("定时调度已启动：每日 %s（%s）生成简报",
                config.settings.daily_run_at, config.TIMEZONE)
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度已停止")
