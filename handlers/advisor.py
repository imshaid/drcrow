"""
Advisor Info — member-facing flow.

Flow:
  Member taps "Advisor Info"
    → student_id stored?
        NO  → ask for it → validate → save → deliver
        YES → deliver directly

Delivery:
  1. Structured text as caption of the common advisor file (utilities category='advisor')
  2. URL button if admin provided one during addadvisor flow
  3. If no file in utilities → send as plain text message

Student ID format: XXX-YY-ZZZ  (e.g. 241-15-045)
Stored as-is in users.student_id.
"""

import re
import logging
from html import escape as h

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database.advisor_queries import (
    find_advisor_by_student_id,
    save_student_id,
    get_student_id,
)
from database.utility_queries import get_utilities_by_category

logger = logging.getLogger(__name__)
HTML   = ParseMode.HTML

# Regex: matches  XXX-YY-ZZZ  where each part is digits
_ID_RE = re.compile(r"^\d{3}-\d{2,3}-\d{3,4}$")

# Context key for pending student ID collection
_KEY = "advisor_awaiting_id"


def _valid_id(text: str) -> bool:
    return bool(_ID_RE.match(text.strip()))


def _build_caption(student_id: str, advisor: dict) -> str:
    """
    Build the structured caption for the advisor file.
    Header and labels bold. Email/phone in <code> for tap-to-copy.
    """
    lines = [
        "<b>Advisor Info — Batch 66</b>",
        "",
        f"<b>Student ID:</b> <code>{h(student_id)}</code>",
        "",
        f"<b>Advisor:</b> <code>{h(advisor['advisor_name'])}</code>",
    ]
    if advisor.get("designation"):
        lines.append(f"<b>Designation:</b> <code>{h(advisor['designation'])}</code>")
    if advisor.get("room"):
        lines.append(f"<b>Room:</b> <code>{h(advisor['room'])}</code>")
    if advisor.get("schedule"):
        lines.append(f"<b>Schedule:</b> <code>{h(advisor['schedule'])}</code>")
    if advisor.get("email"):
        lines.append(f"<b>Email:</b> <code>{h(advisor['email'])}</code>")
    if advisor.get("phone"):
        lines.append(f"<b>Phone:</b> <code>{h(advisor['phone'])}</code>")

    return "\n".join(lines)


async def _deliver_advisor_info(
    chat_id: int,
    student_id: str,
    bot: Bot,
):
    """
    Core delivery function.
    Finds the advisor by student_id, builds caption,
    sends the common utility file (category='advisor') with caption,
    or a plain text message if no file exists.
    """
    advisor = await find_advisor_by_student_id(student_id)

    # Fetch common file from utilities (most recent advisor entry)
    utility_items = await get_utilities_by_category("advisor", offset=0, limit=1)
    util = utility_items[0] if utility_items else None

    # Build URL button if utility has a URL
    keyboard = None
    if util and util.get("url"):
        btn_text = util.get("url_title") or "Open Link"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_text, url=util["url"])
        ]])

    if advisor:
        caption = _build_caption(student_id, advisor)
    else:
        # No match — still show file but with a note
        caption = (
            f"Advisor Info — Batch 66\n\n"
            f"Student ID: {h(student_id)}\n\n"
            f"No advisor found for this ID.\n"
            f"Please check the list below or contact your department."
        )

    # Send file with caption, or plain text if no file
    if util and util.get("file_id"):
        file_id   = util["file_id"]
        file_type = util.get("file_type", "document")
        try:
            if file_type == "photo":
                await bot.send_photo(
                    chat_id, file_id,
                    caption=caption,
                    parse_mode=HTML,
                    reply_markup=keyboard,
                )
            else:
                await bot.send_document(
                    chat_id, file_id,
                    caption=caption,
                    parse_mode=HTML,
                    reply_markup=keyboard,
                )
        except Exception:
            # Fallback: try send_document regardless
            try:
                await bot.send_document(
                    chat_id, file_id,
                    caption=caption,
                    parse_mode=HTML,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"Advisor file send failed: {e}")
                await bot.send_message(chat_id, caption, parse_mode=HTML, reply_markup=keyboard)
    else:
        # No file — plain text message
        await bot.send_message(chat_id, caption, parse_mode=HTML, reply_markup=keyboard)


async def handle_advisor_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point when member taps "Advisor Info".
    Checks if student_id is stored. If yes → deliver. If no → ask.
    """
    user_id    = update.effective_user.id
    chat_id    = update.effective_chat.id
    student_id = await get_student_id(user_id)

    if student_id:
        await _deliver_advisor_info(chat_id, student_id, context.bot)
    else:
        context.user_data[_KEY] = True
        await update.message.reply_text(
            "Enter your Student ID to get your advisor info.\n\n"
            "Format: <code>241-15-XXX</code>",
            parse_mode=HTML,
        )


async def handle_advisor_id_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Handle free-text input when bot is waiting for student ID.
    Called from dm_text_handler. Returns True if handled.
    """
    if not context.user_data.get(_KEY):
        return False

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return False

    if not _valid_id(text):
        await update.message.reply_text(
            "Invalid format. Please enter your Student ID like:\n"
            "<code>241-15-045</code>",
            parse_mode=HTML,
        )
        return True

    user_id = update.effective_user.id
    await save_student_id(user_id, text)
    context.user_data.pop(_KEY, None)

    await update.message.reply_text(
        f"Student ID saved: <code>{h(text)}</code>",
        parse_mode=HTML,
    )

    await _deliver_advisor_info(update.effective_chat.id, text, context.bot)
    return True