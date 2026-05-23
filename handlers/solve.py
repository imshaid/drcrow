"""
Solve & Correction CRUD handler.
Commands:
  /addsolve <uid>
  /editsolve <uid>
  /deletesolve <uid>
  /listsolves
  /addcorrect <solve_uid> <correct_uid>
  /editcorrect <correct_uid>
  /deletecorrect <correct_uid>
Admin only.
"""

import logging
import re as _re
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
from telegram.error import TelegramError
from config.settings import settings
from database.solve_queries import (
    solve_uid_exists, insert_solve, get_solve,
    update_solve_field, update_solve_file, update_solve_cover,
    delete_solve, get_solves_paginated, get_solves_count,
    increment_solve_access, record_solve_delivery,
    get_solve_recipients,
    correct_uid_exists, insert_correction, get_correction,
    get_corrections, update_correction_title, update_correction_file,
    delete_correction
)
from utils.imgbb import upload_to_imgbb

logger   = logging.getLogger(__name__)
HTML     = ParseMode.HTML
PAGE_SIZE = 5

# ── File type detection ─────────────────────────────────────────────────────────
def _detect_file_type(msg) -> tuple:
    if msg.document:
        mime = msg.document.mime_type or ""
        if "pdf" in mime:            return msg.document.file_id, "pdf"
        if "presentation" in mime:   return msg.document.file_id, "pptx"
        if "word" in mime:           return msg.document.file_id, "docx"
        if mime.startswith("image"): return msg.document.file_id, "image"
        return msg.document.file_id, "document"
    if msg.photo:
        return msg.photo[-1].file_id, "image"
    return None, None

# ── States: /addsolve ────────────────────────────────────────────────────────────
AS_FILE, AS_INFO, AS_COVER, AS_TAGS = range(4)

# ── States: /editsolve ───────────────────────────────────────────────────────────
ESV_MENU, ESV_VALUE, ESV_FILE = range(3)

# ── States: /deletesolve ─────────────────────────────────────────────────────────
DSV_CONFIRM = 0

# ── States: /addcorrect ──────────────────────────────────────────────────────────
AC_FILE = 0  # kept for ConversationHandler
# addcorrect uses user_data state machine: _correct_step: "search" | "file"

# ── States: /editcorrect ─────────────────────────────────────────────────────────
EC_MENU, EC_VALUE, EC_FILE = range(3)

# ── States: /deletecorrect ───────────────────────────────────────────────────────
DC_CONFIRM = 0

SOLVE_FIELDS = {
    "title":       ("📌", "Title"),
    "subject":     ("📂", "Subject"),
    "course_code": ("📗", "Course Code"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "File"),
    "cover":       ("🖼", "Cover Image"),
}

CORRECT_FIELDS = {
    "title": ("📌", "Title"),
    "file":  ("📄", "File"),
}


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not settings.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _tag_str(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _solve_summary(s: dict) -> str:
    return (
        f"✅ <b>{h(s['title'])}</b>\n"
        f"📗 {h(s.get('course_code') or 'N/A')}\n"
        f"📂 {h(s.get('subject') or 'N/A')}\n"
        f"🏷 {h(_tag_str(s.get('tags', [])))}\n"
        f"🆔 <code>{h(s['uid'])}</code>\n"
        f"🖼 Cover: {'✅' if s.get('cover_url') else '—'}"
    )


def _correct_summary(c: dict) -> str:
    title = c.get("title") or "—"
    return (
        f"📝 <b>{h(title)}</b>\n"
        f"🆔 <code>{h(c['uid'])}</code>\n"
        f"📄 {c.get('file_type', '').upper()}"
    )


def _solve_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in SOLVE_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"esv_field_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="esv_cancel")])
    return InlineKeyboardMarkup(buttons)


def _correct_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key, (emoji, label) in CORRECT_FIELDS.items():
        buttons.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"ec_field_{key}")])
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="ec_cancel")])
    return InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# /addsolve <uid>
# ═══════════════════════════════════════════════════════════════════════════════

STOP_WORDS = {
    "a","an","the","of","and","or","for","in","to","with","on","at","by","from",
    "as","is","it","its","be","are","was","were","been","has","have","had",
    "not","no","nor","but","so","yet","all","any","some","such","than","too",
}

SOLVE_TYPES = {"mid", "final", "quiz", "assignment", "lab", "ct"}


async def _solve_get_course_info(course_code: str) -> dict:
    if not course_code:
        return {"code": "", "name": "", "abbr": ""}
    code = course_code.strip().upper()
    from database.queries import get_current_semester
    sem = await get_current_semester()
    if not sem:
        return {"code": code, "name": "", "abbr": ""}
    import json as _j
    courses = _j.loads(sem["courses"]) if isinstance(sem["courses"], str) else (sem["courses"] or [])
    for course in courses:
        if course.get("code", "").upper() == code:
            return {"code": code, "name": course.get("name", ""), "abbr": course.get("abbr", "")}
    return {"code": code, "name": "", "abbr": ""}


async def _solve_generate_uid(abbr: str) -> str:
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT uid FROM solves WHERE uid LIKE $1", f"{abbr}%s"
        )
    max_serial = 0
    pattern = _re.compile(rf"^{_re.escape(abbr)}(\d+)s$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"{abbr}{max_serial + 1:02d}s"


async def _correct_generate_uid(solve_uid: str) -> str:
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT uid FROM corrections WHERE solve_uid = $1", solve_uid
        )
    max_serial = 0
    pattern = _re.compile(rf"^{_re.escape(solve_uid)}(\d+)c$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"{solve_uid}{max_serial + 1:02d}c"


def _solve_parse_info(text: str) -> dict:
    """Parse 3-line format:
    Line 1: title (optional)
    Line 2: type — mid/final/quiz/assignment/lab/ct (mandatory)
    Line 3: course code (mandatory)
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(lines) == 2:
        # title omitted
        return {"title": None, "type": lines[0].lower(), "course_code": lines[1].upper()}
    elif len(lines) >= 3:
        return {"title": lines[0], "type": lines[1].lower(), "course_code": lines[2].upper()}
    elif len(lines) == 1:
        # only type — no course
        return {"title": None, "type": lines[0].lower(), "course_code": None}
    return {"title": None, "type": "", "course_code": None}


def _solve_auto_tags(title: str | None, solve_type: str, course_info: dict) -> list[str]:
    tags = {"solve", "solution"}
    if solve_type: tags.add(solve_type)
    if course_info.get("code"):  tags.add(course_info["code"].lower())
    if course_info.get("abbr"):  tags.add(course_info["abbr"].lower())
    if course_info.get("name"):
        for w in course_info["name"].lower().split():
            if w not in STOP_WORDS and len(w) > 1:
                tags.add(w)
    if title:
        for w in _re.sub(r"[^a-z0-9\s]", " ", title.lower()).split():
            if w not in STOP_WORDS and len(w) > 1:
                tags.add(w)
    return sorted(tags)


async def addsolve_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Solve</b>\n\n"
        "<i>Step 1 of 4</i>\n\n"
        "Send the solve file.\n"
        "PDF, DOCX, image — all accepted.",
        parse_mode=HTML
    )
    return AS_FILE


async def addsolve_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("Please send a PDF, DOCX, or image file.")
        return AS_FILE

    context.user_data["file_id"]   = file_id
    context.user_data["file_type"] = file_type

    await msg.reply_text(
        "<i>Step 2 of 4</i>\n\n"
        "Send solve info — each on a new line:\n\n"
        "<code>Title (optional)\n"
        "mid / final / quiz / assignment / lab / ct\n"
        "CSE315</code>\n\n"
        "Example (with title):\n"
        "<code>My Custom Title\n"
        "mid\n"
        "CSE315</code>\n\n"
        "Example (without title):\n"
        "<code>final\n"
        "CSE321</code>",
        parse_mode=HTML
    )
    return AS_INFO


async def addsolve_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    info = _solve_parse_info(text)

    solve_type  = info["type"]
    course_code = info["course_code"]

    if not solve_type or solve_type not in SOLVE_TYPES:
        await update.message.reply_text(
            f"Type must be one of: <code>{', '.join(sorted(SOLVE_TYPES))}</code>\n\nTry again.",
            parse_mode=HTML
        )
        return AS_INFO

    if not course_code:
        await update.message.reply_text("Course code is required. Try again.")
        return AS_INFO

    course_info = await _solve_get_course_info(course_code)
    abbr        = course_info["abbr"].lower() if course_info["abbr"] else "gen"
    uid         = await _solve_generate_uid(abbr)

    # Build title
    type_label = solve_type.capitalize()
    title = info["title"] or (
        f"{course_info['name']} {type_label} Solve"
        if course_info["name"] else f"{course_code} {type_label} Solve"
    )

    auto_tags = _solve_auto_tags(info["title"], solve_type, course_info)

    context.user_data.update({
        "uid":         uid,
        "title":       title,
        "solve_type":  solve_type,
        "course_code": course_code,
        "course_info": course_info,
        "auto_tags":   auto_tags,
    })

    course_line = (
        f"{course_info['code']} — {course_info['name']} ({course_info['abbr']})"
        if course_info.get("name") else course_code
    )

    await update.message.reply_text(
        f"<i>Step 3 of 4</i>\n\n"
        f"<b>Preview:</b>\n"
        f"UID   : <code>{uid}</code>\n"
        f"Title : {h(title)}\n"
        f"Type  : {h(type_label)}\n"
        f"Course: {h(course_line)}\n\n"
        f"Send cover image (JPG/PNG), or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AS_COVER


async def addsolve_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud  = context.user_data

    if msg.text and msg.text.strip() == "-":
        ud["cover_file_id"] = None
        ud["cover_url"]     = None
    elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
        fid       = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        cover_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        ud["cover_file_id"] = fid
        ud["cover_url"]     = cover_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code> to skip.", parse_mode=HTML)
        return AS_COVER

    auto_tags = ud.get("auto_tags", [])
    await msg.reply_text(
        f"<i>Step 4 of 4</i>\n\n"
        f"<b>Auto-generated tags:</b>\n"
        f"<code>{h(' '.join(auto_tags))}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return AS_TAGS


async def addsolve_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return AS_TAGS


async def addsolve_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called on /done — finalize and save."""
    ud   = context.user_data
    tags = ud.get("custom_tags") or ud.get("auto_tags", [])

    uid         = ud["uid"]
    title       = ud["title"]
    file_id     = ud["file_id"]
    file_type   = ud["file_type"]
    course_code = ud.get("course_code")
    course_info = ud.get("course_info", {})
    cover_fid   = ud.get("cover_file_id")
    cover_url   = ud.get("cover_url")
    user_id     = ud.get("_uploader_id", 0)
    subject     = course_info.get("name") or course_code or ""

    from database.queries import get_current_semester
    sem    = await get_current_semester()
    sem_id = sem["id"] if sem else None

    await insert_solve(
        uid, title, subject, course_code,
        file_id, file_type, tags, user_id,
        semester_id=sem_id,
        cover_file_id=cover_fid, cover_url=cover_url
    )

    from utils.notify import notify_resource
    await notify_resource(
        bot=update.get_bot(), category="solve",
        course_code=course_code, title=title, uid=uid
    )

    sem_name      = h(sem["name"]) if sem else "—"
    tag_str       = " ".join([f"#{t}" for t in tags]) or "none"
    course_display = (
        f"{course_info['code']} ({course_info['abbr']})"
        if course_info.get("abbr") else (course_code or "—")
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Edit",            callback_data=f"solve_edit_{uid}"),
        InlineKeyboardButton("Delete",          callback_data=f"solve_delete_{uid}"),
        InlineKeyboardButton("Add Correction",  callback_data=f"adm_add_correct_{uid}"),
    ]])

    await update.message.reply_text(
        f"<b>Solve added.</b>\n\n"
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



def addsolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addsolve", addsolve_start),
            CallbackQueryHandler(addsolve_start, pattern="^adm_add_solve$"),
        ],
        states={
            AS_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, addsolve_file)
            ],
            AS_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsolve_info)
            ],
            AS_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addsolve_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsolve_cover),
            ],
            AS_TAGS: [
                CommandHandler("done", addsolve_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsolve_tags_input),
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
# /editsolve <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editsolve_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/editsolve &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid   = args[0].strip().lower()
    solve = await get_solve(uid)
    if not solve:
        await update.message.reply_text(f"❌ No solve found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["edit_solve_uid"] = uid
    context.user_data["edit_solve"]     = solve
    await update.message.reply_text(
        f"✏️ <b>Edit Solve</b>\n\n{_solve_summary(solve)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_solve_edit_keyboard()
    )
    return ESV_MENU


async def editsolve_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data == "esv_cancel":
        await query.answer()
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "esv_back":
        await query.answer()
        solve = await get_solve(context.user_data["edit_solve_uid"])
        context.user_data["edit_solve"] = solve
        await query.message.edit_text(
            f"✏️ <b>Edit Solve</b>\n\n{_solve_summary(solve)}\n\nWhat to edit?",
            parse_mode=HTML, reply_markup=_solve_edit_keyboard()
        )
        return ESV_MENU

    if data.startswith("esv_field_"):
        field = data.replace("esv_field_", "")
        context.user_data["edit_field"] = field
        solve = context.user_data["edit_solve"]
        await query.answer()

        if field in ("file", "cover"):
            prompt = (
                "📄 <b>Replace File</b>\n\nSend new file (PDF, DOCX, image):\n<i>/cancel to go back</i>"
                if field == "file" else
                "🖼 <b>Update Cover</b>\n\nSend new cover image or <code>-</code> to remove:"
            )
            await query.message.edit_text(prompt, parse_mode=HTML)
            return ESV_FILE

        current = solve.get(field)
        if field == "tags":
            try:
                current = " ".join(json.loads(current)) if isinstance(current, str) else " ".join(current or [])
            except Exception:
                current = ""
        emoji, label = SOLVE_FIELDS[field]
        await query.message.edit_text(
            f"{emoji} <b>Edit {label}</b>\n\nCurrent: <code>{h(str(current or 'N/A'))}</code>\n\n"
            f"Send new value:\n<i>/cancel to go back</i>",
            parse_mode=HTML
        )
        return ESV_VALUE
    return ESV_MENU


async def editsolve_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    uid   = context.user_data["edit_solve_uid"]
    value = update.message.text.strip()

    if field == "tags":
        parsed = [t.lower() for t in value.split() if t]
        await update_solve_field(uid, "tags", json.dumps(parsed) if value != "-" else "[]")
    elif field == "course_code" and value != "-":
        await update_solve_field(uid, field, value.upper())
    else:
        await update_solve_field(uid, field, None if value == "-" else value)

    solve = await get_solve(uid)
    context.user_data["edit_solve"] = solve
    _, label = SOLVE_FIELDS[field]
    await update.message.reply_text(
        f"✅ <b>{label} updated!</b>\n\n{_solve_summary(solve)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_solve_edit_keyboard()
    )
    return ESV_MENU


async def editsolve_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    uid   = context.user_data["edit_solve_uid"]
    field = context.user_data["edit_field"]

    if field == "cover":
        if msg.text and msg.text.strip() == "-":
            await update_solve_cover(uid, None, None)
            label = "Cover removed."
        elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("⏳ Uploading...")
            cover_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            await update_solve_cover(uid, fid, cover_url or None)
            label = "Cover updated!" if cover_url else "Cover saved (imgBB failed)"
        else:
            await msg.reply_text("❌ Send image or <code>-</code>.", parse_mode=HTML)
            return ESV_FILE
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Please send a valid file.")
            return ESV_FILE
        await update_solve_file(uid, file_id, file_type)
        label = "File replaced!"

    solve = await get_solve(uid)
    context.user_data["edit_solve"] = solve
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_solve_summary(solve)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_solve_edit_keyboard()
    )
    return ESV_MENU


def editsolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editsolve", editsolve_start)],
        states={
            ESV_MENU: [CallbackQueryHandler(editsolve_menu_callback, pattern="^esv_")],
            ESV_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editsolve_get_value),
                CallbackQueryHandler(editsolve_menu_callback, pattern="^esv_")
            ],
            ESV_FILE: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    editsolve_get_file
                ),
                CallbackQueryHandler(editsolve_menu_callback, pattern="^esv_")
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletesolve <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletesolve_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/deletesolve &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid   = args[0].strip().lower()
    solve = await get_solve(uid)
    if not solve:
        await update.message.reply_text(f"❌ No solve found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    corrections = await get_corrections(uid)
    context.user_data["delete_solve_uid"] = uid
    context.user_data["delete_solve"]     = solve

    await update.message.reply_text(
        f"⚠️ <b>Delete Solve</b>\n\n{_solve_summary(solve)}\n\n"
        f"{'⚠️ This will also delete <b>' + str(len(corrections)) + ' correction(s)</b>.' if corrections else '📭 No corrections attached.'}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm:</b>\n<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DSV_CONFIRM


async def deletesolve_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_solve_uid"]
    solve = context.user_data["delete_solve"]

    if typed != uid:
        await update.message.reply_text(f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML)
        return DSV_CONFIRM

    await delete_solve(uid)
    await update.message.reply_text(
        f"🗑 <b>Deleted!</b>\n\n✅ <b>{h(solve['title'])}</b>\n🆔 <code>{h(uid)}</code>",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def deletesolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletesolve", deletesolve_start)],
        states={DSV_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletesolve_confirm)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listsolves
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def listsolves_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_solves_page(update, context, page=0, edit=False)


async def _show_solves_page(update, context, page: int, edit: bool):
    total = await get_solves_count()
    if total == 0:
        text = "✅ <b>Solves</b>\n\n<i>No solves uploaded yet.</i>"
        target = update.callback_query if edit else update
        await (target.edit_message_text if edit else update.message.reply_text)(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    solves      = await get_solves_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"✅ <b>Solves</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for s in solves:
        corr  = s.get("correction_count", 0)
        course = s.get("course_code") or "—"
        lines.append(
            f"🆔 <code>{h(s['uid'])}</code>  {'⚠️ ' + str(corr) + 'C' if corr else ''}\n"
            f"   📌 {h(s['title'])}\n"
            f"   📗 {h(course)}\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lsv_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lsv_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lsv_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    text     = "\n".join(lines)

    if edit:
        await update.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


async def listsolves_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lsv_noop":
        await query.answer()
        return
    page = int(query.data.replace("lsv_page_", ""))
    await query.answer()
    await _show_solves_page(update, context, page=page, edit=True)


# ═══════════════════════════════════════════════════════════════════════════════
# /addcorrect <solve_uid> <correct_uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def addcorrect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry — clears correction state and shows solve search button."""
    msg  = update.effective_message
    user = update.effective_user

    for k in ["_correct_step", "_correct_solve_uid", "_correct_solve", "_correct_uid"]:
        context.user_data.pop(k, None)
    context.user_data["_correct_step"]    = "search"
    context.user_data["_in_conversation"] = True

    if not hasattr(context.bot, "_correct_pending"):
        context.bot._correct_pending = set()
    context.bot._correct_pending.add(user.id)

    # Extract solve_uid from callback if triggered via Add Correction button
    if update.callback_query:
        data = update.callback_query.data
        if data.startswith("adm_add_correct_"):
            solve_uid = data[len("adm_add_correct_"):]
            await _addcorrect_with_solve(msg, context, user, solve_uid)
            return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Search Solve", switch_inline_query_current_chat="solve ")
    ]])
    await msg.reply_text(
        "<b>Add Correction</b>\n\n"
        "Search for the solve to attach the correction to.",
        parse_mode=HTML,
        reply_markup=keyboard
    )


async def _addcorrect_with_solve(msg, context, user, solve_uid: str):
    """Called when solve is already known — show prompt for file."""
    solve = await get_solve(solve_uid)
    if not solve:
        await msg.reply_text(f"Solve <code>{h(solve_uid)}</code> not found.", parse_mode=HTML)
        return

    correct_uid = await _correct_generate_uid(solve_uid)
    context.user_data.update({
        "_correct_step":     "file",
        "_correct_solve_uid": solve_uid,
        "_correct_solve":     solve,
        "_correct_uid":       correct_uid,
    })

    if hasattr(context.bot, "_correct_pending"):
        context.bot._correct_pending.discard(user.id)

    ed = f" ({h(solve['title'])})" if solve.get("title") else ""
    await msg.reply_text(
        f"<b>Solve selected:</b>\n"
        f"{h(solve['title'])}\n\n"
        f"<code>Correction UID : {h(correct_uid)}</code>\n\n"
        f"Now send the correction file (PDF, image, DOCX).",
        parse_mode=HTML
    )


async def addcorrect_handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called from _correct_file_handler in main.py when _correct_step == file.
    Supports multiple files — each saved with its own UID. /done to finish.
    """
    if context.user_data.get("_correct_step") != "file":
        return False

    msg = update.message
    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("Please send a PDF, image, or DOCX file, or /done to finish.")
        return True

    ud        = context.user_data
    solve_uid = ud["_correct_solve_uid"]
    solve     = ud["_correct_solve"]

    # Generate a fresh UID for each file
    correct_uid = await _correct_generate_uid(solve_uid)
    await insert_correction(correct_uid, solve_uid, file_id, file_type, title=None)

    # Track count
    ud["_correct_count"] = ud.get("_correct_count", 0) + 1
    count = ud["_correct_count"]

    await msg.reply_text(
        f"File {count} saved. Send more or /done to finish.",
    )
    return True


async def addcorrect_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called on /done — finalize correction upload and push to recipients."""
    if context.user_data.get("_correct_step") != "file":
        return False

    ud        = context.user_data
    solve_uid = ud["_correct_solve_uid"]
    solve     = ud["_correct_solve"]
    count     = ud.get("_correct_count", 0)

    if count == 0:
        await update.message.reply_text("No files uploaded. Send at least one correction file.")
        return True

    corrections = await get_corrections(solve_uid)
    recipients  = await get_solve_recipients(solve_uid)

    if recipients:
        try:
            await _notify_correction(context.bot, recipients, solve, "", title=None)
        except Exception as _e:
            logger.error(f"_notify_correction failed: {_e}", exc_info=True)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Add More Corrections", callback_data=f"adm_add_correct_{solve_uid}"),
    ]])

    await update.message.reply_text(
        f"<b>Correction(s) added.</b>\n\n"
        f"<b>{h(solve['title'])}</b>\n\n"
        f"<code>Files added       : {count}\n"
        f"Total corrections : {len(corrections)}</code>\n\n"
        f"{'Notifying ' + str(len(recipients)) + ' member(s)...' if recipients else 'No previous recipients to notify.'}",
        parse_mode=HTML,
        reply_markup=keyboard
    )

    for k in ["_correct_step", "_in_conversation", "_correct_solve_uid", "_correct_solve", "_correct_uid", "_correct_count"]:
        context.user_data.pop(k, None)
    return True


async def _notify_correction(bot, recipients: list, solve: dict, correct_uid: str, title):
    """Push solve + all corrections to previous recipients.
    Sends each file individually to avoid media group type mixing issues.
    """
    corrections = await get_corrections(solve["uid"])
    if not corrections:
        return

    solve_title = h(solve.get("title") or "")

    for user_id in recipients:
        try:
            # Intro message
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>Correction Update</b>\n\n"
                    f"A correction has been added to a solve you previously downloaded:\n"
                    f"<b>{solve_title}</b>\n\n"
                    f"The updated solve and all corrections are attached below."
                ),
                parse_mode="HTML"
            )
            await asyncio.sleep(0.2)

            # Send solve file first
            solve_caption = f"<b>{solve_title}</b>"
            if solve.get("file_type") == "image":
                await bot.send_photo(
                    chat_id=user_id,
                    photo=solve["file_id"],
                    caption=solve_caption,
                    parse_mode="HTML"
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=solve["file_id"],
                    caption=solve_caption,
                    parse_mode="HTML"
                )

            # Send each correction individually
            for i, corr in enumerate(corrections):
                corr_cap = h(corr.get("title") or f"Correction {i + 1}")
                await asyncio.sleep(0.3)
                if corr.get("file_type") == "image":
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=corr["file_id"],
                        caption=corr_cap
                    )
                else:
                    await bot.send_document(
                        chat_id=user_id,
                        document=corr["file_id"],
                        caption=corr_cap
                    )

            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Failed to push correction to {user_id}: {e}", exc_info=True)


# addcorrect uses user_data state machine — no ConversationHandler needed.
# Entry via: /addcorrect command, adm_add_correct_<uid> callback, or inline solve search.
# File handled by _correct_file_handler in main.py (group=2).


# ═══════════════════════════════════════════════════════════════════════════════
# /editcorrect <correct_uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editcorrect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/editcorrect &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid  = args[0].strip().lower()
    corr = await get_correction(uid)
    if not corr:
        await update.message.reply_text(f"❌ No correction found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["edit_correct_uid"] = uid
    context.user_data["edit_correct"]     = corr

    await update.message.reply_text(
        f"✏️ <b>Edit Correction</b>\n\n{_correct_summary(corr)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_correct_edit_keyboard()
    )
    return EC_MENU


async def editcorrect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    await query.answer()

    if data == "ec_cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data.startswith("ec_field_"):
        field = data.replace("ec_field_", "")
        context.user_data["edit_field"] = field
        corr  = context.user_data["edit_correct"]

        if field == "title":
            current = corr.get("title") or "N/A"
            await query.message.edit_text(
                f"📌 <b>Edit Title</b>\n\nCurrent: <code>{h(current)}</code>\n\n"
                f"Send new title or <code>-</code> to remove:", parse_mode=HTML
            )
            return EC_VALUE

        if field == "file":
            await query.message.edit_text(
                "📄 <b>Replace File</b>\n\nSend new file (PDF, image, DOCX):\n<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EC_FILE

    return EC_MENU


async def editcorrect_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = context.user_data["edit_correct_uid"]
    value = update.message.text.strip()
    await update_correction_title(uid, None if value == "-" else value)

    corr = await get_correction(uid)
    context.user_data["edit_correct"] = corr
    await update.message.reply_text(
        f"✅ <b>Title updated!</b>\n\n{_correct_summary(corr)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_correct_edit_keyboard()
    )
    return EC_MENU


async def editcorrect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg     = update.message
    uid     = context.user_data["edit_correct_uid"]
    file_id, file_type = _detect_file_type(msg)

    if not file_id:
        await msg.reply_text("❌ Please send a valid file.")
        return EC_FILE

    await update_correction_file(uid, file_id, file_type)
    corr = await get_correction(uid)
    context.user_data["edit_correct"] = corr
    await msg.reply_text(
        f"✅ <b>File replaced!</b>\n\n{_correct_summary(corr)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_correct_edit_keyboard()
    )
    return EC_MENU


def editcorrect_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editcorrect", editcorrect_start)],
        states={
            EC_MENU:  [CallbackQueryHandler(editcorrect_callback, pattern="^ec_")],
            EC_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editcorrect_value),
                CallbackQueryHandler(editcorrect_callback, pattern="^ec_")
            ],
            EC_FILE:  [
                MessageHandler(filters.Document.ALL | filters.PHOTO, editcorrect_file),
                CallbackQueryHandler(editcorrect_callback, pattern="^ec_")
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletecorrect <correct_uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletecorrect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/deletecorrect &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid  = args[0].strip().lower()
    corr = await get_correction(uid)
    if not corr:
        await update.message.reply_text(f"❌ No correction found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["delete_correct_uid"] = uid
    context.user_data["delete_correct"]     = corr

    await update.message.reply_text(
        f"⚠️ <b>Delete Correction</b>\n\n{_correct_summary(corr)}\n\n"
        f"<b>Type <code>{h(uid)}</code> to confirm:</b>\n<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DC_CONFIRM


async def deletecorrect_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_correct_uid"]
    corr  = context.user_data["delete_correct"]

    if typed != uid:
        await update.message.reply_text(f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML)
        return DC_CONFIRM

    await delete_correction(uid)
    await update.message.reply_text(
        f"🗑 <b>Correction deleted!</b>\n\n🆔 <code>{h(uid)}</code>",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def deletecorrect_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletecorrect", deletecorrect_start)],
        states={DC_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletecorrect_confirm)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DELIVERY — called from search handler
# ═══════════════════════════════════════════════════════════════════════════════


async def _send_corrections(bot, chat_id: int, corrections: list):
    """Send all corrections to chat_id."""
    for i, corr in enumerate(corrections):
        title = corr.get("title") or f"Correction {i + 1}"
        cap   = f"📝 {title}"
        try:
            if corr.get("file_type") == "image":
                await bot.send_photo(chat_id, corr["file_id"], caption=cap)
            else:
                await bot.send_document(chat_id, corr["file_id"], caption=cap)
            await asyncio.sleep(0.3)
        except TelegramError as e:
            logger.error(f"Failed to send correction {corr['uid']}: {e}")


async def _send_solve_file(bot, chat_id: int, solve: dict):
    """Send main solve file."""
    try:
        tags = json.loads(solve["tags"]) if isinstance(solve["tags"], str) else solve.get("tags", [])
    except Exception:
        tags = []
    tag_str = " ".join([f"#{t}" for t in tags])
    caption = h(solve.get('title') or '')

    try:
        if solve.get("file_type") == "image":
            await bot.send_photo(chat_id, solve["file_id"], caption=caption)
        else:
            await bot.send_document(chat_id, solve["file_id"], caption=caption)
    except TelegramError as e:
        logger.error(f"Failed to send solve {solve['uid']}: {e}")

async def deliver_solve(chat_id: int, solve_uid: str, bot):
    """
    Deliver solve + corrections to user DM.
    Tracks delivery for correction notifications.
    Paginates corrections if > 4.
    """
    solve       = await get_solve(solve_uid)
    corrections = await get_corrections(solve_uid)

    if not solve:
        await bot.send_message(chat_id, "❌ Solve not found.")
        return

    await increment_solve_access(solve_uid)
    await record_solve_delivery(chat_id, solve_uid)
    await award_download(chat_id, "solve", solve.get("uploaded_by"), solve_uid)

    # Build tags string
    try:
        tags = json.loads(solve["tags"]) if isinstance(solve["tags"], str) else solve.get("tags", [])
    except Exception:
        tags = []
    tag_str = " ".join([f"#{t}" for t in tags])

    caption = h(solve.get('title') or '')

    # Send main solve file
    try:
        file_type = solve.get("file_type", "document")
        if file_type == "image":
            await bot.send_photo(chat_id, solve["file_id"], caption=caption)
        else:
            await bot.send_document(chat_id, solve["file_id"], caption=caption)
    except TelegramError as e:
        logger.error(f"Failed to deliver solve {solve_uid}: {e}")
        await bot.send_message(chat_id, "⚠️ Failed to send solve file.")
        return

    if not corrections:
        return

    await bot.send_message(
        chat_id,
        f"⚠️ <b>{len(corrections)} Correction(s) for this solve:</b>",
        parse_mode=HTML
    )
    await _send_corrections(bot, chat_id, corrections)


async def handle_correction_callback(update, context):
    """Handle correction notification button presses."""
    query = update.callback_query
    data  = query.data
    user  = update.effective_user

    await query.answer("📥 Sending to your DM...")

    if data.startswith("correct_one_"):
        # correct_one_<solve_uid>_<correct_uid>
        parts     = data[len("correct_one_"):].split("_", 1)
        solve_uid = parts[0]
        corr_uid  = parts[1] if len(parts) > 1 else ""

        solve = await get_solve(solve_uid)
        corr  = await get_correction(corr_uid)

        if not solve or not corr:
            await context.bot.send_message(user.id, "❌ Content not found.")
            return

        await record_solve_delivery(user.id, solve_uid)
        await _send_solve_file(context.bot, user.id, solve)
        await _send_corrections(context.bot, user.id, [corr])

    elif data.startswith("correct_all_"):
        solve_uid   = data[len("correct_all_"):]
        solve       = await get_solve(solve_uid)
        corrections = await get_corrections(solve_uid)

        if not solve:
            await context.bot.send_message(user.id, "❌ Solve not found.")
            return

        await record_solve_delivery(user.id, solve_uid)
        await _send_solve_file(context.bot, user.id, solve)
        if corrections:
            await context.bot.send_message(
                user.id,
                f"⚠️ <b>{len(corrections)} Correction(s):</b>",
                parse_mode="HTML"
            )
            await _send_corrections(context.bot, user.id, corrections)