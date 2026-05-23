"""
PSQ (Previous Semester Questions) CRUD handler.
Commands: /addpsq <uid>, /editpsq <uid>, /deletepsq <uid>, /listpsqs
Admin only.

PSQ = single merged PDF of all courses' questions.
Metadata: uid, file, tags (admin + system), cover (optional)
System auto-tags: psq previous questions
"""

import logging
import re as _re
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
from database.psq_queries import (
    psq_uid_exists, insert_psq, get_psq,
    update_psq_title, update_psq_tags, update_psq_file, update_psq_cover,
    delete_psq, get_psqs_paginated, get_psqs_count,
    increment_psq_access, SYSTEM_TAGS
)
from utils.imgbb import upload_to_imgbb

logger = logging.getLogger(__name__)
HTML  = ParseMode.HTML
PAGE_SIZE = 5

# ── States: /addpsq ─────────────────────────────────────────────────────────────
AP_FILE, AP_COVER = range(2)

# ── States: /editpsq ────────────────────────────────────────────────────────────
EP_MENU, EP_VALUE, EP_FILE = range(3)

# ── States: /deletepsq ──────────────────────────────────────────────────────────
DP_CONFIRM = 0

EDIT_FIELDS = {
    "title": ("📌", "Title"),
    "tags":  ("🏷", "Tags"),
    "file":  ("📄", "PDF File"),
    "cover": ("🖼", "Cover Image"),
}


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not settings.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _tag_display(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _psq_summary(psq: dict) -> str:
    title = psq.get("title") or "—"
    return (
        f"📋 <b>PSQ</b>\n"
        f"📌 {h(title)}\n"
        f"🆔 <code>{h(psq['uid'])}</code>\n"
        f"🏷 {h(_tag_display(psq.get('tags', [])))}\n"
        f"🖼 Cover: {'✅' if psq.get('cover_url') else '—'}"
    )


def _edit_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in EDIT_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"ep_field_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="ep_cancel")])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# /addpsq <uid>
# ═══════════════════════════════════════════════════════════════════════════════

async def _psq_generate_uid() -> str:
    """Generate UID: psq + zero-padded serial. e.g. psq01"""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid FROM psqs WHERE uid LIKE 'psq%'")
    max_serial = 0
    pattern = _re.compile(r'^psq(\d+)$')
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"psq{max_serial + 1:02d}"


def _title_from_filename(filename: str) -> str:
    """Extract clean title from filename."""
    name = filename.rsplit(".", 1)[0]          # remove extension
    name = _re.sub(r"[_\-]+", " ", name)      # underscores/dashes → space
    name = _re.sub(r"\s+", " ", name).strip()
    return name


def _psq_auto_tags(title: str) -> list[str]:
    tags = {"psq", "previous", "questions"}
    words = _re.sub(r"[^a-z0-9\s]", " ", title.lower()).split()
    tags.update(w for w in words if len(w) > 1)
    return sorted(tags)


async def addpsq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add PSQ</b>\n\n"
        "<i>Step 1 of 2</i>\n\n"
        "Send the merged questions PDF.",
        parse_mode=HTML
    )
    return AP_FILE


async def addpsq_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.document:
        await msg.reply_text("Please send a PDF file.")
        return AP_FILE

    file_id  = msg.document.file_id
    filename = msg.document.file_name or "PSQ"
    title    = _title_from_filename(filename)
    auto_tags = _psq_auto_tags(title)
    uid      = await _psq_generate_uid()

    context.user_data.update({
        "file_id":   file_id,
        "title":     title,
        "uid":       uid,
        "auto_tags": auto_tags,
    })

    await msg.reply_text(
        f"<i>Step 2 of 2</i>\n\n"
        f"<b>Auto title:</b> {h(title)}\n"
        f"<code>UID: {uid}</code>\n\n"
        f"Send cover image (JPG/PNG), or <code>-</code> to skip.\n"
        f"To change the title, send it as text first, then the cover.",
        parse_mode=HTML
    )
    return AP_COVER


async def addpsq_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud  = context.user_data

    # Check if it's a title override (text that is not "-")
    if msg.text:
        text = msg.text.strip()
        if text == "-":
            ud["cover_file_id"] = None
            ud["cover_url"]     = None
            return await _finalize_addpsq(msg, context)
        else:
            # Treat as title override
            ud["title"]    = text
            ud["auto_tags"] = _psq_auto_tags(text)
            await msg.reply_text(
                f"Title updated: <b>{h(text)}</b>\n\n"
                f"Now send cover image or <code>-</code> to skip.",
                parse_mode=HTML
            )
            return AP_COVER

    if msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
        fid       = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        cover_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        ud["cover_file_id"] = fid
        ud["cover_url"]     = cover_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code> to skip.", parse_mode=HTML)
        return AP_COVER

    return await _finalize_addpsq(msg, context)


async def _finalize_addpsq(msg, context):
    ud        = context.user_data
    uid       = ud["uid"]
    file_id   = ud["file_id"]
    title     = ud["title"]
    tags      = ud.get("auto_tags", [])
    cover_fid = ud.get("cover_file_id")
    cover_url = ud.get("cover_url")
    user_id   = ud.get("_uploader_id", 0)

    from database.queries import get_current_semester
    sem    = await get_current_semester()
    sem_id = sem["id"] if sem else None

    await insert_psq(
        uid, title, file_id, tags, user_id,
        semester_id=sem_id,
        cover_file_id=cover_fid,
        cover_url=cover_url
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Edit",   callback_data=f"psq_edit_{uid}"),
        InlineKeyboardButton("Delete", callback_data=f"psq_delete_{uid}"),
    ]])

    sem_name = h(sem["name"]) if sem else "—"
    tag_str  = " ".join([f"#{t}" for t in tags]) or "none"

    await msg.reply_text(
        f"<b>PSQ added.</b>\n\n"
        f"<b>{h(title)}</b>\n\n"
        f"<code>UID      : {h(uid)}\n"
        f"Semester : {sem_name}</code>\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML,
        reply_markup=keyboard
    )
    context.user_data.clear()
    return ConversationHandler.END



def addpsq_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addpsq", addpsq_start),
            CallbackQueryHandler(addpsq_start, pattern="^adm_add_psq$"),
        ],
        states={
            AP_FILE: [
                MessageHandler(filters.Document.ALL, addpsq_file)
            ],
            AP_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addpsq_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addpsq_cover),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )



# ═══════════════════════════════════════════════════════════════════════════════
# /editpsq <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editpsq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/editpsq &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    psq = await get_psq(uid)

    if not psq:
        await update.message.reply_text(
            f"❌ No PSQ found with UID <code>{h(uid)}</code>.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_psq_uid"] = uid
    context.user_data["edit_psq"]     = psq

    await update.message.reply_text(
        f"✏️ <b>Edit PSQ</b>\n\n{_psq_summary(psq)}\n\nWhat to edit?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EP_MENU


async def editpsq_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data == "ep_cancel":
        await query.answer()
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "ep_back":
        await query.answer()
        psq = await get_psq(context.user_data["edit_psq_uid"])
        context.user_data["edit_psq"] = psq
        await query.message.edit_text(
            f"✏️ <b>Edit PSQ</b>\n\n{_psq_summary(psq)}\n\nWhat to edit?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EP_MENU

    if data.startswith("ep_field_"):
        field = data.replace("ep_field_", "")
        context.user_data["edit_field"] = field
        psq   = context.user_data["edit_psq"]
        await query.answer()

        if field == "title":
            current = psq.get("title") or "N/A"
            await query.message.edit_text(
                f"📌 <b>Edit Title</b>\n\n"
                f"Current: <code>{h(current)}</code>\n\n"
                f"Send new title or <code>-</code> to remove:",
                parse_mode=HTML
            )
            return EP_VALUE

        if field == "tags":
            sys_tag_str = " ".join([f"#{t}" for t in SYSTEM_TAGS])
            current_admin = [t for t in (json.loads(psq["tags"]) if isinstance(psq["tags"], str)
                             else psq.get("tags", [])) if t not in SYSTEM_TAGS]
            await query.message.edit_text(
                f"🏷 <b>Edit Tags</b>\n\n"
                f"System tags (fixed): <code>{sys_tag_str}</code>\n"
                f"Current admin tags: <code>{' '.join(current_admin) or 'none'}</code>\n\n"
                f"Send new admin tags (space-separated) or <code>-</code> to clear:",
                parse_mode=HTML
            )
            return EP_VALUE

        if field == "file":
            await query.message.edit_text(
                "📄 <b>Replace PDF</b>\n\nSend the new PDF file:\n<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EP_FILE

        if field == "cover":
            await query.message.edit_text(
                "🖼 <b>Update Cover</b>\n\nSend new cover image or <code>-</code> to remove:",
                parse_mode=HTML
            )
            return EP_FILE

    return EP_MENU


async def editpsq_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = context.user_data["edit_psq_uid"]
    field = context.user_data.get("edit_field")
    value = update.message.text.strip()

    if field == "title":
        await update_psq_title(uid, None if value == "-" else value)
        label = "Title updated!"
    else:
        # tags
        tags = [t for t in value.lower().split() if t] if value != "-" else []
        await update_psq_tags(uid, tags)
        label = "Tags updated!"

    psq = await get_psq(uid)
    context.user_data["edit_psq"] = psq
    await update.message.reply_text(
        f"✅ <b>{label}</b>\n\n{_psq_summary(psq)}\n\nEdit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EP_MENU


async def editpsq_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    uid   = context.user_data["edit_psq_uid"]
    field = context.user_data["edit_field"]

    if field == "cover":
        if msg.text and msg.text.strip() == "-":
            await update_psq_cover(uid, None, None)
            label = "Cover removed."
        elif msg.photo or (msg.document and msg.document.mime_type and
                           msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("⏳ Uploading cover...")
            cover_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            await update_psq_cover(uid, fid, cover_url or None)
            label = "Cover updated!" if cover_url else "Cover saved (imgBB failed)"
        else:
            await msg.reply_text(
                "❌ Send a cover image or <code>-</code> to remove.", parse_mode=HTML
            )
            return EP_FILE

        psq = await get_psq(uid)
        context.user_data["edit_psq"] = psq
        await msg.reply_text(
            f"✅ <b>{label}</b>\n\n{_psq_summary(psq)}\n\nEdit another field?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EP_MENU

    # PDF replace
    if not msg.document:
        await msg.reply_text("❌ Please send a PDF file.")
        return EP_FILE

    await update_psq_file(uid, msg.document.file_id)
    psq = await get_psq(uid)
    context.user_data["edit_psq"] = psq
    await msg.reply_text(
        f"✅ <b>PDF replaced!</b>\n\n{_psq_summary(psq)}\n\nEdit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EP_MENU


async def editpsq_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Edit cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def editpsq_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editpsq", editpsq_start)],
        states={
            EP_MENU: [CallbackQueryHandler(editpsq_menu_callback, pattern="^ep_")],
            EP_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editpsq_get_value),
                       CallbackQueryHandler(editpsq_menu_callback, pattern="^ep_")],
            EP_FILE: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    editpsq_get_file
                ),
                CallbackQueryHandler(editpsq_menu_callback, pattern="^ep_")
            ],
        },
        fallbacks=[CommandHandler("cancel", editpsq_cancel)],
        conversation_timeout=300,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletepsq <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletepsq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/deletepsq &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    psq = await get_psq(uid)

    if not psq:
        await update.message.reply_text(
            f"❌ No PSQ found with UID <code>{h(uid)}</code>.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["delete_psq_uid"] = uid
    context.user_data["delete_psq"]     = psq

    await update.message.reply_text(
        f"⚠️ <b>Delete PSQ</b>\n\n{_psq_summary(psq)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm deletion:</b>\n"
        f"<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DP_CONFIRM


async def deletepsq_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_psq_uid"]
    psq   = context.user_data["delete_psq"]

    if typed != uid:
        await update.message.reply_text(
            f"❌ UID doesn't match. Type <code>{h(uid)}</code>:", parse_mode=HTML
        )
        return DP_CONFIRM

    await delete_psq(uid)
    await update.message.reply_text(
        f"🗑 <b>PSQ deleted!</b>\n\n🆔 <code>{h(uid)}</code>",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


async def deletepsq_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Deletion cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def deletepsq_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletepsq", deletepsq_start)],
        states={
            DP_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletepsq_confirm)]
        },
        fallbacks=[CommandHandler("cancel", deletepsq_cancel)],
        conversation_timeout=120,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listpsqs — paginated
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listpsqs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_psqs_page(update, context, page=0, edit=False)


async def _show_psqs_page(update, context, page: int, edit: bool):
    total = await get_psqs_count()
    if total == 0:
        text = "📋 <b>PSQs</b>\n\n<i>No PSQs uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    psqs        = await get_psqs_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"📋 <b>PSQs</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for p in psqs:
        try:
            tags = json.loads(p["tags"]) if isinstance(p["tags"], str) else p.get("tags", [])
        except Exception:
            tags = []
        admin_tags = [t for t in tags if t not in SYSTEM_TAGS]
        tag_str = " ".join([f"#{t}" for t in admin_tags]) if admin_tags else "—"
        title = p.get("title") or "—"
        lines.append(
            f"🆔 <code>{h(p['uid'])}</code>  📌 {h(title)}\n"
            f"   🏷 {h(tag_str)}  🖼 {'✅' if p.get('cover_url') else '—'}\n"
        )

    text = "\n".join(lines)
    text += f"\n<i>System tags (all PSQs): #psq #previous #questions</i>"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lp_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lp_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lp_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None

    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )


async def listpsqs_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lp_noop":
        await query.answer()
        return
    page = int(query.data.replace("lp_page_", ""))
    await query.answer()
    await _show_psqs_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# PSQ DELIVERY — called from search handler
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_psq(chat_id: int, psq_uid: str, bot):
    """Send PSQ PDF to user DM."""
    psq = await get_psq(psq_uid)
    if not psq:
        await bot.send_message(chat_id, "❌ PSQ not found.")
        return

    await increment_psq_access(psq_uid)
    await award_download(chat_id, "psq", psq.get("uploaded_by"), psq_uid)

    try:
        tags = json.loads(psq["tags"]) if isinstance(psq["tags"], str) else psq.get("tags", [])
    except Exception:
        tags = []
    tag_str = " ".join([f"#{t}" for t in tags])

    title   = psq.get("title") or "Previous Semester Questions"
    caption = h(title)

    try:
        await bot.send_document(chat_id, psq["file_id"], caption=caption)
    except Exception as e:
        logger.error(f"Failed to deliver PSQ {psq_uid}: {e}")
        await bot.send_message(chat_id, "⚠️ Failed to send file. Please try again.")