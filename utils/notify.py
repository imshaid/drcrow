"""
Subscription notification dispatcher.
"""

import asyncio
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from config.settings import settings
from database.queries import get_subscribers_for

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    "note":      "notes",
    "notes":     "notes",
    "book":      "books",
    "books":     "books",
    "solve":     "solutions",
    "solution":  "solutions",
    "psq":       "psqs",
    "vidoc":     "videos",
    "video":     "videos",
    "utility":   "utilities",
    "syllabus":  "syllabus",
    "outline":   "outline",
    "routine":   "routine",
    "calendar":  "calendar",
    "advisor":   "advisor",
    "regpay":    "regpay",
    "waiver":    "regpay",
    "broadcast": "broadcast",
}

_FLOOD_DELAY = getattr(settings, "FILE_FLOOD_DELAY", 0.05)


def _notification_keyboard(uid: str = "") -> InlineKeyboardMarkup | None:
    """
    [📥 Get Resource] if uid provided, else no keyboard.
    UID never visible to member.
    """
    if uid:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Get Resource", callback_data=f"get_res_{uid}"),
        ]])
    return None


async def notify_resource(
    bot: Bot,
    category: str,
    course_code: str,
    title: str,
    uid: str,
    extra: str = "",
):
    """Notify subscribers when a new course resource is added."""
    sub_cat = CATEGORY_MAP.get(category.lower())
    if not sub_cat:
        return

    user_ids = await get_subscribers_for(sub_cat, course_code)
    if not user_ids:
        return

    type_label  = sub_cat.title().rstrip("s")
    course_line = f"Course: {course_code}\n" if course_code else ""
    extra_line  = f"\n{extra}" if extra else ""

    text = (
        f"\U0001f514 *New {type_label}*\n\n"
        f"*{title}*\n"
        f"{course_line}"
        f"{extra_line}"
    )

    keyboard = _notification_keyboard(uid)
    sent = 0
    for uid_ in user_ids:
        try:
            await bot.send_message(
                uid_, text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            sent += 1
            await asyncio.sleep(_FLOOD_DELAY)
        except Exception:
            pass

    logger.info(f"Notified {sent}/{len(user_ids)} subscribers for {sub_cat} [{course_code}]")


async def notify_global(
    bot: Bot,
    category: str,
    title: str,
    uid: str = "",
    extra: str = "",
):
    """Notify subscribers of a global (non-course) topic."""
    sub_cat = CATEGORY_MAP.get(category.lower())
    if not sub_cat:
        return

    user_ids = await get_subscribers_for(sub_cat, course_code=None)
    if not user_ids:
        return

    extra_line = f"\n{extra}" if extra else ""
    text = (
        f"\U0001f514 *New {title}*"
        f"{extra_line}"
    )

    keyboard = _notification_keyboard(uid)
    sent = 0
    for user_id in user_ids:
        try:
            await bot.send_message(
                user_id, text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            sent += 1
            await asyncio.sleep(_FLOOD_DELAY)
        except Exception:
            pass

    logger.info(f"Notified {sent}/{len(user_ids)} global subscribers for {sub_cat}")