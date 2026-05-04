"""
FRC Pyro Recharge Service
Run: uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips '*'
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.token_manager import token_manager
from app.api.callback import router as callback_router
from app.config import settings
from app.db.oracle import close_oracle_pool, init_oracle_pool
from app.db.postgres import close_pg_pool, init_pg_pool
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FRC Pyro Recharge Service -- starting up")
    logger.info("  Pyro URL      : %s", settings.pyro_base_url)
    logger.info("  Callback URL  : %s/callback/recharge", settings.callback_base_url)
    logger.info("  Batch pop     : %02d:%02d daily",
                settings.batch_population_hour, settings.batch_population_minute)
    init_pg_pool()
    init_oracle_pool()
    await token_manager.authenticate()
    start_scheduler()
    logger.info("FRC Pyro Recharge Service -- ready")
    yield
    logger.info("FRC Pyro Recharge Service -- shutting down")
    stop_scheduler()
    close_oracle_pool()
    close_pg_pool()
    logger.info("Shutdown complete")


app = FastAPI(
    title="FRC Pyro Recharge Service",
    version="1.0.0",
    root_path="/smpyro",
    lifespan=lifespan,
)

app.include_router(callback_router)


@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}


@app.get("/token-status", tags=["Ops"])
async def token_status():
    from datetime import datetime, timezone
    tm  = token_manager
    exp = tm._access_token_exp
    now = datetime.now(timezone.utc).timestamp()
    return {
        "session_token_present": tm.session_token is not None,
        "access_token_present":  tm.access_token  is not None,
        "access_expires_in_s":   max(0, round(exp - now)) if exp else None,
    }


@app.post("/admin/trigger-batch-population", tags=["Admin"])
async def trigger_batch_population():
    """Manually run Oracle BCD -> Postgres population."""
    import asyncio
    from app.batch.populator import run_batch_population
    summary = await asyncio.to_thread(run_batch_population)
    return {"triggered": True, "summary": summary}


@app.post("/admin/trigger-recharge", tags=["Admin"])
async def trigger_recharge():
    """Manually trigger recharge dispatch."""
    from app.recharge.processor import process_pending_recharges
    summary = await process_pending_recharges(batch_size=settings.recharge_batch_size)
    return {"triggered": True, "summary": summary}


@app.post("/admin/trigger-status-check", tags=["Admin"])
async def trigger_status_check():
    """Manually trigger status check fallback."""
    from app.status_checker import run_status_checks
    summary = await run_status_checks()
    return {"triggered": True, "summary": summary}