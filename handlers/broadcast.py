"""
Broadcast handler — admin sends files + text to all broadcast subscribers.

Flow (triggered from /admin → Broadcast button):
  1. Bot asks for files (send one by one, - to skip)
  2. Bot asks for text (- to skip, format preserved via copy_message)
  3. Validation: at least one must exist
  4. Deliver to all broadcast subscribers
  5. Summary: sent / failed
"""

import asyncio
import logging
from telegram import Update, Message
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from config.settings import settings
from database import queries
from handlers.semester import _mark_handled

logger = logging.getLogger(__name__)

BC_FILES, BC_TEXT = range(2)
_FLOOD_DELAY = getattr(settings, "FILE_FLOOD_DELAY", 0.05)


# ── Entry ─────────────────────────────────────────────────────────────────────

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["bc_files"] = []   # list of (chat_id, message_id) for copy
    context.user_data["bc_text_msg"] = None  # (chat_id, message_id) for copy
    context.user_data["_in_conversation"] = True

    await query.message.reply_text(
        "<b>Broadcast</b>\n\n"
        "Step 1 — Send file(s) one by one (any format).\n"
        "Send <code>-</code> if no files.",
        parse_mode="HTML"
    )
    return BC_FILES


# ── Step 1: Files ─────────────────────────────────────────────────────────────

async def bc_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # Text input
    if msg.text:
        if msg.text.strip() == "-":
            await msg.reply_text(
                "Step 2 — Send your message text.\n"
                "Send <code>-</code> if no text.",
                parse_mode="HTML"
            )
            return BC_TEXT
        await msg.reply_text("Send a file or - to skip files.")
        return BC_FILES

    # Any media — store chat_id + message_id for copy_message later
    context.user_data["bc_files"].append(
        (msg.chat_id, msg.message_id)
    )
    count = len(context.user_data["bc_files"])
    await msg.reply_text(f"✅ File {count} saved. Send more or send - when done.")
    return BC_FILES


# ── Step 2: Text ──────────────────────────────────────────────────────────────

async def bc_collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    text = msg.text.strip() if msg.text else ""

    files = context.user_data.get("bc_files", [])
    has_text = text != "-"

    if not files and not has_text:
        await msg.reply_text(
            "❌ Must provide at least one — file or text.\n"
            "Send your message text or - if you already added files."
        )
        return BC_TEXT

    # Store text message reference for copy
    if has_text:
        context.user_data["bc_text_msg"] = (msg.chat_id, msg.message_id)

    from database.queries import get_subscribers_for
    subscribers = await get_subscribers_for("broadcast", course_code=None)

    file_count = len(files)
    status = await msg.reply_text(
        f"📢 Broadcasting to {len(subscribers)} subscribers...\n"
        f"📎 {file_count} file(s)  💬 {'Yes' if has_text else 'No'} text"
    )

    sent, failed = await _deliver(
        context.bot,
        subscribers,
        files,
        context.user_data.get("bc_text_msg"),
    )

    await status.edit_text(
        f"✅ Broadcast complete!\n\n"
        f"✔ Sent: {sent}\n"
        f"✖ Failed: {failed}"
    )

    # Log broadcast with entities and media references
    import json as _json
    _media_log = [{"from_chat": fc, "message_id": mid} for fc, mid in files]
    _entities_log = []
    if has_text and msg.entities:
        _entities_log = [e.to_dict() for e in msg.entities]

    pool = await queries.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO broadcasts (message, sent_by, message_entities, media)
               VALUES ($1, $2, $3, $4)""",
            text if has_text else f"[{file_count} file(s) only]",
            update.effective_user.id,
            _json.dumps(_entities_log),
            _json.dumps(_media_log),
        )

    context.user_data.pop("bc_files", None)
    context.user_data.pop("bc_text_msg", None)
    context.user_data.pop("_in_conversation", None)
    _mark_handled(update, context)
    return ConversationHandler.END


# ── Delivery — uses copy_message to preserve exact format ─────────────────────

async def _deliver(
    bot,
    user_ids: list,
    file_msgs: list,          # [(from_chat_id, message_id), ...]
    text_msg: tuple | None,   # (from_chat_id, message_id) or None
):
    sent = failed = 0
    for uid in user_ids:
        try:
            for from_chat, msg_id in file_msgs:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat,
                    message_id=msg_id
                )
                await asyncio.sleep(_FLOOD_DELAY)

            if text_msg:
                from_chat, msg_id = text_msg
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat,
                    message_id=msg_id
                )
                await asyncio.sleep(_FLOOD_DELAY)

            sent += 1
        except Exception as e:
            logger.debug(f"Broadcast failed for {uid}: {e}")
            failed += 1

    return sent, failed


# ── Cancel ────────────────────────────────────────────────────────────────────

async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bc_files", None)
    context.user_data.pop("bc_text_msg", None)
    context.user_data.pop("_in_conversation", None)
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END


# ── ConversationHandler ───────────────────────────────────────────────────────

async def start_broadcast_from_text(update, context):
    """Entry from Reply KB Broadcast button — part of ConversationHandler."""
    from config.settings import settings as _s
    if not _s.is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["bc_files"]      = []
    context.user_data["bc_text_msg"]   = None
    context.user_data["_in_conversation"] = True
    await update.message.reply_text(
        "<b>Broadcast</b>\n\n"
        "Step 1 — Send file(s) one by one (any format).\n"
        "Send <code>-</code> if no files.\n\n"
        "<i>/cancel to stop</i>",
        parse_mode="HTML"
    )
    return BC_FILES


def broadcast_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_broadcast, pattern="^admin_broadcast$"),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(r"^Broadcast$"),
                start_broadcast_from_text
            ),
        ],
        states={
            BC_FILES: [
                MessageHandler(
                    (filters.Document.ALL | filters.PHOTO | filters.VIDEO |
                     filters.AUDIO | filters.VOICE | filters.Sticker.ALL |
                     (filters.TEXT & ~filters.COMMAND)),
                    bc_collect_file
                ),
            ],
            BC_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_collect_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", bc_cancel)],
        conversation_timeout=600,
        per_message=False,
    )