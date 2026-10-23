"""Chain dispatch — immediate vs scheduled pushes, cron admission.

Push always lands as a job row; ``CHAIN_DISPATCH`` decides when it runs:
- immediate: every push runs right away (no scheduler started);
- cron:      every push waits as ``scheduled`` until the cron admits it;
- auto:      OptType 0 (manual) pushes run immediately, the rest wait.
"""

from __future__ import annotations

from typing import Any

from clause_splitter.utils import get_logger

logger = get_logger()


def dispatch_mode(manual: bool) -> str:
    """immediate | scheduled — read at call time so tests can patch config."""
    from clause_splitter import config

    mode = (config.CHAIN_DISPATCH or "auto").lower()
    if mode == "immediate":
        return "immediate"
    if mode == "cron":
        return "scheduled"
    return "immediate" if manual else "scheduled"


def start_scheduler(runner: Any) -> Any:
    """Start the cron admission loop; None when nothing needs scheduling."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from clause_splitter import config

    if (config.CHAIN_DISPATCH or "auto").lower() == "immediate":
        return None
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        runner.drain_scheduled,
        CronTrigger.from_crontab(config.CHAIN_CRON),
        id="chain-dispatch",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("chain dispatcher cron armed: %s", config.CHAIN_CRON)
    return scheduler
