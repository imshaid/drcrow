"""
Scheduler — APScheduler jobs.
Daily exam alert at 08:00 BST, weekly digest Sundays 10:00 BST,
streak reset at midnight.
"""

import logging
import asyncio
import json
from datetime import date, datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application
from telegram.constants import ParseMode
from config.settings import settings
from database import queries

logger = logging.getLogger(__name__)


def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone="Asia/Dhaka")

    scheduler.add_job(
        _daily_exam_alert,
        CronTrigger(hour=8, minute=0),
        args=[app],
        id="daily_exam_alert"
    )
    scheduler.add_job(
        _weekly_digest,
        CronTrigger(day_of_week="sun", hour=10, minute=0),
        args=[app],
        id="weekly_digest"
    )
    scheduler.add_job(
        _midnight_streak_reset,
        CronTrigger(hour=0, minute=1),
        args=[app],
        id="streak_reset"
    )
    scheduler.start()
    logger.info("Scheduler started with 3 jobs.")


async def _daily_exam_alert(app: Application):
    """Daily 08:00 trigger — sends morning exam notifications."""
    try:
        from handlers.exam import run_exam_scheduler
        await run_exam_scheduler(app.bot)
        logger.info("Exam scheduler run complete (08:00 trigger).")
    except Exception as e:
        logger.error(f"Exam scheduler (08:00) failed: {e}")


async def _weekly_digest(app: Application):
    """Disabled — weekly digest removed."""
    pass


async def _midnight_streak_reset(app: Application):
    """Reset warned_today flag and update streaks."""
    try:
        await queries.reset_warned_today()
        # Streak update logic
        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Increment streak for users active today
            await conn.execute("""
                UPDATE users
                SET streak_days = streak_days + 1
                WHERE is_member = TRUE
                  AND last_active >= NOW() - INTERVAL '25 hours'
            """)
            # Reset streak for users inactive
            await conn.execute("""
                UPDATE users
                SET streak_days = 0
                WHERE is_member = TRUE
                  AND last_active < NOW() - INTERVAL '25 hours'
            """)

        logger.info("Streak reset complete.")
    except Exception as e:
        logger.error(f"Streak reset failed: {e}")