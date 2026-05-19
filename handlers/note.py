"""
Note CRUD handler.
Commands: /addnote <uid>, /editnote <uid>, /deletenote <uid>, /listnotes
Admin only.
"""

import logging
import re as _re
from utils.stars import award_download
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from database.note_queries import (
    note_uid_exists, insert_note, get_note,
    update_note_field, update_note_file, update_note_cover,
    delete_note, get_notes_paginated, get_notes_count,
    increment_note_access
)
from database.queries import get_current_semester
from utils.imgbb import upload_to_imgbb

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

# ── File type detection ─────────────────────────────────────────────────────────
def _detect_file_type(msg) -> tuple:
    """Returns (file_id, file_type) or (None, None)."""
    if msg.document:
        mime = msg.document.mime_type or ""
        if "pdf" in mime:
            return msg.document.file_id, "pdf"
        elif "presentation" in mime or msg.document.file_name.endswith(".pptx"):
            return msg.document.file_id, "pptx"
        elif "word" in mime or msg.document.file_name.endswith(".docx"):
            return msg.document.file_id, "docx"
        elif mime.startswith("image"):
            return msg.document.file_id, "image"
        else:
            return msg.document.file_id, "document"
    elif msg.photo:
        return msg.photo[-1].file_id, "image"
    return None, None


# ── States: /addnote ────────────────────────────────────────────────────────────
AN_FILE, AN_INFO, AN_COVER, AN_TAGS = range(4)

# ── States: /editnote ───────────────────────────────────────────────────────────
EN_MENU, EN_VALUE, EN_FILE = range(3)

# ── States: /deletenote ─────────────────────────────────────────────────────────
DN_CONFIRM = 0

# ── Field config ────────────────────────────────────────────────────────────────
NOTE_FIELDS = {
    "title":       ("📌", "Title"),
    "subject":     ("📂", "Subject"),
    "course_code": ("📗", "Course Code"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "File"),
    "cover":       ("🖼", "Cover Image"),
}

PAGE_SIZE = 5


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not settings.is_admin(user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _note_summary(note: dict) -> str:
    import json
    tags_raw = note.get("tags", [])
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    tag_str = " ".join([f"#{t}" for t in tags]) if tags else "none"
    return (
        f"📝 <b>{h(note['title'])}</b>\n"
        f"📂 {h(note.get('subject') or 'N/A')}\n"
        f"📗 {h(note.get('course_code') or 'N/A')}\n"
        f"🏷 {h(tag_str)}\n"
        f"🆔 <code>{h(note['uid'])}</code>"
    )


def _edit_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in NOTE_FIELDS.items():
        row.append(InlineKeyboardButton(
            f"{emoji} {label}", callback_data=f"en_field_{key}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="en_cancel")])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# /addnote <uid>
# ═══════════════════════════════════════════════════════════════════════════════

STOP_WORDS = {
    "a","an","the","of","and","or","for","in","to","with","on","at","by","from",
    "as","is","it","its","be","are","was","were","been","has","have","had",
    "not","no","nor","but","so","yet","all","any","some","such","than","too",
}


async def _note_get_course_info(course_code: str) -> dict:
    """Fetch course name + abbr for given code from current semester."""
    if not course_code:
        return {"code": course_code, "name": "", "abbr": ""}
    code = course_code.strip().upper()
    sem  = await get_current_semester()
    if not sem:
        return {"code": code, "name": "", "abbr": ""}
    import json as _j
    courses = _j.loads(sem["courses"]) if isinstance(sem["courses"], str) else (sem["courses"] or [])
    for c in courses:
        if c.get("code", "").upper() == code:
            return {"code": code, "name": c.get("name", ""), "abbr": c.get("abbr", "")}
    return {"code": code, "name": "", "abbr": ""}


async def _note_generate_uid(abbr: str) -> str:
    """Generate UID: abbr + zero-padded serial + n. e.g. swe01n"""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT uid FROM notes WHERE uid LIKE $1", f"{abbr}%n"
        )
    max_serial = 0
    pattern = _re.compile(rf"^{_re.escape(abbr)}(\d+)n$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"{abbr}{max_serial + 1:02d}n"


def _note_parse_info(text: str) -> dict:
    """Parse:
    Line 1: Title (optional — if looks like a course code, treat as course only)
    Line 2 (or 1): Course code
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    # If single line and looks like course code (e.g. CSE315) → no title
    course_pattern = _re.compile(r'^[A-Za-z]{2,4}\d{3,4}$')
    if len(lines) == 1 and course_pattern.match(lines[0]):
        return {"title": None, "course_code": lines[0].upper()}
    elif len(lines) >= 2:
        return {"title": lines[0], "course_code": lines[1].upper() if course_pattern.match(lines[1]) else None}
    else:
        return {"title": lines[0] if lines else None, "course_code": None}


def _note_auto_tags(title: str | None, course_info: dict) -> list[str]:
    tags = set()
    tags.add("note")
    if course_info.get("code"):   tags.add(course_info["code"].lower())
    if course_info.get("abbr"):   tags.add(course_info["abbr"].lower())
    if course_info.get("name"):
        for w in course_info["name"].lower().split():
            if w not in STOP_WORDS and len(w) > 1:
                tags.add(w)
    if title:
        words = _re.sub(r"[^a-z0-9\s]", " ", title.lower()).split()
        tags.update(w for w in words if w not in STOP_WORDS and len(w) > 1)
    return sorted(tags)


async def addnote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Note</b>\n\n"
        "<i>Step 1 of 4</i>\n\n"
        "Send the note file.\n"
        "PDF, DOCX, PPTX, image — all accepted.",
        parse_mode=HTML
    )
    return AN_FILE


async def addnote_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("Please send a PDF, DOCX, PPTX, or image file.")
        return AN_FILE

    context.user_data["file_id"]   = file_id
    context.user_data["file_type"] = file_type

    await msg.reply_text(
        "<i>Step 2 of 4</i>\n\n"
        "Send the course code, and optionally a title on the first line:\n\n"
        "Course code only:\n"
        "<code>CSE315</code>\n\n"
        "With title:\n"
        "<code>Mid Term Notes\n"
        "CSE315</code>\n\n"
        "Send <code>-</code> if no course.",
        parse_mode=HTML
    )
    return AN_INFO


async def addnote_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "-":
        info = {"title": None, "course_code": None}
    else:
        info = _note_parse_info(text)

    course_info = await _note_get_course_info(info["course_code"] or "")
    abbr        = course_info["abbr"].lower() if course_info["abbr"] else "gen"
    uid         = await _note_generate_uid(abbr)

    # Auto title if not provided
    title = info["title"] or (
        f"{course_info['name']} Note" if course_info["name"] else "Note"
    )

    auto_tags = _note_auto_tags(info["title"], course_info)

    context.user_data.update({
        "uid":         uid,
        "title":       title,
        "course_code": info["course_code"],
        "course_info": course_info,
        "auto_tags":   auto_tags,
    })

    course_line = (
        f"{course_info['code']} — {course_info['name']} ({course_info['abbr']})"
        if course_info.get("name") else (info["course_code"] or "None")
    )

    await update.message.reply_text(
        f"<i>Step 3 of 4</i>\n\n"
        f"<b>Preview:</b>\n"
        f"UID: <code>{uid}</code>\n"
        f"Title: {h(title)}\n"
        f"Course: {h(course_line)}\n\n"
        f"Send cover image (JPG/PNG), or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AN_COVER


async def addnote_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg.text and msg.text.strip() == "-":
        context.user_data["cover_file_id"] = None
        context.user_data["cover_url"]     = None
    elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
        file_id   = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        cover_url = await upload_to_imgbb(context.bot, file_id)
        await uploading.delete()
        context.user_data["cover_file_id"] = file_id
        context.user_data["cover_url"]     = cover_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code> to skip.", parse_mode=HTML)
        return AN_COVER

    auto_tags = context.user_data.get("auto_tags", [])
    await msg.reply_text(
        f"<i>Step 4 of 4</i>\n\n"
        f"<b>Auto-generated tags:</b>\n"
        f"<code>{h(' '.join(auto_tags))}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return AN_TAGS


async def addnote_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return AN_TAGS


async def addnote_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called on /done — finalize and save."""
    ud   = context.user_data
    tags = ud.get("custom_tags") or ud.get("auto_tags", [])

    uid         = ud["uid"]
    title       = ud["title"]
    file_id     = ud["file_id"]
    file_type   = ud["file_type"]
    course_code = ud.get("course_code")
    cover_fid   = ud.get("cover_file_id")
    cover_url   = ud.get("cover_url")
    user_id     = ud.get("_uploader_id", 0)

    course_info = ud.get("course_info", {})
    subject     = course_info.get("name") or course_code or ""

    semester    = await get_current_semester()
    semester_id = semester["id"] if semester else None

    await insert_note(
        uid, title, subject, course_code, semester_id,
        file_id, file_type, tags, user_id,
        cover_file_id=cover_fid,
        cover_url=cover_url
    )

    from utils.notify import notify_resource
    await notify_resource(
        bot=update.get_bot(), category="note",
        course_code=course_code, title=title, uid=uid
    )

    sem_name      = h(semester["name"]) if semester else "—"
    tag_str       = " ".join([f"#{t}" for t in tags]) or "none"
    course_display = (
        f"{course_info['code']} ({course_info['abbr']})"
        if course_info.get("abbr") else (course_code or "—")
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Edit",   callback_data=f"note_edit_{uid}"),
        InlineKeyboardButton("Delete", callback_data=f"note_delete_{uid}"),
    ]])

    await update.message.reply_text(
        f"<b>Note added.</b>\n\n"
        f"<b>{h(title)}</b>\n\n"
        f"<code>UID      : {h(uid)}\n"
        f"Course   : {h(course_display)}\n"
        f"Semester : {sem_name}</code>\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML,
        reply_markup=keyboard
    )
    context.user_data.clear()
    return ConversationHandler.END



# ═══════════════════════════════════════════════════════════════════════════════
# /editnote <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editnote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/editnote &lt;uid&gt;</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid  = args[0].strip().lower()
    note = await get_note(uid)

    if not note:
        await update.message.reply_text(
            f"❌ No note found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_note_uid"] = uid
    context.user_data["edit_note"]     = note

    await update.message.reply_text(
        f"✏️ <b>Edit Note</b>\n\n"
        f"{_note_summary(note)}\n\n"
        f"Which field do you want to edit?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EN_MENU


async def editnote_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data == "en_cancel":
        await query.answer()
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "en_back":
        await query.answer()
        note = await get_note(context.user_data["edit_note_uid"])
        context.user_data["edit_note"] = note
        await query.message.edit_text(
            f"✏️ <b>Edit Note</b>\n\n"
            f"{_note_summary(note)}\n\n"
            f"Which field do you want to edit?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EN_MENU

    if data.startswith("en_field_"):
        field = data.replace("en_field_", "")
        context.user_data["edit_field"] = field
        note  = context.user_data["edit_note"]
        await query.answer()

        if field == "file":
            await query.message.edit_text(
                f"📄 <b>Replace File</b>\n\n"
                f"Current note: <b>{h(note['title'])}</b>\n\n"
                f"Send the new file (PDF, DOCX, PPTX, Image):\n"
                f"<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EN_FILE

        if field == "cover":
            await query.message.edit_text(
                f"🖼 <b>Update Cover Image</b>\n\n"
                f"Send new cover image (JPG/PNG) or <code>-</code> to remove:\n"
                f"<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EN_FILE

        import json
        current = note.get(field)
        if field == "tags":
            try:
                current = " ".join(json.loads(current)) if isinstance(current, str) else " ".join(current or [])
            except Exception:
                current = ""

        emoji, label = NOTE_FIELDS[field]
        await query.message.edit_text(
            f"{emoji} <b>Edit {label}</b>\n\n"
            f"Current: <code>{h(str(current or 'N/A'))}</code>\n\n"
            f"Send the new value:\n"
            f"<i>/cancel to go back</i>",
            parse_mode=HTML
        )
        return EN_VALUE

    return EN_MENU


async def editnote_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    uid   = context.user_data.get("edit_note_uid")
    value = update.message.text.strip()

    if field == "tags":
        import json
        parsed = [t.lower() for t in value.split() if t]
        value  = json.dumps(parsed)
    elif field == "course_code" and value != "-":
        value = value.upper()

    await update_note_field(uid, field, value if value != "-" else None)
    note = await get_note(uid)
    context.user_data["edit_note"] = note

    emoji, label = NOTE_FIELDS[field]
    await update.message.reply_text(
        f"✅ <b>{label}</b> updated!\n\n"
        f"{_note_summary(note)}\n\n"
        f"Edit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EN_MENU


async def editnote_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    uid   = context.user_data.get("edit_note_uid")
    field = context.user_data.get("edit_field")

    # Cover update
    if field == "cover":
        if msg.text and msg.text.strip() == "-":
            await update_note_cover(uid, None, None)
            label = "Cover removed."
        elif msg.photo or (msg.document and msg.document.mime_type and
                           msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("⏳ Uploading cover image...")
            cover_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            await update_note_cover(uid, fid, cover_url or None)
            label = "Cover updated!" if cover_url else "Cover saved (imgBB failed)"
        else:
            await msg.reply_text(
                "❌ Send a cover image (JPG/PNG) or <code>-</code> to remove.",
                parse_mode=HTML
            )
            return EN_FILE

        note = await get_note(uid)
        context.user_data["edit_note"] = note
        await msg.reply_text(
            f"✅ <b>{label}</b>\n\n{_note_summary(note)}\n\nEdit another field?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EN_MENU

    # File update
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("❌ Please send a PDF, DOCX, PPTX, or image file.")
        return EN_FILE

    await update_note_file(uid, file_id, file_type)
    note = await get_note(uid)
    context.user_data["edit_note"] = note

    await msg.reply_text(
        f"✅ <b>File replaced!</b> ({file_type.upper()})\n\n"
        f"{_note_summary(note)}\n\nEdit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EN_MENU


async def editnote_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Edit cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def editnote_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editnote", editnote_start)],
        states={
            EN_MENU: [
                CallbackQueryHandler(editnote_menu_callback, pattern="^en_")
            ],
            EN_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editnote_get_value),
                CallbackQueryHandler(editnote_menu_callback, pattern="^en_")
            ],
            EN_FILE: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    editnote_get_file
                ),
                CallbackQueryHandler(editnote_menu_callback, pattern="^en_")
            ],
        },
        fallbacks=[CommandHandler("cancel", editnote_cancel)],
        conversation_timeout=300,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletenote <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletenote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/deletenote &lt;uid&gt;</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid  = args[0].strip().lower()
    note = await get_note(uid)

    if not note:
        await update.message.reply_text(
            f"❌ No note found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["delete_note_uid"] = uid
    context.user_data["delete_note"]     = note

    await update.message.reply_text(
        f"⚠️ <b>Delete Note</b>\n\n"
        f"{_note_summary(note)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm deletion:</b>\n"
        f"<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DN_CONFIRM


async def deletenote_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data.get("delete_note_uid")
    note  = context.user_data.get("delete_note")

    if typed != uid:
        await update.message.reply_text(
            f"❌ UID doesn't match. Type exactly <code>{h(uid)}</code>:",
            parse_mode=HTML
        )
        return DN_CONFIRM

    await delete_note(uid)
    await update.message.reply_text(
        f"🗑 <b>Note deleted!</b>\n\n"
        f"📝 <b>{h(note['title'])}</b>\n"
        f"🆔 <code>{h(uid)}</code>",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


async def deletenote_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Deletion cancelled. Note is safe. 🦅")
    context.user_data.clear()
    return ConversationHandler.END


def deletenote_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletenote", deletenote_start)],
        states={
            DN_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletenote_confirm)]
        },
        fallbacks=[CommandHandler("cancel", deletenote_cancel)],
        conversation_timeout=120,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listnotes — paginated
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listnotes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_notes_page(update, context, page=0, edit=False)


async def _show_notes_page(update, context, page: int, edit: bool):
    total = await get_notes_count()
    if total == 0:
        text = "📝 <b>Notes</b>\n\n<i>No notes uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    notes       = await get_notes_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    import json
    lines = [f"📝 <b>Notes</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for n in notes:
        tags_raw = n.get("tags", [])
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
        tag_str = " ".join([f"#{t}" for t in tags[:3]]) if tags else ""
        course  = n.get("course_code") or "—"
        lines.append(
            f"🆔 <code>{h(n['uid'])}</code> [{n['file_type'].upper()}]\n"
            f"   📝 {h(n['title'])}\n"
            f"   📗 {h(course)}  {h(tag_str)}\n"
        )

    text = "\n".join(lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"ln_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ln_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"ln_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None

    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )


async def listnotes_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "ln_noop":
        await query.answer()
        return
    page = int(query.data.replace("ln_page_", ""))
    await query.answer()
    await _show_notes_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# NOTE DELIVERY — called from search handler
# ═══════════════════════════════════════════════════════════════════════════════

async def deliver_note(chat_id: int, note_uid: str, bot):
    """Send note file to user DM."""
    note = await get_note(note_uid)
    if not note:
        await bot.send_message(chat_id, "❌ Note not found.")
        return

    await increment_note_access(note_uid)
    await award_download(chat_id, "note", note.get("uploaded_by"), note_uid)

    import json
    tags_raw = note.get("tags", [])
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    tag_str = " ".join([f"#{t}" for t in tags]) if tags else ""

    title = note.get("title") or ""
    caption = h(title) if title else None

    file_type = note["file_type"]
    try:
        if file_type == "image":
            await bot.send_photo(chat_id, note["file_id"], caption=caption, parse_mode="HTML")
        else:
            await bot.send_document(chat_id, note["file_id"], caption=caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to deliver note {note_uid}: {e}")
        await bot.send_message(chat_id, "Failed to send file. Please try again.")


def addnote_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addnote", addnote_start),
            CallbackQueryHandler(addnote_start, pattern="^adm_add_note$"),
        ],
        states={
            AN_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, addnote_file)
            ],
            AN_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_info)
            ],
            AN_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addnote_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_cover),
            ],
            AN_TAGS: [
                CommandHandler("done", addnote_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )