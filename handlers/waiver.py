"""
Waiverulator CRUD + Interactive calculation.
Commands: /addwaiver <uid>, /editwaiver <uid>, /deletewaiver <uid>, /listwaivers

Member flow (DM):
  Click inline result → PDF + URL sent → bot asks waiver % → asks registration paid
  → sends calculation breakdown
"""

import logging
from utils.stars import award_download
import json
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from database.waiver_queries import (
    waiver_uid_exists, insert_waiver, get_waiver,
    update_waiver_field, delete_waiver,
    get_waivers_paginated, get_waivers_count,
    increment_waiver_access, search_waivers
)
from utils.imgbb import upload_to_imgbb

logger    = logging.getLogger(__name__)
HTML      = ParseMode.HTML
PAGE_SIZE = 5

# ── Add states ──────────────────────────────────────────────────────────────────
AW_FILE, AW_URL, AW_URL_TITLE, AW_SEMESTER, AW_TUITION, AW_SEMESTER_FEE, AW_THUMB, AW_TAGS = range(8)

# ── Edit states ─────────────────────────────────────────────────────────────────
EW_MENU, EW_VALUE, EW_FILE, EW_THUMB = range(4)

# ── Delete state ────────────────────────────────────────────────────────────────
DW_CONFIRM = 0

# ── Calculator states (member DM) ───────────────────────────────────────────────
CW_WAIVER_PCT, CW_REG_PAID = range(2)

EDIT_FIELDS = {
    "semester_name": ("🗓", "Semester Name"),
    "tuition_fee":   ("💵", "Tuition Fee"),
    "semester_fee":  ("💰", "Semester Fee"),
    "tags":          ("🏷", "Tags"),
    "file":          ("📄", "File"),
    "url":           ("🔗", "URL"),
    "cover":         ("🖼", "Thumbnail"),
}


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
        if "pdf"         in mime:                               return msg.document.file_id, "pdf"
        if "spreadsheet" in mime or fn.endswith((".xlsx",".xls")): return msg.document.file_id, "excel"
        if "presentation" in mime or fn.endswith(".pptx"):     return msg.document.file_id, "pptx"
        if "word"        in mime or fn.endswith(".docx"):      return msg.document.file_id, "docx"
        if mime.startswith("image"):                           return msg.document.file_id, "image_doc"
        return msg.document.file_id, "document"
    return None, None


def _tag_str(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _fmt(amount: int) -> str:
    """Format number with comma separators."""
    return f"৳{amount:,}"


def _waiver_summary(w: dict) -> str:
    return (
        f"🧮 <b>Waiverulator</b>\n"
        f"🗓 {h(w['semester_name'])}\n"
        f"🆔 <code>{h(w['uid'])}</code>\n"
        f"💵 Tuition Fee: {_fmt(w['tuition_fee'])}\n"
        f"💰 Semester Fee: {_fmt(w['semester_fee'])}\n"
        f"📄 File: {'✅' if w.get('file_id') else '—'}\n"
        f"🔗 URL: {'✅' if w.get('url') else '—'}\n"
        f"🖼 Thumbnail: {'✅' if w.get('thumbnail_url') else '—'}\n"
        f"🏷 {h(_tag_str(w.get('tags', [])))}"
    )


def _edit_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in EDIT_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"ew_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="ew_cancel")])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# /addwaiver <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def addwaiver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/addwaiver &lt;uid&gt;</code>\n"
            "Example: <code>/addwaiver waiver01</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    if not uid.replace("-", "").isalnum():
        await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
        return ConversationHandler.END

    if await waiver_uid_exists(uid):
        await update.message.reply_text(
            f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["waiver_uid"]   = uid
    context.user_data["_uploader_id"] = update.effective_user.id

    await update.message.reply_text(
        f"🧮 <b>Add Waiverulator</b>\n\n"
        f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
        f"Step 1/7 — <b>Policy File</b> (optional)\n\n"
        f"Send the waiver policy PDF or <code>-</code> to skip.\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return AW_FILE


async def addwaiver_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return AW_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "Step 2/7 — <b>Official URL</b> (optional)\n\n"
        "Link to the official waiver page or financial aid portal.\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AW_URL


async def addwaiver_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        context.user_data["url"]       = None
        context.user_data["url_title"] = None
        await _ask_semester(update.message)
        return AW_SEMESTER
    context.user_data["url"] = text
    await update.message.reply_text(
        "Step 2b — <b>URL Button Title</b>\n\n"
        "Example: <code>Official Financial Aid Portal</code>\n"
        "Send <code>-</code> to use URL as text.", parse_mode=HTML
    )
    return AW_URL_TITLE


async def addwaiver_url_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["url_title"] = None if text == "-" else text
    await _ask_semester(update.message)
    return AW_SEMESTER


async def _ask_semester(msg):
    await msg.reply_text(
        "Step 3/7 — <b>Semester Name</b>\n\n"
        "Example: <code>Summer 2026</code>  <code>Spring 2026</code>",
        parse_mode=HTML
    )


async def addwaiver_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["semester_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Step 4/7 — <b>Total Tuition Fee</b>\n\n"
        "Enter the full tuition fee amount (numbers only).\n"
        "Example: <code>15000</code>", parse_mode=HTML
    )
    return AW_TUITION


async def addwaiver_tuition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace("৳", "")
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number. Example: <code>15000</code>", parse_mode=HTML)
        return AW_TUITION
    context.user_data["tuition_fee"] = amount
    await update.message.reply_text(
        "Step 5/7 — <b>Total Semester Fee</b>\n\n"
        "Full semester fee (tuition + lab + library + all other fees).\n"
        "Example: <code>22000</code>", parse_mode=HTML
    )
    return AW_SEMESTER_FEE


async def addwaiver_semester_fee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace("৳", "")
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number.", parse_mode=HTML)
        return AW_SEMESTER_FEE
    context.user_data["semester_fee"] = amount
    await update.message.reply_text(
        "Step 6/7 — <b>Thumbnail</b> (optional)\n\n"
        "Send a cover image for inline search preview or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AW_THUMB


async def addwaiver_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return AW_THUMB
    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid
    await _ask_tags(msg)
    return AW_TAGS


async def addwaiver_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text("Send an image or <code>-</code>.", parse_mode=HTML)
        return AW_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_tags(update.message)
    return AW_TAGS


async def _ask_tags(msg):
    await msg.reply_text(
        "Step 7/7 — <b>Tags</b>\n\n"
        "Space-separated keywords.\n"
        "Example: <code>waiver fee summer 2026 cse</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )


async def addwaiver_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_waiver(
        ud["waiver_uid"], ud["semester_name"],
        ud["tuition_fee"], ud["semester_fee"],
        tags, ud.get("_uploader_id", 0),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        url=ud.get("url"), url_title=ud.get("url_title")
    )

    w = await get_waiver(ud["waiver_uid"])
    await update.message.reply_text(
        f"✅ <b>Waiverulator registered!</b>\n\n{_waiver_summary(w)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addwaiver_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addwaiver", addwaiver_start)],
        states={
            AW_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, addwaiver_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_file),
            ],
            AW_URL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_url)],
            AW_URL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_url_title)],
            AW_SEMESTER:      [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_semester)],
            AW_TUITION:       [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_tuition)],
            AW_SEMESTER_FEE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_semester_fee)],
            AW_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addwaiver_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_thumb_skip),
            ],
            AW_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /editwaiver <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editwaiver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/editwaiver &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid = args[0].strip().lower()
    w   = await get_waiver(uid)
    if not w:
        await update.message.reply_text(f"❌ No waiver found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["edit_waiver_uid"] = uid
    context.user_data["edit_waiver"]     = w

    await update.message.reply_text(
        f"✏️ <b>Edit Waiverulator</b>\n\n{_waiver_summary(w)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EW_MENU


async def editwaiver_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    data   = query.data
    action = data.replace("ew_", "")
    await query.answer()

    uid = context.user_data["edit_waiver_uid"]
    w   = context.user_data["edit_waiver"]
    context.user_data["edit_field"] = action

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "file":
        await query.message.edit_text(
            "📄 <b>Replace File</b>\n\nSend new file or <code>-</code> to remove:", parse_mode=HTML
        )
        return EW_FILE

    if action == "cover":
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\nSend new image or <code>-</code> to remove:", parse_mode=HTML
        )
        return EW_THUMB

    # Text/number fields
    emoji, label = EDIT_FIELDS.get(action, ("", action))
    current = w.get(action) or "N/A"
    if action in ("tuition_fee", "semester_fee"):
        current = _fmt(int(current)) if current != "N/A" else "N/A"
    elif action == "tags":
        current = _tag_str(current)

    await query.message.edit_text(
        f"{emoji} <b>Edit {label}</b>\n\n"
        f"Current: <code>{h(str(current))}</code>\n\n"
        f"Send new value or <code>-</code> to clear:", parse_mode=HTML
    )
    return EW_VALUE


async def editwaiver_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data["edit_field"]
    uid   = context.user_data["edit_waiver_uid"]
    text  = update.message.text.strip()

    if field == "tags":
        tags = [t.lower() for t in text.split() if t] if text != "-" else []
        await update_waiver_field(uid, "tags", json.dumps(tags))
    elif field in ("tuition_fee", "semester_fee"):
        val = text.replace(",", "").replace("৳", "")
        try:
            amount = int(val)
        except ValueError:
            await update.message.reply_text("❌ Enter a valid number.")
            return EW_VALUE
        await update_waiver_field(uid, field, amount)
    elif field == "url":
        await update_waiver_field(uid, "url", None if text == "-" else text)
    else:
        await update_waiver_field(uid, field, None if text == "-" else text)

    w = await get_waiver(uid)
    context.user_data["edit_waiver"] = w
    _, label = EDIT_FIELDS.get(field, ("", field))
    await update.message.reply_text(
        f"✅ <b>{label} updated!</b>\n\n{_waiver_summary(w)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EW_MENU


async def editwaiver_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_waiver_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_waiver_field(uid, "file_id", None)
        await update_waiver_field(uid, "file_type", None)
        label = "File removed."
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return EW_FILE
        await update_waiver_field(uid, "file_id", file_id)
        await update_waiver_field(uid, "file_type", file_type)
        label = f"File updated! ({file_type.upper()})"
    w = await get_waiver(uid)
    context.user_data["edit_waiver"] = w
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_waiver_summary(w)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EW_MENU


async def editwaiver_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_waiver_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_waiver_field(uid, "thumbnail_url", None)
        await update_waiver_field(uid, "cover_file_id", None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_waiver_field(uid, "thumbnail_url", thumb_url or None)
        await update_waiver_field(uid, "cover_file_id", fid)
        label = "Thumbnail updated!" if thumb_url else "Saved (imgBB failed)"
    else:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return EW_THUMB
    w = await get_waiver(uid)
    context.user_data["edit_waiver"] = w
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_waiver_summary(w)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EW_MENU


def editwaiver_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editwaiver", editwaiver_start)],
        states={
            EW_MENU: [CallbackQueryHandler(editwaiver_callback, pattern="^ew_")],
            EW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editwaiver_value),
                CallbackQueryHandler(editwaiver_callback, pattern="^ew_"),
            ],
            EW_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                    editwaiver_file
                ),
                CallbackQueryHandler(editwaiver_callback, pattern="^ew_"),
            ],
            EW_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    editwaiver_thumb
                ),
                CallbackQueryHandler(editwaiver_callback, pattern="^ew_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletewaiver <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletewaiver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/deletewaiver &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid = args[0].strip().lower()
    w   = await get_waiver(uid)
    if not w:
        await update.message.reply_text(f"❌ No waiver found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["delete_waiver_uid"] = uid
    await update.message.reply_text(
        f"⚠️ <b>Delete Waiverulator</b>\n\n{_waiver_summary(w)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm:</b>\n<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DW_CONFIRM


async def deletewaiver_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_waiver_uid"]
    if typed != uid:
        await update.message.reply_text(f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML)
        return DW_CONFIRM
    await delete_waiver(uid)
    await update.message.reply_text(
        f"🗑 <b>Deleted!</b> 🆔 <code>{h(uid)}</code>", parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def deletewaiver_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletewaiver", deletewaiver_start)],
        states={DW_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletewaiver_confirm)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled. 🦅") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listwaivers
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listwaivers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_waivers_page(update, context, page=0, edit=False)


async def _show_waivers_page(update, context, page: int, edit: bool):
    total = await get_waivers_count()
    if total == 0:
        text = "🧮 <b>Waiverulators</b>\n\n<i>Nothing uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    waivers     = await get_waivers_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"🧮 <b>Waiverulators</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for w in waivers:
        lines.append(
            f"🆔 <code>{h(w['uid'])}</code>  🗓 {h(w['semester_name'])}\n"
            f"   💵 {_fmt(w['tuition_fee'])}  💰 {_fmt(w['semester_fee'])}\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lw_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lw_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lw_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    if edit:
        await update.callback_query.edit_message_text("\n".join(lines), parse_mode=HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text("\n".join(lines), parse_mode=HTML, reply_markup=keyboard)


async def listwaivers_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lw_noop":
        await query.answer()
        return
    page = int(query.data.replace("lw_page_", ""))
    await query.answer()
    await _show_waivers_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DELIVERY — send file + URL, then start calculator conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_waiver(chat_id: int, waiver_uid: str, bot):
    """Send policy file + URL, then start interactive calculator."""
    w = await get_waiver(waiver_uid)
    if not w:
        await bot.send_message(chat_id, "❌ Waiverulator not found.")
        return

    await increment_waiver_access(waiver_uid)
    await award_download(chat_id, "waiver", w.get("uploaded_by"), waiver_uid)

    keyboard = None
    if w.get("url"):
        btn_title = w.get("url_title") or "🔗 Official Portal"
        keyboard  = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_title, url=w["url"])
        ]])

    # Send file if exists
    if w.get("file_id"):
        file_type = w.get("file_type", "document")
        caption   = f"📋 Waiver Policy — {w['semester_name']}"
        try:
            if file_type == "photo":
                await bot.send_photo(chat_id, w["file_id"], caption=caption, reply_markup=keyboard)
            else:
                await bot.send_document(chat_id, w["file_id"], caption=caption, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Failed to deliver waiver file {waiver_uid}: {e}")
    elif keyboard:
        await bot.send_message(
            chat_id,
            f"🧮 Waiverulator — {w['semester_name']}",
            reply_markup=keyboard
        )

    # Store waiver data in bot_data for calculator conversation
    # Use chat_id as key
    await bot.send_message(
        chat_id,
        f"🧮 <b>Waiverulator ({h(w['semester_name'])})</b>\n\n"
        f"What is your <b>waiver percentage</b>? (0–100)\n"
        f"Example: <code>25</code>",
        parse_mode=HTML
    )

    # Store pending calculation info
    import asyncio
    # We'll use a simple in-memory store via bot_data
    if not hasattr(bot, '_waiver_pending'):
        bot._waiver_pending = {}
    bot._waiver_pending[chat_id] = {
        "uid":          waiver_uid,
        "tuition_fee":  w["tuition_fee"],
        "semester_fee": w["semester_fee"],
        "semester":     w["semester_name"],
        "step":         "waiver_pct"
    }


async def deliver_waiver_files_only(chat_id: int, waiver_uid: str, bot):
    """Send policy file + URL only — no calculator prompt, no pending state.
    Used by the Reply KB Waiver flow in start.py."""
    w = await get_waiver(waiver_uid)
    if not w:
        return

    await increment_waiver_access(waiver_uid)

    keyboard = None
    if w.get("url"):
        btn_title = w.get("url_title") or "Official Portal"
        keyboard  = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_title, url=w["url"])
        ]])

    if w.get("file_id"):
        file_type = w.get("file_type", "document")
        caption   = f"Waiver Policy — {w['semester_name']}"
        try:
            if file_type == "photo":
                await bot.send_photo(chat_id, w["file_id"], caption=caption, reply_markup=keyboard)
            else:
                await bot.send_document(chat_id, w["file_id"], caption=caption, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"deliver_waiver_files_only failed for {waiver_uid}: {e}")
    elif keyboard:
        await bot.send_message(
            chat_id,
            f"Waiver Policy — {w['semester_name']}",
            reply_markup=keyboard
        )


async def handle_waiver_calc_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle Waiverulator messages in DM.
    Returns True if message was handled, False otherwise.
    Call this from dm_fallback before LLM.
    """
    msg  = update.message
    user = update.effective_user
    bot  = context.bot

    if not hasattr(bot, '_waiver_pending'):
        return False

    pending = bot._waiver_pending.get(user.id)
    if not pending:
        return False

    text = msg.text.strip() if msg.text else ""
    step = pending.get("step")

    if step == "waiver_pct":
        try:
            pct = float(text.replace("%", ""))
            if not (0 <= pct <= 100):
                raise ValueError
        except ValueError:
            await msg.reply_text(
                "❌ Enter a valid percentage between 0 and 100.\n"
                "Example: <code>25</code>", parse_mode=HTML
            )
            return True

        pending["waiver_pct"] = pct
        pending["step"]       = "reg_paid"
        bot._waiver_pending[user.id] = pending

        await msg.reply_text(
            f"✅ Waiver: <b>{pct}%</b>\n\n"
            f"How much did you pay during <b>registration</b>?\n"
            f"Example: <code>5000</code>",
            parse_mode=HTML
        )
        return True

    if step == "reg_paid":
        try:
            paid = int(text.replace(",", "").replace("৳", ""))
            if paid < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text(
                "❌ Enter a valid amount.\n"
                "Example: <code>5000</code>", parse_mode=HTML
            )
            return True

        # Calculate
        tuition     = pending["tuition_fee"]
        semester    = pending["semester_fee"]
        pct         = pending["waiver_pct"]
        sem_name    = pending["semester"]

        waiver_amt       = int(tuition * pct / 100)
        tuition_after    = tuition - waiver_amt
        other_fees       = semester - tuition
        semester_after   = other_fees + tuition_after
        remaining        = max(0, semester_after - paid)

        result = (
            f"🧮 <b>Waiverulator ({h(sem_name)})</b>\n\n"
            f"<b>Tuition Fee</b>\n"
            f"Total Tuition Fee:      {_fmt(tuition)}\n"
            f"Waiver ({pct:.0f}%):          -{_fmt(waiver_amt)}\n"
            f"Tuition after waiver:   {_fmt(tuition_after)}\n\n"
            f"<b>Semester Fee</b>\n"
            f"Total Semester Fee:     {_fmt(semester)}\n"
            f"Semester after waiver:  {_fmt(semester_after)}\n\n"
            f"✅ Paid at Registration: {_fmt(paid)}\n"
            f"💳 Remaining (before final): <b>{_fmt(remaining)}</b>"
        )

        await msg.reply_text(result, parse_mode=HTML)

        # Clear pending
        del bot._waiver_pending[user.id]
        return True

    return False

# ═══════════════════════════════════════════════════════════════════════════════
# NEW ADD FLOW — auto UID, single file, info parsing
# ═══════════════════════════════════════════════════════════════════════════════

AW2_FILE, AW2_INFO, AW2_COVER, AW2_TAGS = range(4)


async def _waiver_generate_uid() -> str:
    from database.db import get_pool
    import re as _re
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid FROM waivers")
    max_n = 0
    for row in rows:
        m = _re.match(r"^waiver(\d+)$", row["uid"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"waiver{max_n + 1:02d}"


def _parse_waiver_info(text: str) -> dict:
    """Parse info block:
    Numbers (int) → tuition_fee then semester_fee (in order)
    URL (http/https) → url
    Line after URL → url_title
    """
    import re as _re
    lines     = [l.strip() for l in text.strip().splitlines() if l.strip()]
    numbers   = []
    url       = None
    url_title = None
    url_idx   = None

    for i, line in enumerate(lines):
        if line.startswith("http://") or line.startswith("https://"):
            url     = line
            url_idx = i
        else:
            try:
                numbers.append(int(line.replace(",", "").replace("৳", "").replace(" ", "")))
            except ValueError:
                pass

    if url_idx is not None and url_idx + 1 < len(lines):
        next_line = lines[url_idx + 1]
        if not (next_line.startswith("http://") or next_line.startswith("https://")):
            try:
                int(next_line.replace(",", ""))
            except ValueError:
                url_title = next_line

    tuition_fee   = numbers[0] if len(numbers) > 0 else None
    semester_fee  = numbers[1] if len(numbers) > 1 else None

    return {
        "tuition_fee":  tuition_fee,
        "semester_fee": semester_fee,
        "url":          url,
        "url_title":    url_title,
    }


@admin_only
async def addwaiver2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Waiver</b>\n\n"
        "<i>Step 1 of 4</i>\n\n"
        "Send the waiver policy file (PDF, image, DOCX).",
        parse_mode=HTML
    )
    return AW2_FILE


async def addwaiver2_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("Please send a PDF, image, or document file.")
        return AW2_FILE

    context.user_data["file_id"]   = file_id
    context.user_data["file_type"] = file_type

    await msg.reply_text(
        "<i>Step 2 of 4</i>\n\n"
        "Send fee info (each on a new line):\n\n"
        "<code>45000\n"
        "65000\n"
        "https://payment.link\n"
        "View Payment Portal</code>\n\n"
        "Format:\n"
        "• 1st number → Tuition fee\n"
        "• 2nd number → Semester fee\n"
        "• URL → link button (optional)\n"
        "• Line after URL → button text (optional)\n\n"
        "Send <code>-</code> if no fees yet.",
        parse_mode=HTML
    )
    return AW2_INFO


async def addwaiver2_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "-":
        info = {"tuition_fee": None, "semester_fee": None, "url": None, "url_title": None}
    else:
        info = _parse_waiver_info(text)

    context.user_data.update(info)

    await update.message.reply_text(
        "<i>Step 3 of 4</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AW2_COVER


async def addwaiver2_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["cover_file_id"] = None
        context.user_data["thumbnail_url"] = None
    elif msg.photo or (msg.document and msg.document.mime_type and
                       msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        context.user_data["cover_file_id"] = fid
        context.user_data["thumbnail_url"] = thumb_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code>.", parse_mode=HTML)
        return AW2_COVER

    await msg.reply_text(
        "<i>Step 4 of 4</i>\n\n"
        "<b>Auto tags:</b> <code>waiver</code>\n\n"
        "Send your own tags to replace, then /done.\n"
        "Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return AW2_TAGS


async def addwaiver2_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return AW2_TAGS


async def addwaiver2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.queries import get_current_semester
    ud   = context.user_data
    tags = ud.get("custom_tags") or ["waiver"]
    uid  = await _waiver_generate_uid()

    sem          = await get_current_semester()
    sem_name     = sem["name"] if sem else "Unknown"
    tuition_fee  = ud.get("tuition_fee") or 0
    semester_fee = ud.get("semester_fee") or 0

    await insert_waiver(
        uid, sem_name, tuition_fee, semester_fee,
        tags, ud.get("_uploader_id", 0),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        url=ud.get("url"), url_title=ud.get("url_title"),
    )

    tag_str  = " ".join([f"#{t}" for t in tags])
    url_line = f"\nLink: {h(ud['url'])}" if ud.get("url") else ""

    await update.message.reply_text(
        f"<b>Waiver added.</b>\n\n"
        f"<code>UID          : {h(uid)}\n"
        f"Semester     : {h(sem_name)}\n"
        f"Tuition Fee  : {_fmt(tuition_fee)}\n"
        f"Semester Fee : {_fmt(semester_fee)}</code>"
        f"{url_line}\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addwaiver2_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addwaiver", addwaiver2_start),
            CallbackQueryHandler(addwaiver2_start, pattern="^adm_add_waiver$"),
        ],
        states={
            AW2_FILE: [MessageHandler(filters.Document.ALL | filters.PHOTO, addwaiver2_file)],
            AW2_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver2_info)],
            AW2_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addwaiver2_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver2_cover),
            ],
            AW2_TAGS: [
                CommandHandler("done", addwaiver2_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwaiver2_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )