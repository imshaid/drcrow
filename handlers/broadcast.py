"""
Broadcast handler — admin sends files + text to members (DM) or group topic.

Flow:
  1. Choose target: Members (DM) or Group Topic
  2. Send file(s) one by one, - to skip
  3. Send text, - to skip
  4. Deliver
  5. Summary
"""

import asyncio
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from config.settings import settings
from database import queries
from handlers.semester import _mark_handled

logger = logging.getLogger(__name__)

BC_TARGET, BC_FILES, BC_TEXT = range(3)
_FLOOD_DELAY = getattr(settings, "FILE_FLOOD_DELAY", 0.05)


# ── Shared entry helper ───────────────────────────────────────────────────────

def _target_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Members (DM)", callback_data="bc_target_dm"),
        InlineKeyboardButton("Group Topic",  callback_data="bc_target_group"),
    ]])


async def _ask_target(msg_or_query, context, from_callback=False):
    context.user_data["bc_files"]          = []
    context.user_data["bc_text_msg"]       = None
    context.user_data["bc_target"]         = None
    context.user_data["_in_conversation"]  = True

    text = "<b>Broadcast</b>\n\nChoose delivery target:"
    if from_callback:
        await msg_or_query.message.reply_text(text, parse_mode="HTML",
                                               reply_markup=_target_keyboard())
    else:
        await msg_or_query.reply_text(text, parse_mode="HTML",
                                      reply_markup=_target_keyboard())
    return BC_TARGET


# ── Entry points ─────────────────────────────────────────────────────────────

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _ask_target(query, context, from_callback=True)


async def start_broadcast_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.is_admin(update.effective_user.id):
        return ConversationHandler.END
    return await _ask_target(update.message, context, from_callback=False)


# ── Step 0: Target selection ──────────────────────────────────────────────────

async def bc_choose_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    target = "dm" if query.data == "bc_target_dm" else "group"
    context.user_data["bc_target"] = target

    label = "Members (DM)" if target == "dm" else "Group Topic"
    await query.message.reply_text(
        f"<b>Broadcast — {label}</b>\n\n"
        "Step 1 — Send file(s) one by one (any format).\n"
        "Send <code>-</code> if no files.\n\n"
        "<i>/cancel to stop</i>",
        parse_mode="HTML"
    )
    return BC_FILES


# ── Step 1: Files ─────────────────────────────────────────────────────────────

async def bc_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

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

    # Extract file_id and file_type for mini app delivery
    if msg.document:
        fid, ftype = msg.document.file_id, "document"
    elif msg.photo:
        fid, ftype = msg.photo[-1].file_id, "photo"
    elif msg.video:
        fid, ftype = msg.video.file_id, "video"
    elif msg.audio:
        fid, ftype = msg.audio.file_id, "audio"
    elif msg.voice:
        fid, ftype = msg.voice.file_id, "voice"
    elif msg.sticker:
        fid, ftype = msg.sticker.file_id, "sticker"
    else:
        fid, ftype = None, "document"

    context.user_data["bc_files"].append({
        "from_chat":  msg.chat_id,
        "message_id": msg.message_id,
        "file_id":    fid,
        "file_type":  ftype,
    })
    count = len(context.user_data["bc_files"])
    await msg.reply_text(f"File {count} saved. Send more or - when done.")
    return BC_FILES


# ── Step 2: Text ──────────────────────────────────────────────────────────────

async def bc_collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    text  = msg.text.strip() if msg.text else ""
    files = context.user_data.get("bc_files", [])
    target = context.user_data.get("bc_target", "dm")
    has_text = text != "-"

    if not files and not has_text:
        await msg.reply_text(
            "Must provide at least one — file or text.\n"
            "Send your message text or - if you already added files."
        )
        return BC_TEXT

    if has_text:
        context.user_data["bc_text_msg"] = (msg.chat_id, msg.message_id)

    file_count = len(files)

    if target == "group":
        # Deliver to group topic
        topic_id = settings.ALLOWED_TOPIC_IDS[0] if settings.ALLOWED_TOPIC_IDS else None
        status = await msg.reply_text(f"Sending to group topic...")
        try:
            for f in files:
                fc  = f["from_chat"]  if isinstance(f, dict) else f[0]
                mid = f["message_id"] if isinstance(f, dict) else f[1]
                await context.bot.copy_message(
                    chat_id=settings.GROUP_ID,
                    from_chat_id=fc,
                    message_id=mid,
                    message_thread_id=topic_id,
                )
                await asyncio.sleep(_FLOOD_DELAY)
            if has_text:
                from_chat, msg_id = context.user_data["bc_text_msg"]
                await context.bot.copy_message(
                    chat_id=settings.GROUP_ID,
                    from_chat_id=from_chat,
                    message_id=msg_id,
                    message_thread_id=topic_id,
                )
            await status.edit_text("Sent to group topic.")
        except Exception as e:
            await status.edit_text(f"Failed: {e}")

    else:
        # Deliver to member DMs
        subscribers = await queries.get_subscribers_for("broadcast", course_code=None)
        status = await msg.reply_text(
            f"Broadcasting to {len(subscribers)} subscribers...\n"
            f"{file_count} file(s)  text: {'Yes' if has_text else 'No'}"
        )
        sent, failed = await _deliver(
            context.bot, subscribers, files,
            context.user_data.get("bc_text_msg"),
        )
        await status.edit_text(
            f"Broadcast complete.\n\nSent: {sent}\nFailed: {failed}"
        )

        # Log to broadcasts table (DM only)
        import json as _json
        _media_log    = [f if isinstance(f, dict) else {"from_chat": f[0], "message_id": f[1]} for f in files]
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

    context.user_data.pop("bc_files",         None)
    context.user_data.pop("bc_text_msg",       None)
    context.user_data.pop("bc_target",         None)
    context.user_data.pop("_in_conversation",  None)
    _mark_handled(update, context)
    return ConversationHandler.END


# ── Delivery ──────────────────────────────────────────────────────────────────

async def _deliver(bot, user_ids, file_msgs, text_msg):
    sent = failed = 0
    for uid in user_ids:
        try:
            for f in file_msgs:
                fc   = f["from_chat"]  if isinstance(f, dict) else f[0]
                mid  = f["message_id"] if isinstance(f, dict) else f[1]
                await bot.copy_message(
                    chat_id=uid, from_chat_id=fc, message_id=mid
                )
                await asyncio.sleep(_FLOOD_DELAY)
            if text_msg:
                from_chat, msg_id = text_msg
                await bot.copy_message(
                    chat_id=uid, from_chat_id=from_chat, message_id=msg_id
                )
                await asyncio.sleep(_FLOOD_DELAY)
            sent += 1
        except Exception as e:
            logger.debug(f"Broadcast failed for {uid}: {e}")
            failed += 1
    return sent, failed


# ── Cancel ────────────────────────────────────────────────────────────────────

async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("bc_files", "bc_text_msg", "bc_target", "_in_conversation"):
        context.user_data.pop(k, None)
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END


# ── ConversationHandler ───────────────────────────────────────────────────────

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
            BC_TARGET: [
                CallbackQueryHandler(bc_choose_target, pattern="^bc_target_(dm|group)$"),
            ],
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