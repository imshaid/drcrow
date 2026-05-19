"""
Upload Resource handler — member submission flow.
/upload or "Upload" button

Flow:
  Files (/done) → Admin notified → Approve/Reject
"""

import logging
import json
import asyncio
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from database import queries
from utils.stars import award_upload

logger = logging.getLogger(__name__)
HTML   = ParseMode.HTML

_CANCEL_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Cancel", callback_data="flow_cancel")
]])

# In-memory store for report flow state
_report_pending: dict = {}

# ── States ────────────────────────────────────────────────────────────────────
U_FILES = 0
R_RESOURCE, R_REASON, R_FILES = range(3)


def _detect_file_type(msg) -> tuple:
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.video_note:
        return msg.video_note.file_id, "video_note"
    if msg.audio:
        return msg.audio.file_id, "audio"
    if msg.voice:
        return msg.voice.file_id, "voice"
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        fn   = (msg.document.file_name or "").lower()
        if "pdf"          in mime:                                   return msg.document.file_id, "pdf"
        if "spreadsheet"  in mime or fn.endswith((".xlsx", ".xls")): return msg.document.file_id, "excel"
        if "presentation" in mime or fn.endswith(".pptx"):           return msg.document.file_id, "pptx"
        if "word"         in mime or fn.endswith(".docx"):           return msg.document.file_id, "docx"
        if mime.startswith("image"):                                  return msg.document.file_id, "image"
        if mime.startswith("video"):                                  return msg.document.file_id, "video"
        return msg.document.file_id, "document"
    return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# MEMBER UPLOAD FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    user = update.effective_user

    from middleware.membership import check_membership
    if not settings.is_admin(user.id):
        allowed = await check_membership(context.bot, user.id)
        if not allowed:
            await msg.reply_text("Members only.")
            return ConversationHandler.END

    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["uploader_id"]      = user.id
    context.user_data["uploader_name"]    = user.full_name or "Unknown"
    context.user_data["uploader_user"]    = f"@{user.username}" if user.username else str(user.id)
    context.user_data["files"]            = []

    await msg.reply_text(
        "<b>Upload Resource</b>\n\n"
        "Send your file(s) one by one.\n"
        "PDF, image, DOCX, video, link \u2014 all types accepted.\n\n"
        "Send /done when finished.",
        parse_mode=HTML,
        reply_markup=_CANCEL_KB
    )
    return U_FILES


async def upload_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # Guard: if files key missing, conversation was cancelled — end silently
    if "files" not in context.user_data:
        return ConversationHandler.END

    file_id, file_type = _detect_file_type(msg)

    if file_id:
        context.user_data["files"].append({
            "file_id":   file_id,
            "file_type": file_type,
            "name":      getattr(getattr(msg, "document", None), "file_name", None) or file_type
        })
        count = len(context.user_data["files"])
        await msg.reply_text(f"File {count} saved. Send more or /done.")
    elif msg.text:
        text = msg.text.strip()
        context.user_data["files"].append({
            "file_id":   None,
            "file_type": "link",
            "link":      text
        })
        count = len(context.user_data["files"])
        await msg.reply_text(f"Link {count} saved. Send more or /done.")
    else:
        await msg.reply_text("Please send a file or a link.")

    return U_FILES


async def upload_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = context.user_data.get("files", [])
    if not files:
        await update.message.reply_text("Please send at least one file first.")
        return U_FILES
    return await _submit(update, context)


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud    = context.user_data
    files = ud.get("files", [])

    first_file_type = next(
        (f["file_type"] for f in files if f.get("file_type") and f.get("file_type") != "link"),
        files[0]["file_type"] if files else "other"
    )
    first_file_id = next(
        (f["file_id"] for f in files if f.get("file_id")),
        None
    )

    pending_id = await queries.insert_pending_resource(
        submitted_by = ud["uploader_id"],
        title        = "Untitled",
        course_code  = "",
        category     = "other",
        tags         = {"files": files},
        file_id      = first_file_id,
        file_type    = first_file_type,
    )

    info = (
        f"<b>New Submission #{pending_id}</b>\n\n"
        f"<b>Files:</b> {len(files)}\n"
        f"<b>From:</b> {h(ud['uploader_name'])} {ud['uploader_user']}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"upload_approve_{pending_id}_{ud['uploader_id']}"),
        InlineKeyboardButton("Reject",  callback_data=f"upload_reject_{pending_id}_{ud['uploader_id']}"),
    ]])

    for admin_id in settings.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, info, parse_mode=HTML, reply_markup=keyboard)
            for i, f in enumerate(files):
                if f.get("file_type") == "link":
                    await context.bot.send_message(admin_id, f"Link {i+1}: {f['link']}")
                else:
                    cap = f"File {i+1}/{len(files)}"
                    ft  = f["file_type"]
                    try:
                        if ft == "photo":
                            await context.bot.send_photo(admin_id, f["file_id"], caption=cap)
                        elif ft == "video":
                            await context.bot.send_video(admin_id, f["file_id"], caption=cap)
                        elif ft == "audio":
                            await context.bot.send_audio(admin_id, f["file_id"], caption=cap)
                        elif ft == "voice":
                            await context.bot.send_voice(admin_id, f["file_id"], caption=cap)
                        elif ft == "video_note":
                            await context.bot.send_video_note(admin_id, f["file_id"])
                        else:
                            await context.bot.send_document(admin_id, f["file_id"], caption=cap)
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error(f"Failed to forward file to admin: {e}")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    await update.effective_message.reply_text(
        f"<b>Submitted.</b>\n\n"
        f"Reference: <code>#{pending_id}</code>\n\n"
        f"You'll be notified once an admin reviews it.",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN APPROVE / REJECT
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    user  = update.effective_user

    if not settings.is_admin(user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    await query.answer()

    if data.startswith("upload_approve_"):
        parts       = data.replace("upload_approve_", "").split("_")
        pending_id  = int(parts[0])
        uploader_id = int(parts[1])

        await queries.update_pending_status(pending_id, "approved", user.id)

        _upload_star_val = 3
        try:
            from database.db import get_pool
            _pool = await get_pool()
            async with _pool.acquire() as _conn:
                _row = await _conn.fetchrow(
                    "SELECT category FROM pending_resources WHERE id = $1", pending_id
                )
            _cat = (_row["category"] or "other").lower() if _row else "other"
            _upload_map = {
                "book": 15, "solution_manual": 12, "solve": 10, "note": 8,
                "psq": 6, "vidoc": 6, "syllabus": 4, "outline": 4,
                "routine": 3, "cal": 3, "advisor": 3, "fee": 3,
                "utility": 2, "waiver": 2, "regpay": 2,
            }
            _upload_star_val = _upload_map.get(_cat, 3)
            await award_upload(uploader_id, _cat, str(pending_id))
        except Exception as _e:
            logger.warning(f"award_upload failed: {_e}")

        try:
            await context.bot.send_message(
                uploader_id,
                f"<b>Submission approved.</b>\n\n"
                f"Reference: <code>#{pending_id}</code>\n\n"
                f"Your resource has been reviewed and accepted.\n"
                f"You earned <b>+{_upload_star_val} \u2b50</b> for your contribution.",
                parse_mode=HTML
            )
        except Exception as e:
            logger.error(f"Failed to notify uploader {uploader_id}: {e}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Approved #{pending_id}. Uploader notified.")

    elif data.startswith("upload_reject_"):
        parts       = data.replace("upload_reject_", "").split("_")
        pending_id  = int(parts[0])
        uploader_id = int(parts[1])

        if not hasattr(context.bot, "_admin_pending"):
            context.bot._admin_pending = {}
        context.bot._admin_pending[user.id] = {
            "action":      "reject",
            "pending_id":  pending_id,
            "uploader_id": uploader_id,
        }
        await query.message.reply_text(
            f"<b>Rejecting #{pending_id}</b>\n\n"
            f"Send a reason for the uploader:",
            parse_mode=HTML
        )


async def admin_reject_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg      = update.message
    admin_id = update.effective_user.id
    text     = (msg.text or "").strip()
    bot      = context.bot

    if not hasattr(bot, "_admin_pending") or admin_id not in bot._admin_pending:
        return False
    if bot._admin_pending[admin_id].get("action") != "reject":
        return False

    pending     = bot._admin_pending[admin_id]
    pending_id  = pending["pending_id"]
    uploader_id = pending["uploader_id"]

    await queries.update_pending_status(pending_id, "rejected", admin_id)

    reason_part = f"\n\nReason: {h(text)}" if text and text != "-" else ""
    try:
        await bot.send_message(
            uploader_id,
            f"<b>Submission not approved.</b>\n\n"
            f"Reference: <code>#{pending_id}</code>{reason_part}",
            parse_mode=HTML
        )
    except Exception as e:
        logger.error(f"Failed to notify uploader {uploader_id}: {e}")

    del bot._admin_pending[admin_id]
    await msg.reply_text("Rejected. Uploader notified.")
    return True


async def handle_admin_pending_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not settings.is_admin(user.id):
        return False
    bot = context.bot
    if not hasattr(bot, "_admin_pending") or user.id not in bot._admin_pending:
        return False
    action = bot._admin_pending[user.id].get("action")
    if action == "reject":
        return await admin_reject_message(update, context)
    return False


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _report_pending.pop(update.effective_user.id, None)
    if hasattr(context.bot, "_report_pending"):
        context.bot._report_pending.discard(update.effective_user.id)
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


async def _cancel_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel upload from inline button — properly ends ConversationHandler."""
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data.clear()
    await query.message.reply_text("Upload cancelled.")
    return ConversationHandler.END


async def _exit_to_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: user pressed Report while in upload flow — cancel upload, start report."""
    context.user_data.clear()
    await report_start(update, context)
    return ConversationHandler.END


def upload_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("upload", upload_start),
            CallbackQueryHandler(upload_start, pattern="^menu_upload$"),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.TEXT & filters.Regex(r"^Upload$"),
                upload_start
            ),
        ],
        states={
            U_FILES: [
                CommandHandler("done", upload_files_done),
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO |
                    filters.VIDEO | filters.VIDEO_NOTE |
                    filters.AUDIO | filters.VOICE |
                    (filters.TEXT & ~filters.COMMAND),
                    upload_collect_file
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
            CallbackQueryHandler(_cancel_from_callback, pattern="^flow_cancel$"),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.TEXT & filters.Regex(r"^Report$"),
                _exit_to_report
            ),
        ],
        conversation_timeout=600,
        per_message=False,
        allow_reentry=True
    )


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT FLOW  — state machine via user_data (no ConversationHandler)
#
# user_data keys:
#   _report_step      : "reason" | "files"
#   resource_ref      : inline result_id
#   resource_title    : human-readable title
#   resource_uploader : uploader user_id or None
#   reason            : reason text or None
#   report_files      : list of file dicts
#   _in_conversation  : True (blocks dm_fallback)
# ═══════════════════════════════════════════════════════════════════════════════

async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry — from 'Report' button or /report command."""
    msg  = update.effective_message
    user = update.effective_user

    from middleware.membership import check_membership
    if not settings.is_admin(user.id):
        allowed = await check_membership(context.bot, user.id)
        if not allowed:
            await msg.reply_text("Members only.")
            return

    # Clear any previous report state
    for key in ["_report_step", "resource_ref", "resource_title",
                "resource_uploader", "reason", "report_files"]:
        context.user_data.pop(key, None)

    if not hasattr(context.bot, "_report_pending"):
        context.bot._report_pending = set()
    context.bot._report_pending.add(user.id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Search Resource", switch_inline_query_current_chat="")
    ]])
    await msg.reply_text(
        "<b>Report a Resource</b>\n\n"
        "Search for the resource you want to report, "
        "then select it from the results.",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Search Resource", switch_inline_query_current_chat=""),
            InlineKeyboardButton("Cancel", callback_data="flow_cancel"),
        ]])
    )


async def report_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from dm_text_handler when user is in report flow.
    Returns True if handled, False otherwise.
    """
    ud   = context.user_data
    step = ud.get("_report_step")
    if not step:
        return False

    msg  = update.message
    text = (msg.text or "").strip()

    if text == "/cancel":
        await _report_cancel(update, context)
        return True

    if step == "reason":
        if not text or text == "-":
            await msg.reply_text(
                "Please describe the issue. This is required to submit a report."
            )
            return True
        ud["reason"] = text
        ud["_report_step"] = "files"
        await msg.reply_text(
            "Attach correction file(s) if you have any. (optional)\n"
            "PDF, image, DOCX, video \u2014 all types accepted.\n\n"
            "Send /done when finished, or /done now to skip.",
            parse_mode=HTML,
            reply_markup=_CANCEL_KB
        )
        return True

    if step == "files":
        await msg.reply_text("Send a file, or /done to submit.")
        return True

    return False


async def report_handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from dm_text_handler when user sends a file during report flow.
    Returns True if handled.
    """
    ud   = context.user_data
    step = ud.get("_report_step")
    if step != "files":
        return False

    msg = update.message
    file_id, file_type = _detect_file_type(msg)
    if file_id:
        ud.setdefault("report_files", []).append({
            "file_id":   file_id,
            "file_type": file_type,
            "name":      getattr(getattr(msg, "document", None), "file_name", None) or file_type
        })
        count = len(ud["report_files"])
        await msg.reply_text(f"File {count} saved. Send more or /done.")
    return True


async def _report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ["_report_step", "_in_conversation", "resource_ref",
                "resource_title", "resource_uploader", "reason", "report_files"]:
        context.user_data.pop(key, None)
    if hasattr(context.bot, "_report_pending"):
        context.bot._report_pending.discard(update.effective_user.id)
    await update.effective_message.reply_text("Report cancelled.")


async def _report_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud          = context.user_data
    reporter_id = update.effective_user.id
    reason      = ud.get("reason")
    files       = ud.get("report_files", [])
    uploader_id = ud.get("resource_uploader")
    title       = ud.get("resource_title", "\u2014")

    if not reason:
        await update.effective_message.reply_text(
            "Please provide a reason before submitting."
        )
        return

    try:
        report_id = await queries.insert_report(reporter_id, None, reason or "")
    except Exception:
        report_id = "N/A"

    reason_line   = f"<b>Reason:</b> {h(reason)}\n" if reason else ""
    files_line    = f"<b>Files:</b> {len(files)}\n" if files else ""
    uploader_line = f"<b>Uploader ID:</b> {uploader_id}\n" if uploader_id else ""

    reporter_name = update.effective_user.full_name or "Unknown"
    reporter_user = f"@{update.effective_user.username}" if update.effective_user.username else str(reporter_id)

    info = (
        f"<b>Report #{report_id}</b>\n\n"
        f"<b>Resource:</b> {h(title)}\n"
        f"{reason_line}"
        f"{files_line}"
        f"{uploader_line}"
        f"<b>Reporter:</b> {h(reporter_name)} {reporter_user}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Accept",
            callback_data=f"rpt_accept_{report_id}_{reporter_id}_{uploader_id or 0}"
        ),
        InlineKeyboardButton(
            "Reject",
            callback_data=f"rpt_reject_{report_id}_{reporter_id}"
        ),
    ]])

    for admin_id in settings.ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, info, parse_mode=HTML, reply_markup=keyboard
            )
            for i, f in enumerate(files):
                cap = f"Correction {i+1}/{len(files)}"
                ft  = f["file_type"]
                try:
                    if ft == "photo":
                        await context.bot.send_photo(admin_id, f["file_id"], caption=cap)
                    elif ft == "video":
                        await context.bot.send_video(admin_id, f["file_id"], caption=cap)
                    elif ft == "audio":
                        await context.bot.send_audio(admin_id, f["file_id"], caption=cap)
                    elif ft == "voice":
                        await context.bot.send_voice(admin_id, f["file_id"], caption=cap)
                    else:
                        await context.bot.send_document(admin_id, f["file_id"], caption=cap)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"Failed to forward correction file: {e}")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    # Clear state
    for key in ["_report_step", "_in_conversation", "resource_ref",
                "resource_title", "resource_uploader", "reason", "report_files"]:
        context.user_data.pop(key, None)
    if hasattr(context.bot, "_report_pending"):
        context.bot._report_pending.discard(reporter_id)

    await update.effective_message.reply_text(
        f"<b>Report submitted.</b>\n\n"
        f"Reference: <code>#{report_id}</code>\n\n"
        f"You'll be notified once an admin reviews it.",
        parse_mode=HTML
    )


async def handle_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    user  = update.effective_user

    if not settings.is_admin(user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    await query.answer()

    if data.startswith("rpt_accept_"):
        parts       = data.split("_")
        report_id   = int(parts[2])
        reporter_id = int(parts[3])
        uploader_id = int(parts[4]) if len(parts) > 4 and parts[4] not in ("0", "") else None

        logger.info(f"Report accept: report={report_id} reporter={reporter_id} uploader={uploader_id}")

        await queries.add_stars(reporter_id, 3, f"report_accepted:{report_id}")
        if uploader_id and not settings.is_admin(uploader_id):
            await queries.add_stars(uploader_id, -5, f"report_against:{report_id}")

        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE reports SET status=\'accepted\', reviewed_by=$1 WHERE id=$2",
                user.id, report_id
            )

        try:
            await context.bot.send_message(
                reporter_id,
                f"<b>Report #{report_id} accepted.</b>\n\n"
                f"You earned <b>+3 \u2b50</b> for the valid report.",
                parse_mode=HTML
            )
        except Exception as e:
            logger.error(f"Failed to notify reporter: {e}")

        if uploader_id and not settings.is_admin(uploader_id):
            try:
                await context.bot.send_message(
                    uploader_id,
                    f"<b>A report against one of your resources has been accepted.</b>\n\n"
                    f"<b>-5 \u2b50</b> deducted from your stars.",
                    parse_mode=HTML
                )
            except Exception as e:
                logger.error(f"Failed to notify uploader: {e}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Report #{report_id} accepted. Stars updated.")

    elif data.startswith("rpt_reject_"):
        parts       = data.split("_")
        report_id   = int(parts[2])
        reporter_id = int(parts[3])

        if not hasattr(context.bot, "_admin_pending"):
            context.bot._admin_pending = {}
        context.bot._admin_pending[user.id] = {
            "action":      "report_reject",
            "report_id":   report_id,
            "reporter_id": reporter_id,
        }
        await query.message.reply_text(
            f"<b>Rejecting report #{report_id}</b>\n\n"
            f"Send a reason for the reporter "
            f"(or <code>-</code> to skip):",
            parse_mode=HTML
        )


async def admin_report_reject_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    admin_id = update.effective_user.id
    bot      = context.bot
    if not hasattr(bot, "_admin_pending") or admin_id not in bot._admin_pending:
        return False
    pending = bot._admin_pending[admin_id]
    if pending.get("action") != "report_reject":
        return False

    text        = (update.message.text or "").strip()
    report_id   = pending["report_id"]
    reporter_id = pending["reporter_id"]

    await queries.add_stars(reporter_id, -2, f"report_rejected:{report_id}")

    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE reports SET status=\'rejected\', reviewed_by=$1 WHERE id=$2",
            admin_id, report_id
        )

    reason_part = f"\n\nReason: {h(text)}" if text and text != "-" else ""
    try:
        await bot.send_message(
            reporter_id,
            f"<b>Report #{report_id} rejected.</b>\n\n"
            f"<b>-2 \u2b50</b> deducted from your stars.{reason_part}",
            parse_mode=HTML
        )
    except Exception as e:
        logger.error(f"Failed to notify reporter: {e}")

    del bot._admin_pending[admin_id]
    await update.message.reply_text(f"Report #{report_id} rejected. Reporter notified.")
    return True


async def handle_report_pending_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Legacy stub — kept for import compatibility."""
    return False


def report_conversation():
    """Stub — report flow no longer uses ConversationHandler."""
    return None