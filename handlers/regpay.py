"""
Registration & Payment Info CRUD + Profile + Help handlers.

Commands (admin):
  /addregpay <uid>, /editregpay <uid>, /deleteregpay <uid>, /listregpays
  /sethelp  — set help text (formatted message)

Member actions (buttons/inline):
  Profile button → show user stats
  Help button    → send help text
"""

import logging
from utils.stars import award_download
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
from database.regpay_queries import (
    regpay_uid_exists, insert_regpay, get_regpay,
    update_regpay_files, update_regpay_field,
    delete_regpay, get_regpay_paginated, get_regpay_count,
    increment_regpay_access, search_regpay,
    get_setting, set_setting
)
from database import queries
from utils.imgbb import upload_to_imgbb

logger    = logging.getLogger(__name__)
HTML      = ParseMode.HTML
PAGE_SIZE = 5

# ── Add states ──────────────────────────────────────────────────────────────────
AR_SEMESTER, AR_FILES, AR_THUMB, AR_TAGS = range(4)

# ── Edit states ─────────────────────────────────────────────────────────────────
ER_MENU, ER_VALUE, ER_FILES, ER_THUMB = range(4)

# ── Delete state ────────────────────────────────────────────────────────────────
DR_CONFIRM = 0

# ── Help set state ──────────────────────────────────────────────────────────────
SH_TEXT = 0


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not settings.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _detect_file_type(msg) -> tuple:
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.document:
        mime = msg.document.mime_type or ""
        fn   = (msg.document.file_name or "").lower()
        if "pdf"          in mime:                               return msg.document.file_id, "pdf"
        if "spreadsheet"  in mime or fn.endswith((".xlsx",".xls")): return msg.document.file_id, "excel"
        if "presentation" in mime or fn.endswith(".pptx"):      return msg.document.file_id, "pptx"
        if "word"         in mime or fn.endswith(".docx"):      return msg.document.file_id, "docx"
        if mime.startswith("image"):                            return msg.document.file_id, "image_doc"
        return msg.document.file_id, "document"
    return None, None


def _tag_str(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _regpay_summary(r: dict) -> str:
    try:
        files = json.loads(r["file_ids"]) if isinstance(r["file_ids"], str) else r.get("file_ids", [])
    except Exception:
        files = []
    return (
        f"📋 <b>Registration & Payment Info</b>\n"
        f"🗓 {h(r['semester'])}\n"
        f"🆔 <code>{h(r['uid'])}</code>\n"
        f"📄 Files: {len(files)}\n"
        f"🖼 Thumbnail: {'✅' if r.get('thumbnail_url') else '—'}\n"
        f"🏷 {h(_tag_str(r.get('tags', [])))}"
    )


def _edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗓 Semester",   callback_data="er_semester"),
        InlineKeyboardButton("📄 Files",      callback_data="er_files"),
    ], [
        InlineKeyboardButton("🏷 Tags",       callback_data="er_tags"),
        InlineKeyboardButton("🖼 Thumbnail",  callback_data="er_thumb"),
    ], [
        InlineKeyboardButton("✖ Cancel",      callback_data="er_cancel"),
    ]])


# ═══════════════════════════════════════════════════════════════════════════════
# /addregpay <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def addregpay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/addregpay &lt;uid&gt;</code>\n"
            "Example: <code>/addregpay regpay01</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    if not uid.replace("-", "").isalnum():
        await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
        return ConversationHandler.END

    if await regpay_uid_exists(uid):
        await update.message.reply_text(
            f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["regpay_uid"]   = uid
    context.user_data["_uploader_id"] = update.effective_user.id
    context.user_data["files"]        = []

    await update.message.reply_text(
        f"📋 <b>Add Registration & Payment Info</b>\n\n"
        f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
        f"Step 1/4 — <b>Semester</b>\n\n"
        f"Example: <code>Summer 2026</code>\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return AR_SEMESTER


async def addregpay_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["semester"] = update.message.text.strip()
    await update.message.reply_text(
        "Step 2/4 — <b>Files</b>\n\n"
        "Send files one by one (PDF, image, DOCX, Excel, PPTX).\n"
        "All files will be delivered together to members.\n\n"
        "Send <b>/done</b> when finished.",
        parse_mode=HTML
    )
    return AR_FILES


async def addregpay_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("❌ Please send a file (PDF, image, DOCX, Excel, PPTX).")
        return AR_FILES

    context.user_data["files"].append({"file_id": file_id, "file_type": file_type})
    count = len(context.user_data["files"])
    await msg.reply_text(f"✅ File {count} saved. Send more or /done to finish.")
    return AR_FILES


async def addregpay_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = context.user_data.get("files", [])
    if not files:
        await update.message.reply_text("❌ Please send at least one file first.")
        return AR_FILES

    await update.message.reply_text(
        f"✅ {len(files)} file(s) saved!\n\n"
        f"Step 3/4 — <b>Thumbnail</b> (optional)\n\n"
        f"Send cover image for inline search preview or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AR_THUMB


async def addregpay_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return AR_THUMB
    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid
    await _ask_tags(msg)
    return AR_TAGS


async def addregpay_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text("Send an image or <code>-</code>.", parse_mode=HTML)
        return AR_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_tags(update.message)
    return AR_TAGS


async def _ask_tags(msg):
    await msg.reply_text(
        "Step 4/4 — <b>Tags</b>\n\n"
        "Space-separated keywords.\n"
        "Example: <code>registration payment summer 2026 cse fee</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )


async def addregpay_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_regpay(
        ud["regpay_uid"], ud["semester"],
        ud.get("files", []), tags, ud.get("_uploader_id", 0),
        thumbnail_url=ud.get("thumbnail_url"),
        cover_file_id=ud.get("cover_file_id")
    )

    r = await get_regpay(ud["regpay_uid"])
    await update.message.reply_text(
        f"✅ <b>Registered!</b>\n\n{_regpay_summary(r)}", parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addregpay_conversation() -> ConversationHandler:
    done_cmd = CommandHandler("done", addregpay_files_done)
    return ConversationHandler(
        entry_points=[CommandHandler("addregpay", addregpay_start)],
        states={
            AR_SEMESTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, addregpay_semester)],
            AR_FILES: [
                done_cmd,
                MessageHandler(filters.Document.ALL | filters.PHOTO, addregpay_collect_file),
            ],
            AR_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addregpay_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addregpay_thumb_skip),
            ],
            AR_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, addregpay_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /editregpay <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editregpay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/editregpay &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    r   = await get_regpay(uid)
    if not r:
        await update.message.reply_text(
            f"❌ No entry found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_regpay_uid"] = uid
    context.user_data["edit_regpay"]     = r

    await update.message.reply_text(
        f"✏️ <b>Edit Registration & Payment Info</b>\n\n"
        f"{_regpay_summary(r)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return ER_MENU


async def editregpay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    action = query.data.replace("er_", "")
    await query.answer()

    uid = context.user_data["edit_regpay_uid"]
    r   = context.user_data["edit_regpay"]
    context.user_data["edit_field"] = action

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "files":
        context.user_data["new_files"] = []
        try:
            existing = json.loads(r["file_ids"]) if isinstance(r["file_ids"], str) else r.get("file_ids", [])
        except Exception:
            existing = []
        await query.message.edit_text(
            f"📄 <b>Replace Files</b>\n\n"
            f"Current: {len(existing)} file(s) — all will be replaced.\n\n"
            f"Send new files one by one, then <b>/done</b>.",
            parse_mode=HTML
        )
        return ER_FILES

    if action == "thumb":
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\nSend new image or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return ER_THUMB

    if action == "semester":
        await query.message.edit_text(
            f"🗓 <b>Edit Semester</b>\n\nCurrent: <code>{h(r['semester'])}</code>\n\n"
            f"Send new semester name:", parse_mode=HTML
        )
        return ER_VALUE

    if action == "tags":
        await query.message.edit_text(
            f"🏷 <b>Edit Tags</b>\n\nCurrent: <code>{_tag_str(r.get('tags', []))}</code>\n\n"
            f"Send new tags or <code>-</code> to clear:", parse_mode=HTML
        )
        return ER_VALUE

    return ER_MENU


async def editregpay_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data["edit_field"]
    uid   = context.user_data["edit_regpay_uid"]
    value = update.message.text.strip()

    if field == "tags":
        tags = [t.lower() for t in value.split() if t] if value != "-" else []
        await update_regpay_field(uid, "tags", json.dumps(tags))
    else:
        await update_regpay_field(uid, "semester", value)

    r = await get_regpay(uid)
    context.user_data["edit_regpay"] = r
    await update.message.reply_text(
        f"✅ Updated!\n\n{_regpay_summary(r)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return ER_MENU


async def editregpay_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("❌ Send a file.")
        return ER_FILES
    context.user_data.setdefault("new_files", []).append(
        {"file_id": file_id, "file_type": file_type}
    )
    count = len(context.user_data["new_files"])
    await msg.reply_text(f"✅ File {count} saved. Send more or /done.")
    return ER_FILES


async def editregpay_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = context.user_data.get("new_files", [])
    if not files:
        await update.message.reply_text("❌ Send at least one file first.")
        return ER_FILES

    uid = context.user_data["edit_regpay_uid"]
    await update_regpay_files(uid, files)

    r = await get_regpay(uid)
    context.user_data["edit_regpay"] = r
    await update.message.reply_text(
        f"✅ <b>{len(files)} file(s) replaced!</b>\n\n{_regpay_summary(r)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return ER_MENU


async def editregpay_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_regpay_uid"]

    if msg.text and msg.text.strip() == "-":
        await update_regpay_field(uid, "thumbnail_url", None)
        await update_regpay_field(uid, "cover_file_id", None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_regpay_field(uid, "thumbnail_url", thumb_url or None)
        await update_regpay_field(uid, "cover_file_id", fid)
        label = "Thumbnail updated!" if thumb_url else "Saved (imgBB failed)"
    else:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return ER_THUMB

    r = await get_regpay(uid)
    context.user_data["edit_regpay"] = r
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_regpay_summary(r)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return ER_MENU


def editregpay_conversation() -> ConversationHandler:
    done_cmd = CommandHandler("done", editregpay_files_done)
    return ConversationHandler(
        entry_points=[CommandHandler("editregpay", editregpay_start)],
        states={
            ER_MENU: [CallbackQueryHandler(editregpay_callback, pattern="^er_")],
            ER_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editregpay_value),
                CallbackQueryHandler(editregpay_callback, pattern="^er_"),
            ],
            ER_FILES: [
                done_cmd,
                MessageHandler(filters.Document.ALL | filters.PHOTO, editregpay_collect_file),
            ],
            ER_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    editregpay_thumb
                ),
                CallbackQueryHandler(editregpay_callback, pattern="^er_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deleteregpay <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deleteregpay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/deleteregpay &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    r   = await get_regpay(uid)
    if not r:
        await update.message.reply_text(
            f"❌ Not found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["delete_regpay_uid"] = uid
    await update.message.reply_text(
        f"⚠️ <b>Delete Registration & Payment Info</b>\n\n"
        f"{_regpay_summary(r)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm:</b>",
        parse_mode=HTML
    )
    return DR_CONFIRM


async def deleteregpay_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_regpay_uid"]
    if typed != uid:
        await update.message.reply_text(f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML)
        return DR_CONFIRM
    await delete_regpay(uid)
    await update.message.reply_text(
        f"🗑 <b>Deleted!</b> 🆔 <code>{h(uid)}</code>", parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def deleteregpay_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deleteregpay", deleteregpay_start)],
        states={DR_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deleteregpay_confirm)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled. 🦅") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listregpays
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listregpays_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_page(update, context, page=0, edit=False)


async def _show_page(update, context, page: int, edit: bool):
    total = await get_regpay_count()
    if total == 0:
        text = "📋 <b>Registration & Payment Info</b>\n\n<i>Nothing uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    items       = await get_regpay_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"📋 <b>Registration & Payment</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for r in items:
        try:
            files = json.loads(r["file_ids"]) if isinstance(r["file_ids"], str) else r.get("file_ids", [])
        except Exception:
            files = []
        lines.append(
            f"🆔 <code>{h(r['uid'])}</code>  🗓 {h(r['semester'])}  📄 {len(files)}f\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lrp_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lrp_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lrp_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    text = "\n".join(lines)
    if edit:
        await update.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


async def listregpays_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lrp_noop":
        await query.answer()
        return
    page = int(query.data.replace("lrp_page_", ""))
    await query.answer()
    await _show_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DELIVERY
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_regpay(chat_id: int, regpay_uid: str, bot):
    """Send all files to DM."""
    r = await get_regpay(regpay_uid)
    if not r:
        await bot.send_message(chat_id, "❌ Content not found.")
        return

    await increment_regpay_access(regpay_uid)
    await award_download(chat_id, "regpay", r.get("uploaded_by"), regpay_uid)

    try:
        files = json.loads(r["file_ids"]) if isinstance(r["file_ids"], str) else r.get("file_ids", [])
    except Exception:
        files = []

    if not files:
        await bot.send_message(chat_id, "⚠️ No files available.")
        return

    header = f"📋 Registration & Payment Info — {r['semester']}"
    await bot.send_message(chat_id, header)

    for i, f in enumerate(files):
        try:
            ftype = f.get("file_type", "document")
            cap   = f"📄 File {i + 1}/{len(files)}"
            if ftype == "photo":
                await bot.send_photo(chat_id, f["file_id"], caption=cap)
            else:
                await bot.send_document(chat_id, f["file_id"], caption=cap)
            if i < len(files) - 1:
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Failed to deliver regpay file {i}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# /sethelp — Admin sets help text
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def sethelp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = await get_setting("help_text")
    await update.message.reply_text(
        f"📖 <b>Set Help Text</b>\n\n"
        f"Current: {'✅ Set' if current else '— Not set'}\n\n"
        f"Send the new help message (formatted text preserved).\n"
        f"<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return SH_TEXT


async def sethelp_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg      = update.message
    text     = msg.text or ""
    entities = msg.entities or []

    entity_list = []
    for e in entities:
        entry = {"type": e.type.value if hasattr(e.type, "value") else str(e.type),
                 "offset": e.offset, "length": e.length}
        if e.url:      entry["url"]      = e.url
        if e.language: entry["language"] = e.language
        entity_list.append(entry)

    payload = json.dumps({"text": text, "entities": entity_list})
    await set_setting("help_text", payload)

    await msg.reply_text("✅ <b>Help text updated!</b>", parse_mode=HTML)
    return ConversationHandler.END


def sethelp_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("sethelp", sethelp_start)],
        states={SH_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sethelp_text)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PROFILE — show user stats
# ═══════════════════════════════════════════════════════════════════════════════

async def show_profile(chat_id: int, user_id: int, bot):
    """Send user profile stats to DM."""
    user = await queries.get_user(user_id)
    if not user:
        await bot.send_message(chat_id, "❌ Profile not found.")
        return

    joined = user.get("joined_at")
    joined_str = joined.strftime("%d %b %Y") if joined else "—"

    last_active = user.get("last_active")
    last_str = last_active.strftime("%d %b %Y") if last_active else "—"

    rank    = user.get("rank") or "Egg"
    points  = user.get("points") or 0
    streak  = user.get("streak_days") or 0
    dls     = user.get("download_count") or 0
    uploads = user.get("upload_count") or 0
    uname   = f"@{user['username']}" if user.get("username") else "—"

    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🏷 Name: {h(user.get('full_name') or '—')}\n"
        f"🔗 Username: {uname}\n\n"
        f"🥇 Rank: <b>{h(rank)}</b>\n"
        f"⭐ Points: <b>{points}</b>\n"
        f"🔥 Streak: {streak} day(s)\n\n"
        f"📥 Downloads: {dls}\n"
        f"📤 Uploads: {uploads}\n\n"
        f"📅 Joined: {joined_str}\n"
        f"🕐 Last Active: {last_str}"
    )
    await bot.send_message(chat_id, text, parse_mode=HTML)


# ═══════════════════════════════════════════════════════════════════════════════
# HELP — send help text
# ═══════════════════════════════════════════════════════════════════════════════

async def send_help(chat_id: int, bot):
    """Send admin-set help text to DM."""
    raw = await get_setting("help_text")
    if not raw:
        await bot.send_message(
            chat_id,
            "❓ <b>Help</b>\n\nNo help text set yet.\n"
            "Search resources: <code>@drcrow_bot your query</code>",
            parse_mode=HTML
        )
        return

    try:
        from telegram import MessageEntity
        from telegram import LinkPreviewOptions
        data     = json.loads(raw)
        text     = data.get("text", "")
        ent_data = data.get("entities", [])

        tg_entities = []
        for e in ent_data:
            try:
                tg_entities.append(MessageEntity(
                    type=e["type"], offset=e["offset"], length=e["length"],
                    url=e.get("url"), language=e.get("language")
                ))
            except Exception:
                pass

        await bot.send_message(
            chat_id, text,
            entities=tg_entities if tg_entities else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logger.error(f"Failed to send help: {e}")
        await bot.send_message(chat_id, "⚠️ Failed to load help text.")