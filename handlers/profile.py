"""Profile handler."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from middleware.membership import should_respond
from database import queries

logger = logging.getLogger(__name__)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await should_respond(update, context.bot):
        return
    user = update.effective_user
    db_user = await queries.get_user(user.id)
    if not db_user:
        await update.message.reply_text("Send /start first to register.")
        return

    subs = await queries.get_user_subscriptions(user.id)
    sub_text = ", ".join(subs) if subs else "None"
    stars = db_user["stars"] or 0.0
    stars_display = f"{stars:.1f}".rstrip("0").rstrip(".")

    report_stats = await _get_report_stats(user.id)

    await update.message.reply_text(
        f"<b>{db_user['full_name']}</b>\n"
        f"@{db_user['username'] or 'N/A'}\n\n"
        f"<code>"
        f"Stars     : {stars_display} ⭐\n"
        f"Uploads   : {db_user['upload_count']}\n"
        f"Downloads : {db_user['download_count']}\n"
        f"Reports   : {report_stats['valid']} valid / {report_stats['false']} false"
        f"</code>\n\n"
        f"Subscribed: {sub_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Leaderboard", callback_data="profile_leaderboard")
        ]])
    )


async def _get_report_stats(user_id: int) -> dict:
    try:
        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            valid = await conn.fetchval(
                "SELECT COUNT(*) FROM reports WHERE reporter_id = $1 AND status = 'accepted'",
                user_id
            ) or 0
            false = await conn.fetchval(
                "SELECT COUNT(*) FROM reports WHERE reporter_id = $1 AND status = 'rejected'",
                user_id
            ) or 0
        return {"valid": valid, "false": false}
    except Exception:
        return {"valid": 0, "false": 0}


async def handle_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await should_respond(update, context.bot):
        return
    query = update.callback_query
    await query.answer()
    if query.data == "profile_leaderboard":
        leaders = await queries.get_leaderboard(10)
        text = "<b>Leaderboard — Top 10</b>\n\n"
        for i, u in enumerate(leaders, 1):
            name = u["full_name"] or u["username"] or "Unknown"
            stars = u["stars"] or 0.0
            stars_display = f"{stars:.1f}".rstrip("0").rstrip(".")
            text += f"{i}. {name} — {stars_display} ⭐\n"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data="profile_back")
            ]])
        )
    elif query.data == "profile_back":
        await query.delete_message()