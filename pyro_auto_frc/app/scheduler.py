"""
app/scheduler.py
----------------
Four APScheduler jobs:
  1. daily_auth       -- 00:05 daily (CronTrigger — clock time matters)
  2. batch_population -- every 1 hour
  3. recharge_batch   -- every 30 min after previous run completes
  4. status_check     -- every 5 min after previous run completes
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.auth.token_manager import token_manager
from app.config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def _daily_auth_job():
    logger.info("Scheduler: daily re-auth")
    try:
        ok = await token_manager.authenticate()
        logger.info("Scheduler: daily re-auth %s", "OK" if ok else "FAILED")
    except Exception as exc:
        logger.error("Scheduler: daily re-auth exception: %s", exc)


async def _batch_population_job():
    logger.info("Scheduler: batch population starting")
    try:
        from app.batch.populator import run_batch_population
        summary = await asyncio.to_thread(run_batch_population)
        logger.info("Scheduler: batch population done -- %s", summary)
    except Exception as exc:
        logger.error("Scheduler: batch population exception: %s", exc)


async def _recharge_job():
    logger.info("Scheduler: recharge batch starting")
    try:
        from app.processor import process_pending_recharges
        summary = await process_pending_recharges(
            batch_size=settings.recharge_batch_size
        )
        logger.info("Scheduler: recharge done -- %s", summary)
    except Exception as exc:
        logger.error("Scheduler: recharge exception: %s", exc)


async def _status_check_job():
    try:
        from app.status_checker import run_status_checks
        summary = await run_status_checks()
        logger.info("Scheduler: status check done -- %s", summary)
    except Exception as exc:
        logger.error("Scheduler: status check exception: %s", exc)


def start_scheduler():
    # ── Daily re-auth (CronTrigger — must run at specific clock time) ──────────
    scheduler.add_job(
        _daily_auth_job,
        CronTrigger(hour=0, minute=5),
        id="daily_auth",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Batch population (every 1 hour from app start) ─────────────────────────
    scheduler.add_job(
        _batch_population_job,
        IntervalTrigger(hours=1),
        id="batch_population",
        replace_existing=True,
        max_instances=1,        # never overlap — two batch runs at once would
        coalesce=True,          # duplicate Oracle fetches and Postgres inserts
        misfire_grace_time=120,
        next_run_time=datetime.now(),   # run immediately on startup
    )

    # ── Recharge pusher (30 min after previous run completes) ──────────────────
    scheduler.add_job(
        _recharge_job,
        IntervalTrigger(minutes=30),
        id="recharge_batch",
        replace_existing=True,
        max_instances=1,        # never overlap — same rows could be double-pushed
        coalesce=True,
        misfire_grace_time=60,
    )

    # ── Status check (5 min after previous run completes) ──────────────────────
    scheduler.add_job(
        _status_check_job,
        IntervalTrigger(minutes=5),
        id="status_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    scheduler.start()
    logger.info(
        "Scheduler started -- "
        "batch_pop: every 1hr (immediate) | "
        "recharge: every 30min | "
        "status: every 5min | "
        "auth: 00:05 daily"
    )


def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped")