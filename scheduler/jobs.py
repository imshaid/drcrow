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
    """Sunday digest — top resources, leaderboard, weekly stats."""
    try:
        stats = await queries.get_weekly_stats()
        leaderboard = await queries.get_leaderboard(5)
        top_resources = await queries.get_top_resources_this_week(5)

        lb_text = ""
        for i, u in enumerate(leaderboard, 1):
            name = u["full_name"] or u["username"] or "Unknown"
            stars = u["stars"] or 0.0
            lb_text += f"{i}. {name} — {stars:.1f} ⭐\n"

        res_text = ""
        for r in top_resources:
            res_text += f"• {r['title']} ({r['access_count']} downloads)\n"

        digest = (
            f"🦅 *Weekly Digest — Twilight Crows*\n"
            f"_{datetime.now().strftime('%B %d, %Y')}_\n\n"
            f"📊 *This Week:*\n"
            f"⬇️ Downloads: {stats['downloads']}\n"
            f"⬆️ Uploads: {stats['uploads']}\n"
            f"👥 Active members: {stats['active_users']}\n\n"
            f"🔥 *Top Resources:*\n{res_text or 'No data yet'}\n"
            f"🏆 *Leaderboard:*\n{lb_text or 'No data yet'}\n\n"
            f"Keep contributing! 🖤"
        )

        # Post to group
        try:
            await app.bot.send_message(
                chat_id=settings.GROUP_ID,
                text=digest,
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=settings.ALLOWED_TOPIC_IDS[0] if settings.ALLOWED_TOPIC_IDS else None
            )
        except Exception as e:
            logger.warning(f"Could not post digest to group: {e}")

        logger.info("Weekly digest posted.")
    except Exception as e:
        logger.error(f"Weekly digest failed: {e}")


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