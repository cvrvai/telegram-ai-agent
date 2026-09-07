"""Background scheduler for automated periodic digests."""

from __future__ import annotations

import logging
from typing import Callable, Awaitable, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from config import AppConfig, config

logger = logging.getLogger("scheduler")


class DigestScheduler:
    """Manages recurring execution of the digest generator."""

    def __init__(self, callback: Callable[[], Awaitable[None]], cfg: Optional[AppConfig] = None):
        self.callback = callback
        self.cfg = cfg or config
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        """Schedules digest jobs based on configuration."""
        # 1. Specific scheduled times (e.g. 08:00, 13:00, 19:00, 22:00)
        times = self.cfg.schedule_times_list
        if times:
            for t_str in times:
                try:
                    parts = t_str.split(":")
                    hour = int(parts[0])
                    minute = int(parts[1]) if len(parts) > 1 else 0
                    trigger = CronTrigger(hour=hour, minute=minute)
                    self.scheduler.add_job(
                        self.callback,
                        trigger=trigger,
                        id=f"digest_cron_{hour:02d}_{minute:02d}",
                        replace_existing=True,
                    )
                    logger.info(f"Scheduled daily digest at {hour:02d}:{minute:02d}")
                except Exception as e:
                    logger.error(f"Invalid digest time format '{t_str}': {e}")

        # 2. Or fallback to fixed interval if no specific times configured
        if not times and self.cfg.digest_interval_hours > 0:
            trigger = IntervalTrigger(hours=self.cfg.digest_interval_hours)
            self.scheduler.add_job(
                self.callback,
                trigger=trigger,
                id="digest_interval",
                replace_existing=True,
            )
            logger.info(f"Scheduled periodic digest every {self.cfg.digest_interval_hours} hours")

        self.scheduler.start()
        logger.info("Digest scheduler started.")

    def stop(self) -> None:
        """Stops the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Digest scheduler stopped.")


