import asyncio
import logging
from datetime import datetime, timezone

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
        logger.error("Scheduler: recharge exception: %s", exc, exc_info=True)


async def _status_check_job():
    try:
        from app.status_checker import run_status_checks
        summary = await run_status_checks()
        logger.info("Scheduler: status check done -- %s", summary)
    except Exception as exc:
        logger.error("Scheduler: status check exception: %s", exc)


def start_scheduler():
    now = datetime.now(timezone.utc)   

    # ── Daily re-auth (CronTrigger — must fire at specific wall-clock time) ────
    scheduler.add_job(
        _daily_auth_job,
        CronTrigger(hour=0, minute=5),
        id="daily_auth",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        _batch_population_job,
        IntervalTrigger(hours=1),
        id="batch_population",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now,      
    )

   
    scheduler.add_job(
        _recharge_job,
        IntervalTrigger(minutes=30),
        id="recharge_batch",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=now,      # pick up any rows left over from before restart
    )

    # ── Status check (every 5 min) ─────────────────────────────────────────────
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
        "batch_pop: every 1hr (immediate on startup) | "
        "recharge: every 30min (immediate on startup) | "
        "status_check: every 5min | "
        "daily_auth: 00:05"
    )


def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped")