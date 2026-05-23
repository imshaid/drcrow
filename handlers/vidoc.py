"""
Vidoc (Videos & Docs) CRUD handler.
Commands: /addvidoc <uid>, /editvidoc <uid>, /deletevidoc <uid>, /listvidocs
Admin only.

Flow:
  /addvidoc <uid>
  → Subject → Course Code → Messages (/done to finish) → Tags → Thumbnail → Done

  /editvidoc <uid>
  → [📝 Replace Messages] [🏷 Edit Tags] [🖼 Edit Thumbnail]
"""

import logging
from utils.stars import award_download
import json
import re
import asyncio
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from database.vidoc_queries import (
    vidoc_uid_exists, insert_vidoc, get_vidoc,
    update_vidoc_messages, update_vidoc_tags, update_vidoc_thumbnail,
    update_vidoc_metadata, delete_vidoc,
    get_vidocs_paginated, get_vidocs_count,
    increment_vidoc_access, search_vidocs
)
from utils.imgbb import upload_to_imgbb

logger    = logging.getLogger(__name__)
HTML      = ParseMode.HTML
PAGE_SIZE = 5

# ── States: /addvidoc ────────────────────────────────────────────────────────────
AV_SUBJECT, AV_COURSE, AV_MESSAGES, AV_TAGS, AV_THUMB = range(5)

# ── States: /editvidoc ───────────────────────────────────────────────────────────
EV_MENU, EV_MESSAGES, EV_TAGS, EV_THUMB = range(4)

# ── States: /deletevidoc ─────────────────────────────────────────────────────────
DV_CONFIRM = 0


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not settings.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _extract_youtube_id(text: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _youtube_thumbnail(url: str) -> str:
    """Get YouTube thumbnail URL from video URL."""
    vid_id = _extract_youtube_id(url)
    if vid_id:
        return f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg"
    return ""


def _tag_str(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _vidoc_summary(v: dict) -> str:
    try:
        msgs = json.loads(v["messages"]) if isinstance(v["messages"], str) else v.get("messages", [])
    except Exception:
        msgs = []
    return (
        f"🎬 <b>Videos & Docs</b>\n"
        f"📂 {h(v.get('subject') or 'N/A')}\n"
        f"📗 {h(v.get('course_code') or 'N/A')}\n"
        f"💬 {len(msgs)} message(s)\n"
        f"🏷 {h(_tag_str(v.get('tags', [])))}\n"
        f"🖼 Thumbnail: {'✅' if v.get('thumbnail_url') else '—'}\n"
        f"🆔 <code>{h(v['uid'])}</code>"
    )


def _edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 Replace Messages", callback_data="ev_messages"),
        InlineKeyboardButton("🏷 Edit Tags",        callback_data="ev_tags"),
        InlineKeyboardButton("🖼 Edit Thumbnail",   callback_data="ev_thumb"),
    ], [
        InlineKeyboardButton("✖ Cancel", callback_data="ev_cancel")
    ]])


# ═══════════════════════════════════════════════════════════════════════════════
# /addvidoc <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def addvidoc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/addvidoc &lt;uid&gt;</code>\n"
            "Example: <code>/addvidoc cse311vd01</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    if not uid.replace("-", "").isalnum():
        await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
        return ConversationHandler.END

    if await vidoc_uid_exists(uid):
        await update.message.reply_text(
            f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["vidoc_uid"]    = uid
    context.user_data["_uploader_id"] = update.effective_user.id

    await update.message.reply_text(
        f"🎬 <b>Add Videos & Docs</b>\n\n"
        f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
        f"Step 1/4 — <b>Subject</b>\n\n"
        f"Example: <code>Database Management Systems</code>\n"
        f"Send <code>-</code> to skip.\n\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return AV_SUBJECT


async def addvidoc_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["subject"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 2/4 — <b>Course Code</b>\n\n"
        "Example: <code>CSE311</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AV_COURSE


async def addvidoc_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["course_code"] = None if text == "-" else text.upper()
    context.user_data["messages"]    = []

    await update.message.reply_text(
        "Step 3/4 — <b>Messages</b>\n\n"
        "Send your messages one by one.\n"
        "Bot will deliver them exactly as you send.\n\n"
        "Send <b>/done</b> when finished.",
        parse_mode=HTML
    )
    return AV_MESSAGES


async def addvidoc_collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect each text message from admin — preserving formatting entities."""
    msg     = update.message
    text    = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []

    # Serialize entities to dicts for JSON storage
    entity_list = []
    for e in entities:
        entry = {
            "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
            "offset": e.offset,
            "length": e.length,
        }
        if e.url:       entry["url"]      = e.url
        if e.user:      entry["user_id"]  = e.user.id
        if e.language:  entry["language"] = e.language
        entity_list.append(entry)

    context.user_data["messages"].append({
        "type":     "text",
        "content":  text,
        "entities": entity_list
    })
    count = len(context.user_data["messages"])
    await update.message.reply_text(
        f"✅ Message {count} saved. Send more or /done to finish."
    )
    return AV_MESSAGES


async def addvidoc_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sent /done — move to tags step."""
    msgs = context.user_data.get("messages", [])
    if not msgs:
        await update.message.reply_text(
            "❌ No messages added yet. Send at least one message first."
        )
        return AV_MESSAGES

    await update.message.reply_text(
        f"✅ {len(msgs)} message(s) saved!\n\n"
        f"Step 4/4 — <b>Search Tags</b>\n\n"
        f"Space-separated keywords:\n"
        f"Example: <code>dbms final normalization cse311</code>\n"
        f"Send <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AV_TAGS


async def addvidoc_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    context.user_data["tags"] = tags

    await update.message.reply_text(
        "Last step — <b>Thumbnail</b>\n\n"
        "Send a YouTube link → bot extracts thumbnail automatically\n"
        "Or send an image directly\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AV_THUMB


async def addvidoc_thumb_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for thumbnail — YouTube URL or '-'."""
    text = update.message.text.strip()

    if text == "-":
        context.user_data["thumbnail_url"]  = None
        context.user_data["cover_file_id"]  = None
    else:
        thumb = _youtube_thumbnail(text)
        if thumb:
            context.user_data["thumbnail_url"] = thumb
            context.user_data["cover_file_id"] = None
            await update.message.reply_text(f"✅ YouTube thumbnail extracted!")
        else:
            await update.message.reply_text(
                "❌ Not a valid YouTube URL.\n"
                "Send a YouTube link, an image, or <code>-</code> to skip.",
                parse_mode=HTML
            )
            return AV_THUMB

    await _finalize_addvidoc(update.message, context)
    return ConversationHandler.END


async def addvidoc_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image upload for thumbnail."""
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and
        msg.document.mime_type and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image, YouTube URL, or <code>-</code>.", parse_mode=HTML)
        return AV_THUMB

    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()

    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid

    if not thumb_url:
        await msg.reply_text("⚠️ imgBB upload failed.")

    await _finalize_addvidoc(msg, context)
    return ConversationHandler.END


async def _finalize_addvidoc(msg, context):
    ud = context.user_data
    from database.queries import get_current_semester
    _sem = await get_current_semester()
    _sem_id = _sem["id"] if _sem else None
    await insert_vidoc(
        ud["vidoc_uid"], ud.get("subject"), ud.get("course_code"),
        ud.get("messages", []), ud.get("tags", []),
        ud.get("_uploader_id", 0),
        semester_id=_sem_id,
        thumbnail_url=ud.get("thumbnail_url"),
        cover_file_id=ud.get("cover_file_id")
    )
    v = await get_vidoc(ud["vidoc_uid"])
    await msg.reply_text(
        f"✅ <b>Videos & Docs registered!</b>\n\n{_vidoc_summary(v)}",
        parse_mode=HTML
    )
    context.user_data.clear()


def addvidoc_conversation() -> ConversationHandler:
    done_cmd = CommandHandler("done", addvidoc_done)
    return ConversationHandler(
        entry_points=[CommandHandler("addvidoc", addvidoc_start)],
        states={
            AV_SUBJECT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc_subject)],
            AV_COURSE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc_course)],
            AV_MESSAGES: [
                done_cmd,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc_collect_message),
            ],
            AV_TAGS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc_tags)],
            AV_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addvidoc_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc_thumb_url),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=600,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /editvidoc <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editvidoc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/editvidoc &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    v   = await get_vidoc(uid)
    if not v:
        await update.message.reply_text(
            f"❌ No vidoc found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_vidoc_uid"] = uid
    context.user_data["edit_vidoc"]     = v

    await update.message.reply_text(
        f"✏️ <b>Edit Videos & Docs</b>\n\n{_vidoc_summary(v)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EV_MENU


async def editvidoc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    await query.answer()

    if data == "ev_cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "ev_messages":
        context.user_data["new_messages"] = []
        await query.message.edit_text(
            "📝 <b>Replace Messages</b>\n\n"
            "Send new messages one by one.\n"
            "All existing messages will be replaced.\n\n"
            "Send <b>/done</b> when finished.",
            parse_mode=HTML
        )
        return EV_MESSAGES

    if data == "ev_tags":
        v = context.user_data["edit_vidoc"]
        try:
            cur = json.loads(v["tags"]) if isinstance(v["tags"], str) else v.get("tags", [])
        except Exception:
            cur = []
        await query.message.edit_text(
            f"🏷 <b>Edit Tags</b>\n\n"
            f"Current: <code>{' '.join(cur) or 'none'}</code>\n\n"
            f"Send new tags (space-separated) or <code>-</code> to clear:",
            parse_mode=HTML
        )
        return EV_TAGS

    if data == "ev_thumb":
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\n"
            "Send YouTube link → auto thumbnail\n"
            "Or send an image directly\n"
            "Send <code>-</code> to remove thumbnail:",
            parse_mode=HTML
        )
        return EV_THUMB

    return EV_MENU


async def editvidoc_collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg      = update.message
    text     = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []

    entity_list = []
    for e in entities:
        entry = {
            "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
            "offset": e.offset,
            "length": e.length,
        }
        if e.url:       entry["url"]      = e.url
        if e.user:      entry["user_id"]  = e.user.id
        if e.language:  entry["language"] = e.language
        entity_list.append(entry)

    context.user_data.setdefault("new_messages", []).append({
        "type":     "text",
        "content":  text,
        "entities": entity_list
    })
    count = len(context.user_data["new_messages"])
    await update.message.reply_text(f"✅ Message {count} saved. Send more or /done.")
    return EV_MESSAGES


async def editvidoc_messages_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = context.user_data.get("new_messages", [])
    if not msgs:
        await update.message.reply_text("❌ No messages added yet.")
        return EV_MESSAGES

    uid = context.user_data["edit_vidoc_uid"]
    await update_vidoc_messages(uid, msgs)

    v = await get_vidoc(uid)
    context.user_data["edit_vidoc"] = v
    await update.message.reply_text(
        f"✅ <b>Messages replaced!</b> ({len(msgs)} message(s))\n\n"
        f"{_vidoc_summary(v)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EV_MENU


async def editvidoc_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    uid  = context.user_data["edit_vidoc_uid"]
    await update_vidoc_tags(uid, tags)

    v = await get_vidoc(uid)
    context.user_data["edit_vidoc"] = v
    await update.message.reply_text(
        f"✅ <b>Tags updated!</b>\n\n{_vidoc_summary(v)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EV_MENU


async def editvidoc_thumb_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = context.user_data["edit_vidoc_uid"]

    if text == "-":
        await update_vidoc_thumbnail(uid, None, None)
        label = "Thumbnail removed."
    else:
        thumb = _youtube_thumbnail(text)
        if thumb:
            await update_vidoc_thumbnail(uid, thumb, None)
            label = "YouTube thumbnail updated!"
        else:
            await update.message.reply_text(
                "❌ Not a valid YouTube URL. Send YouTube link, image, or <code>-</code>.",
                parse_mode=HTML
            )
            return EV_THUMB

    v = await get_vidoc(uid)
    context.user_data["edit_vidoc"] = v
    await update.message.reply_text(
        f"✅ <b>{label}</b>\n\n{_vidoc_summary(v)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EV_MENU


async def editvidoc_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_vidoc_uid"]
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and
        msg.document.mime_type and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image, YouTube URL, or <code>-</code>.", parse_mode=HTML)
        return EV_THUMB

    uploading = await msg.reply_text("⏳ Uploading...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    await update_vidoc_thumbnail(uid, thumb_url or None, fid)

    v = await get_vidoc(uid)
    context.user_data["edit_vidoc"] = v
    label = "Thumbnail updated!" if thumb_url else "Thumbnail saved (imgBB failed)"
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_vidoc_summary(v)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard()
    )
    return EV_MENU


def editvidoc_conversation() -> ConversationHandler:
    done_cmd = CommandHandler("done", editvidoc_messages_done)
    return ConversationHandler(
        entry_points=[CommandHandler("editvidoc", editvidoc_start)],
        states={
            EV_MENU: [CallbackQueryHandler(editvidoc_callback, pattern="^ev_")],
            EV_MESSAGES: [
                done_cmd,
                MessageHandler(filters.TEXT & ~filters.COMMAND, editvidoc_collect_message),
            ],
            EV_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, editvidoc_tags)],
            EV_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, editvidoc_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editvidoc_thumb_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=600,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletevidoc <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletevidoc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/deletevidoc &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    v   = await get_vidoc(uid)
    if not v:
        await update.message.reply_text(
            f"❌ No vidoc found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["delete_vidoc_uid"] = uid
    context.user_data["delete_vidoc"]     = v

    await update.message.reply_text(
        f"⚠️ <b>Delete Videos & Docs</b>\n\n{_vidoc_summary(v)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm:</b>\n<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DV_CONFIRM


async def deletevidoc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_vidoc_uid"]

    if typed != uid:
        await update.message.reply_text(
            f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML
        )
        return DV_CONFIRM

    await delete_vidoc(uid)
    await update.message.reply_text(
        f"🗑 <b>Deleted!</b> 🆔 <code>{h(uid)}</code>", parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def deletevidoc_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletevidoc", deletevidoc_start)],
        states={
            DV_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletevidoc_confirm)]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listvidocs — paginated
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listvidocs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_vidocs_page(update, context, page=0, edit=False)


async def _show_vidocs_page(update, context, page: int, edit: bool):
    total = await get_vidocs_count()
    if total == 0:
        text = "🎬 <b>Videos & Docs</b>\n\n<i>Nothing uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    vidocs      = await get_vidocs_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"🎬 <b>Videos & Docs</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for v in vidocs:
        try:
            msgs = json.loads(v["messages"]) if isinstance(v["messages"], str) else v.get("messages", [])
        except Exception:
            msgs = []
        course = v.get("course_code") or "—"
        lines.append(
            f"🆔 <code>{h(v['uid'])}</code>  💬 {len(msgs)}msg  🖼 {'✅' if v.get('thumbnail_url') else '—'}\n"
            f"   📗 {h(course)}  📂 {h(v.get('subject') or '—')}\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lv_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lv_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lv_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    text = "\n".join(lines)

    if edit:
        await update.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


async def listvidocs_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lv_noop":
        await query.answer()
        return
    page = int(query.data.replace("lv_page_", ""))
    await query.answer()
    await _show_vidocs_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DELIVERY — called from search handler
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_vidoc(chat_id: int, vidoc_uid: str, bot):
    """Deliver all messages to user DM exactly as stored."""
    v = await get_vidoc(vidoc_uid)
    if not v:
        await bot.send_message(chat_id, "❌ Content not found.")
        return

    await increment_vidoc_access(vidoc_uid)
    await award_download(chat_id, "vidoc", v.get("uploaded_by"), vidoc_uid)

    try:
        messages = json.loads(v["messages"]) if isinstance(v["messages"], str) else v.get("messages", [])
    except Exception:
        messages = []

    if not messages:
        await bot.send_message(chat_id, "⚠️ No content available for this collection.")
        return

    from telegram import MessageEntity

    for i, msg in enumerate(messages):
        try:
            text        = msg.get("content", "")
            entity_data = msg.get("entities", [])

            # Reconstruct MessageEntity objects
            tg_entities = []
            for e in entity_data:
                try:
                    tg_entities.append(MessageEntity(
                        type=e["type"],
                        offset=e["offset"],
                        length=e["length"],
                        url=e.get("url"),
                        language=e.get("language"),
                    ))
                except Exception:
                    pass  # Skip malformed entities

            from telegram import LinkPreviewOptions
            await bot.send_message(
                chat_id,
                text,
                entities=tg_entities if tg_entities else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            if i < len(messages) - 1:
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Failed to deliver vidoc message {i} for {vidoc_uid}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# NEW ADD FLOW — auto UID, info parsing, message collection
# ═══════════════════════════════════════════════════════════════════════════════

AV2_INFO, AV2_MESSAGES, AV2_COVER, AV2_TAGS = range(4)

_STOP_VD = {"a","an","the","of","and","or","for","in","to","with","on","at","by",
            "from","as","is","it","its","and","video","doc","docs","videos"}


async def _vidoc_generate_uid(abbr: str) -> str:
    """Generate UID: {abbr}{serial:02d}vd — e.g. cn01vd, dbms02vd"""
    import re as _re
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid FROM vidocs")
    max_n = 0
    pat = _re.compile(rf"^{_re.escape(abbr.lower())}(\d+)vd$")
    for row in rows:
        m = pat.match(row["uid"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{abbr.lower()}{max_n + 1:02d}vd"


@admin_only
async def addvidoc2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    context.user_data["_vd_messages"]     = []
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Videos & Docs</b>\n\n"
        "<i>Step 1 of 4</i>\n\n"
        "Send course code, or <code>-</code> to skip:\n"
        "<code>CSE315</code>",
        parse_mode=HTML
    )
    return AV2_INFO


async def addvidoc2_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "-":
        context.user_data["course_code"] = None
        context.user_data["subject"]     = None
        context.user_data["abbr"]        = "vd"
        context.user_data["auto_tags"]   = ["vidoc", "video", "document", "link", "playlist"]
    else:
        code = text.upper()
        context.user_data["course_code"] = code

        # Fetch course info from current semester
        from database.queries import get_current_semester
        import json as _j
        sem = await get_current_semester()
        courses = _j.loads(sem["courses"]) if sem and isinstance(sem["courses"], str) else (sem["courses"] if sem else [])
        course_map = {c["code"].upper(): c for c in (courses or [])}
        info = course_map.get(code, {})

        subject = info.get("name") or code
        abbr    = info.get("abbr") or re.sub(r"[^a-z]", "", code.lower())[:4] or "vd"

        context.user_data["subject"] = subject
        context.user_data["abbr"]    = abbr

        # Auto tags: course code + abbr + subject words + vidoc
        tags = {"vidoc", "video", "document", "link", "playlist", code.lower(), abbr.lower()}
        for w in re.sub(r"[^a-z0-9\s]", " ", subject.lower()).split():
            if w not in _STOP_VD and len(w) > 1:
                tags.add(w)
        context.user_data["auto_tags"] = sorted(tags)

    await update.message.reply_text(
        "<i>Step 2 of 4</i>\n\n"
        "Send messages one by one — text, links, files.\n"
        "Bot will deliver them exactly as you send.\n\n"
        "Send /done when finished.",
        parse_mode=HTML
    )
    return AV2_MESSAGES


async def addvidoc2_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg      = update.message
    messages = context.user_data.get("_vd_messages", [])

    if msg.document or msg.photo or msg.video or msg.audio:
        # File message
        if msg.photo:
            fid, ftype = msg.photo[-1].file_id, "photo"
        elif msg.document:
            fid, ftype = msg.document.file_id, "document"
        elif msg.video:
            fid, ftype = msg.video.file_id, "video"
        elif msg.audio:
            fid, ftype = msg.audio.file_id, "audio"
        else:
            fid, ftype = None, None

        if fid:
            messages.append({"type": ftype, "file_id": fid, "file_type": ftype})
    else:
        # Text message — preserve entities
        text     = msg.text or msg.caption or ""
        entities = msg.entities or msg.caption_entities or []
        entity_list = []
        for e in entities:
            entry = {
                "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
                "offset": e.offset,
                "length": e.length,
            }
            if e.url:      entry["url"]      = e.url
            if e.language: entry["language"] = e.language
            entity_list.append(entry)
        messages.append({"type": "text", "content": text, "entities": entity_list})

    context.user_data["_vd_messages"] = messages
    await msg.reply_text(f"Message {len(messages)} saved. Send more or /done to finish.")
    return AV2_MESSAGES


async def addvidoc2_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = context.user_data.get("_vd_messages", [])
    if not msgs:
        await update.message.reply_text("No messages yet. Send at least one first.")
        return AV2_MESSAGES

    await update.message.reply_text(
        "<i>Step 3 of 4</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AV2_COVER


async def addvidoc2_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        return AV2_COVER

    auto_tags = context.user_data.get("auto_tags", ["vidoc"])
    await msg.reply_text(
        f"<i>Step 4 of 4</i>\n\n"
        f"<b>Auto tags:</b> <code>{h(' '.join(auto_tags))}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return AV2_TAGS


async def addvidoc2_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return AV2_TAGS


async def addvidoc2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud       = context.user_data
    tags     = ud.get("custom_tags") or ud.get("auto_tags", ["vidoc"])
    abbr     = ud.get("abbr") or "vd"
    uid      = await _vidoc_generate_uid(abbr)
    messages = ud.get("_vd_messages", [])

    from database.queries import get_current_semester
    sem    = await get_current_semester()
    sem_id = sem["id"] if sem else None

    await insert_vidoc(
        uid, ud.get("subject"), ud.get("course_code"),
        messages, tags, ud.get("_uploader_id", 0),
        semester_id=sem_id,
        thumbnail_url=ud.get("thumbnail_url"),
        cover_file_id=ud.get("cover_file_id"),
    )

    tag_str = " ".join([f"#{t}" for t in tags])
    course  = ud.get("course_code") or "—"
    subject = ud.get("subject") or "—"

    await update.message.reply_text(
        f"<b>Vidoc added.</b>\n\n"
        f"<code>UID      : {h(uid)}\n"
        f"Course   : {h(course)}\n"
        f"Subject  : {h(subject)}\n"
        f"Messages : {len(messages)}</code>\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addvidoc2_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addvidoc", addvidoc2_start),
            CallbackQueryHandler(addvidoc2_start, pattern="^adm_add_vidoc$"),
        ],
        states={
            AV2_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc2_info)],
            AV2_MESSAGES: [
                CommandHandler("done", addvidoc2_done),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND |
                    filters.Document.ALL | filters.PHOTO |
                    filters.VIDEO | filters.AUDIO,
                    addvidoc2_collect
                ),
            ],
            AV2_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addvidoc2_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc2_cover),
            ],
            AV2_TAGS: [
                CommandHandler("done", addvidoc2_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addvidoc2_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )