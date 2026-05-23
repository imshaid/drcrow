"""
resource_picker.py — Admin-only Edit/Delete flow via inline search.

Flow:
  Reply KB "Edit" / "Delete"
    → inline type-picker keyboard
    → admin taps type → switch_inline_query pre-filled ("edit:note ")
    → inline results shown → admin picks one
    → ChosenInlineResultHandler detects "edit:" / "delete:" prefix
    → bot sends field menu (edit) or confirm (delete)
    → fully button-driven from here, no commands needed

States (ConversationHandler):
  PE_FIELD   — field menu shown, waiting for field button
  PE_VALUE   — waiting for text input
  PE_FILE    — waiting for file/photo
  PE_MULTI   — waiting for multi-file sub-action (RegPay/Vidoc files)
  PE_SOL     — solution sub-menu (book context)
  PE_SOL_FILE — waiting for solution replacement file
  PE_CORR    — correction sub-menu (solve context)
  PE_CORR_FILE — waiting for correction replacement file

Entry point: callback_data "rpe_start_edit_{uid}_{rtype}"
             callback_data "rpe_start_delete_{uid}_{rtype}"
These are fired from chosen_inline_result_handler in search.py.
"""

import json
import logging
from html import escape as h
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CallbackQueryHandler, CommandHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

from config.settings import settings
from utils.imgbb import upload_to_imgbb

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

# ── States ────────────────────────────────────────────────────────────────────
PE_FIELD, PE_VALUE, PE_FILE, PE_MULTI, \
PE_SOL, PE_SOL_FILE, PE_CORR, PE_CORR_FILE = range(8)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _tags_display(raw) -> str:
    try:
        tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        tags = []
    return " ".join(f"#{t}" for t in tags) if tags else "—"


def _tags_parse(text: str) -> list:
    return [t.lower().strip("#") for t in text.split() if t.strip("#")]


async def _get_course_info(course_code: str) -> dict:
    """Fetch course name + abbr from current semester DB."""
    if not course_code:
        return {"code": "", "name": "", "abbr": ""}
    from database.queries import get_current_semester
    code = course_code.strip().upper()
    sem  = await get_current_semester()
    if not sem:
        return {"code": code, "name": "", "abbr": ""}
    courses = sem["courses"]
    if isinstance(courses, str):
        try:
            courses = json.loads(courses)
        except Exception:
            courses = []
    for c in (courses or []):
        if c.get("code", "").upper() == code:
            return {"code": code, "name": c.get("name", ""), "abbr": c.get("abbr", "")}
    return {"code": code, "name": "", "abbr": ""}


# ── Per-type field config ─────────────────────────────────────────────────────
#
# Each entry: field_key → (emoji, label, field_type)
# field_type:
#   "text"    — plain text input
#   "tags"    — space-separated tags
#   "course"  — text input, auto-updates subject from DB
#   "file"    — single document upload
#   "image"   — photo or image document → imgbb upload
#   "multi"   — multi-file sub-menu (RegPay / Vidoc)

FIELD_CFG = {
    "book": {
        "title":        ("📌", "Title",        "text"),
        "authors":      ("✍️", "Authors",      "text"),
        "edition":      ("📖", "Edition",      "text"),
        "course_codes": ("📗", "Course Codes", "course"),
        "tags":         ("🏷", "Tags",         "tags"),
        "file":         ("📄", "PDF File",     "file"),
        "cover":        ("🖼", "Cover",        "image"),
        # solutions injected dynamically in field menu
    },
    "note": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "solve": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
        # corrections injected dynamically
    },
    "psq": {
        "title": ("📌", "Title", "text"),
        "tags":  ("🏷", "Tags",  "tags"),
        "file":  ("📄", "File",  "file"),
        "cover": ("🖼", "Cover", "image"),
    },
    "vidoc": {
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "cover":       ("🖼", "Cover",       "image"),
        "messages":    ("💬", "Messages",    "multi"),
    },
    "syllabus": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "outline": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "routine": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "util": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "slide": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "waiver": {
        "semester_name": ("🗓", "Semester",     "text"),
        "tuition_fee":   ("💰", "Tuition Fee",  "text"),
        "semester_fee":  ("💰", "Semester Fee", "text"),
        "tags":          ("🏷", "Tags",         "tags"),
        "file":          ("📄", "File",         "file"),
        "cover":         ("🖼", "Cover",        "image"),
    },
    "regpay": {
        "semester": ("🗓", "Semester", "text"),
        "tags":     ("🏷", "Tags",    "tags"),
        "cover":    ("🖼", "Cover",   "image"),
        "files":    ("📄", "Files",   "multi"),
    },
    "cal": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
    "advisor": {
        "title":       ("📌", "Title",       "text"),
        "course_code": ("📗", "Course Code", "course"),
        "tags":        ("🏷", "Tags",        "tags"),
        "file":        ("📄", "File",        "file"),
        "cover":       ("🖼", "Cover",       "image"),
    },
}

# DB table for each type (utilities share one table)
_UTILITY_TYPES = {"syllabus", "outline", "routine", "util", "slide", "cal", "advisor"}
_UTILITY_CAT   = {
    "syllabus": "syllabus", "outline": "outline", "routine": "routine",
    "util": "util_misc", "slide": "slides", "cal": "cal", "advisor": "advisor",
}


# ── DB wrappers ───────────────────────────────────────────────────────────────

async def _db_get(rtype: str, uid: str) -> dict | None:
    if rtype == "book":
        from database.book_queries import get_book
        return await get_book(uid)
    if rtype == "note":
        from database.note_queries import get_note
        return await get_note(uid)
    if rtype == "solve":
        from database.solve_queries import get_solve
        return await get_solve(uid)
    if rtype == "psq":
        from database.psq_queries import get_psq
        return await get_psq(uid)
    if rtype == "vidoc":
        from database.vidoc_queries import get_vidoc
        return await get_vidoc(uid)
    if rtype in _UTILITY_TYPES:
        from database.utility_queries import get_utility
        return await get_utility(uid)
    if rtype == "waiver":
        from database.waiver_queries import get_waiver
        return await get_waiver(uid)
    if rtype == "regpay":
        from database.regpay_queries import get_regpay
        return await get_regpay(uid)
    return None


async def _db_delete(rtype: str, uid: str):
    if rtype == "book":
        from database.book_queries import delete_book
        await delete_book(uid)
    elif rtype == "note":
        from database.note_queries import delete_note
        await delete_note(uid)
    elif rtype == "solve":
        from database.solve_queries import delete_solve
        await delete_solve(uid)
    elif rtype == "psq":
        from database.psq_queries import delete_psq
        await delete_psq(uid)
    elif rtype == "vidoc":
        from database.vidoc_queries import delete_vidoc
        await delete_vidoc(uid)
    elif rtype in _UTILITY_TYPES:
        from database.utility_queries import delete_utility
        await delete_utility(uid)
    elif rtype == "waiver":
        from database.waiver_queries import delete_waiver
        await delete_waiver(uid)
    elif rtype == "regpay":
        from database.regpay_queries import delete_regpay
        await delete_regpay(uid)


async def _db_update_text(rtype: str, uid: str, field: str, value):
    """Update a simple text/number/tags field."""
    import json as _json
    if rtype == "book":
        from database.book_queries import update_book_field
        # tags stored as JSON string in book
        if field == "tags" and isinstance(value, list):
            value = _json.dumps(value)
        await update_book_field(uid, field, value)
    elif rtype == "note":
        from database.note_queries import update_note_field
        if field == "tags" and isinstance(value, list):
            value = _json.dumps(value)
        await update_note_field(uid, field, value)
    elif rtype == "solve":
        from database.solve_queries import update_solve_field
        if field == "tags" and isinstance(value, list):
            value = _json.dumps(value)
        await update_solve_field(uid, field, value)
    elif rtype == "psq":
        from database.psq_queries import update_psq_field, update_psq_tags
        if field == "tags":
            tags = value if isinstance(value, list) else []
            await update_psq_tags(uid, tags)
        else:
            await update_psq_field(uid, field, value)
    elif rtype == "vidoc":
        from database.vidoc_queries import (
            update_vidoc_tags, update_vidoc_metadata, get_vidoc
        )
        if field == "tags":
            tags = value if isinstance(value, list) else []
            await update_vidoc_tags(uid, tags)
        elif field in ("course_code", "subject"):
            rec = await get_vidoc(uid)
            subj = rec.get("subject") if rec else None
            code = rec.get("course_code") if rec else None
            if field == "subject":
                subj = value
            else:
                code = value
            await update_vidoc_metadata(uid, subj, code)
    elif rtype in _UTILITY_TYPES:
        from database.utility_queries import update_utility_metadata, update_utility_tags
        if field == "tags":
            tags = value if isinstance(value, list) else []
            await update_utility_tags(uid, tags)
        else:
            rec = await _db_get(rtype, uid)
            kw = {
                "title":       rec.get("title"),
                "subject":     rec.get("subject"),
                "course_code": rec.get("course_code"),
            }
            kw[field] = value
            await update_utility_metadata(uid, **kw)
    elif rtype == "waiver":
        from database.waiver_queries import update_waiver_field
        import json as _j2
        if field == "tags" and isinstance(value, list):
            value = _j2.dumps(value)
        await update_waiver_field(uid, field, value)
    elif rtype == "regpay":
        from database.regpay_queries import update_regpay_field
        import json as _j3
        if field == "tags" and isinstance(value, list):
            value = _j3.dumps(value)
        await update_regpay_field(uid, field, value)


async def _db_update_file(rtype: str, uid: str, file_id: str, file_type: str):
    if rtype == "book":
        from database.book_queries import update_book_file
        await update_book_file(uid, file_id)
    elif rtype == "note":
        from database.note_queries import update_note_file
        await update_note_file(uid, file_id, file_type)
    elif rtype == "solve":
        from database.solve_queries import update_solve_file
        await update_solve_file(uid, file_id, file_type)
    elif rtype == "psq":
        from database.psq_queries import update_psq_file
        await update_psq_file(uid, file_id)
    elif rtype == "vidoc":
        pass  # vidoc file handled via messages multi-flow
    elif rtype in _UTILITY_TYPES:
        from database.utility_queries import update_utility_file
        await update_utility_file(uid, file_id, file_type)
    elif rtype == "waiver":
        from database.waiver_queries import update_waiver_file
        await update_waiver_file(uid, file_id, file_type)


async def _db_update_cover(rtype: str, uid: str, cover_file_id, cover_url):
    if rtype == "book":
        from database.book_queries import update_cover_file
        await update_cover_file(uid, cover_file_id, cover_url=cover_url)
    elif rtype == "note":
        from database.note_queries import update_note_cover
        await update_note_cover(uid, cover_file_id, cover_url=cover_url)
    elif rtype == "solve":
        from database.solve_queries import update_solve_cover
        await update_solve_cover(uid, cover_file_id, cover_url=cover_url)
    elif rtype == "psq":
        from database.psq_queries import update_psq_cover
        await update_psq_cover(uid, cover_file_id, cover_url=cover_url)
    elif rtype == "vidoc":
        from database.vidoc_queries import update_vidoc_thumbnail
        await update_vidoc_thumbnail(uid, cover_url, cover_file_id=cover_file_id)
    elif rtype in _UTILITY_TYPES:
        from database.utility_queries import update_utility_thumbnail
        await update_utility_thumbnail(uid, cover_url, cover_file_id=cover_file_id)
    elif rtype == "waiver":
        from database.waiver_queries import update_waiver_cover
        await update_waiver_cover(uid, cover_file_id, cover_url=cover_url)
    elif rtype == "regpay":
        from database.regpay_queries import update_regpay_cover
        await update_regpay_cover(uid, cover_file_id, cover_url=cover_url)


# ── Summary builders ──────────────────────────────────────────────────────────

def _summary(rtype: str, rec: dict) -> str:
    """Clean key-value summary, no emojis, monospace values."""
    uid  = h(rec.get("uid", "?"))
    tgs  = h(_tags_display(rec.get("tags")))

    def kv(key: str, val) -> str:
        return f"<b>{key}:</b> {h(str(val or '—'))}"

    if rtype == "book":
        ed = f" ({h(rec['edition'])})" if rec.get("edition") else ""
        lines = [
            kv("Title",   f"{rec.get('title','')}{ed}"),
            kv("Authors", rec.get("authors", "")),
            kv("Course",  rec.get("course_codes") or "—"),
            kv("Tags",    _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "note":
        lines = [
            kv("Title",  rec.get("title", "")),
            kv("Course", rec.get("course_code") or "—"),
            kv("Tags",   _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "solve":
        lines = [
            kv("Title",  rec.get("title", "")),
            kv("Course", rec.get("course_code") or "—"),
            kv("Tags",   _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "psq":
        lines = [
            kv("Title", rec.get("title") or "—"),
            kv("Tags",  _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "vidoc":
        try:
            msgs = json.loads(rec["messages"]) if isinstance(rec.get("messages"), str) else (rec.get("messages") or [])
        except Exception:
            msgs = []
        lines = [
            kv("Course",   rec.get("course_code") or "—"),
            kv("Messages", len(msgs)),
            kv("Tags",     _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "waiver":
        lines = [
            kv("Semester",     rec.get("semester_name", "")),
            kv("Tuition fee",  rec.get("tuition_fee", "—")),
            kv("Semester fee", rec.get("semester_fee", "—")),
            kv("Tags",         _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    elif rtype == "regpay":
        try:
            fids = json.loads(rec["file_ids"]) if isinstance(rec.get("file_ids"), str) else (rec.get("file_ids") or [])
        except Exception:
            fids = []
        lines = [
            kv("Semester", rec.get("semester", "")),
            kv("Files",    len(fids)),
            kv("Tags",     _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]
    else:
        # utilities: syllabus, outline, routine, util, cal, advisor
        lines = [
            kv("Title",  rec.get("title") or "—"),
            kv("Course", rec.get("course_code") or "—"),
            kv("Tags",   _tags_display(rec.get("tags"))),
            f"<b>UID:</b> <code>{uid}</code>",
        ]

    return "\n".join(lines)


# ── Field menu keyboard ───────────────────────────────────────────────────────

def _field_keyboard(rtype: str, uid: str,
                    solutions: list = None,
                    corrections: list = None) -> InlineKeyboardMarkup:
    fields = FIELD_CFG.get(rtype, {})
    rows   = []
    row    = []
    for key, (emoji, label, _) in fields.items():
        row.append(InlineKeyboardButton(
            f"{emoji} {label}", callback_data=f"rpe_field_{key}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Solution manuals section (book only)
    if rtype == "book" and solutions is not None:
        rows.append([InlineKeyboardButton("─── Solution Manuals ───", callback_data="rpe_noop")])
        sol_row = []
        for i, sol in enumerate(solutions, 1):
            sol_row.append(InlineKeyboardButton(
                f"📋 Sol #{i}", callback_data=f"rpe_sol_{sol['uid']}"
            ))
            if len(sol_row) == 3:
                rows.append(sol_row)
                sol_row = []
        if sol_row:
            rows.append(sol_row)
        rows.append([InlineKeyboardButton("➕ Add Solution", callback_data="rpe_sol_add")])

    # Corrections section (solve only)
    if rtype == "solve" and corrections is not None:
        rows.append([InlineKeyboardButton("─── Corrections ───", callback_data="rpe_noop")])
        corr_row = []
        for i, corr in enumerate(corrections, 1):
            corr_row.append(InlineKeyboardButton(
                f"📝 Corr #{i}", callback_data=f"rpe_corr_{corr['uid']}"
            ))
            if len(corr_row) == 3:
                rows.append(corr_row)
                corr_row = []
        if corr_row:
            rows.append(corr_row)
        rows.append([InlineKeyboardButton("➕ Add Correction", callback_data="rpe_corr_add")])

    rows.append([InlineKeyboardButton("✖ Done", callback_data="rpe_done")])
    return InlineKeyboardMarkup(rows)


def _delete_confirm_keyboard(rtype: str, uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"rpe_delconfirm"),
        InlineKeyboardButton("❌ Cancel",      callback_data="rpe_done"),
    ]])


def _sol_action_keyboard(sol_uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Replace file", callback_data=f"rpe_sol_replace_{sol_uid}"),
            InlineKeyboardButton("🗑 Delete",        callback_data=f"rpe_sol_delete_{sol_uid}"),
        ],
        [InlineKeyboardButton("← Back", callback_data="rpe_sol_back")],
    ])


def _corr_action_keyboard(corr_uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📌 Edit title", callback_data=f"rpe_corr_title_{corr_uid}"),
            InlineKeyboardButton("🔄 Replace file", callback_data=f"rpe_corr_replace_{corr_uid}"),
        ],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"rpe_corr_delete_{corr_uid}")],
        [InlineKeyboardButton("← Back",   callback_data="rpe_corr_back")],
    ])


def _multi_file_keyboard(rtype: str, uid: str, file_count: int) -> InlineKeyboardMarkup:
    is_vidoc   = rtype == "vidoc"
    add_label  = "➕ Add message" if is_vidoc else "➕ Add file"
    rm_label   = "🗑 Remove a message" if is_vidoc else "🗑 Remove a file"
    repl_label = "🔄 Replace all messages" if is_vidoc else "🔄 Replace all files"
    rows = [
        [InlineKeyboardButton(add_label, callback_data="rpe_multi_add")],
    ]
    if file_count > 0:
        rows.append([InlineKeyboardButton(rm_label,   callback_data="rpe_multi_remove")])
        rows.append([InlineKeyboardButton(repl_label, callback_data="rpe_multi_replace")])
    rows.append([InlineKeyboardButton("← Back", callback_data="rpe_multi_back")])
    return InlineKeyboardMarkup(rows)


# ── Entry: called from chosen_inline_result_handler in search.py ──────────────

async def pe_init_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point for the ConversationHandler.
    Fired when admin taps the Edit/Delete button on the resource summary message.
    Reads action, rtype, uid from callback_data, sets up user_data, shows flow.
    """
    query = update.callback_query
    await query.answer()
    data = query.data  # rpe_init_edit_{rtype}_{uid} or rpe_init_delete_{rtype}_{uid}

    user = update.effective_user
    if not (user and settings.is_admin(user.id)):
        await query.answer("Admin only.", show_alert=True)
        return ConversationHandler.END

    parts = data.split("_", 4)  # ["rpe", "init", "edit", rtype, uid]
    if len(parts) < 5:
        return ConversationHandler.END

    action = parts[2]   # "edit" or "delete"
    rtype  = parts[3]
    uid    = parts[4]

    rec = await _db_get(rtype, uid)
    if not rec:
        await query.message.edit_text(
            f"❌ Resource not found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data.update({
        "rpe_uid":          uid,
        "rpe_rtype":        rtype,
        "rpe_rec":          rec,
        "rpe_action":       action,
        "_in_conversation": True,
    })

    if action == "delete":
        await query.message.edit_text(
            f"⚠️ <b>Delete {rtype.title()}</b>\n\n{_summary(rtype, rec)}\n\n"
            f"<b>This cannot be undone.</b>",
            parse_mode=HTML,
            reply_markup=_delete_confirm_keyboard(rtype, uid)
        )
        return PE_FIELD

    # Edit: load solutions/corrections if needed
    solutions   = None
    corrections = None
    if rtype == "book":
        from database.book_queries import get_book_solutions
        solutions = await get_book_solutions(uid)
        context.user_data["rpe_solutions"] = solutions
    if rtype == "solve":
        from database.solve_queries import get_corrections
        corrections = await get_corrections(uid)
        context.user_data["rpe_corrections"] = corrections

    await query.message.edit_text(
        f"✏️ <b>Edit {rtype.title()}</b>\n\n{_summary(rtype, rec)}\n\nChoose a field:",
        parse_mode=HTML,
        reply_markup=_field_keyboard(rtype, uid, solutions, corrections)
    )
    return PE_FIELD


async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     uid: str, rtype: str) -> None:
    """
    Send field menu directly — no extra "Start Editing" tap needed.
    The first rpe_field_* or rpe_done button click fires pe_field_cb
    which is also registered as an entry_point.
    user_data is pre-populated here so pe_field_cb can read it immediately.
    """
    rec = await _db_get(rtype, uid)
    if not rec:
        await context.bot.send_message(
            update.effective_user.id,
            f"❌ Resource not found: <code>{h(uid)}</code>",
            parse_mode=HTML
        )
        return

    solutions   = None
    corrections = None
    if rtype == "book":
        from database.book_queries import get_book_solutions
        solutions = await get_book_solutions(uid)
    if rtype == "solve":
        from database.solve_queries import get_corrections
        corrections = await get_corrections(uid)

    # Pre-populate user_data so pe_field_cb works on first tap
    update.effective_user  # ensure user context is set
    context.user_data.update({
        "rpe_uid":          uid,
        "rpe_rtype":        rtype,
        "rpe_rec":          rec,
        "rpe_action":       "edit",
        "_in_conversation": True,
        "rpe_solutions":    solutions,
        "rpe_corrections":  corrections,
    })

    await context.bot.send_message(
        update.effective_user.id,
        f"✏️ <b>Edit {rtype.title()}</b>\n\n{_summary(rtype, rec)}\n\nChoose a field:",
        parse_mode=HTML,
        reply_markup=_field_keyboard(rtype, uid, solutions, corrections)
    )


async def start_delete(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       uid: str, rtype: str) -> None:
    """
    Send delete confirm directly — no extra tap needed.
    rpe_delconfirm / rpe_done buttons are entry_points.
    """
    rec = await _db_get(rtype, uid)
    if not rec:
        await context.bot.send_message(
            update.effective_user.id,
            f"❌ Resource not found: <code>{h(uid)}</code>",
            parse_mode=HTML
        )
        return

    context.user_data.update({
        "rpe_uid":          uid,
        "rpe_rtype":        rtype,
        "rpe_rec":          rec,
        "rpe_action":       "delete",
        "_in_conversation": True,
    })

    await context.bot.send_message(
        update.effective_user.id,
        f"🗑 <b>Delete {rtype.title()}</b>\n\n{_summary(rtype, rec)}\n\n"
        f"<b>This cannot be undone.</b>",
        parse_mode=HTML,
        reply_markup=_delete_confirm_keyboard(rtype, uid)
    )


# ── State: PE_FIELD — field buttons ──────────────────────────────────────────

async def pe_field_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rtype = ud["rpe_rtype"]
    rec   = ud["rpe_rec"]

    # ── no-op separator ──────────────────────────────────────────────────────
    if data == "rpe_noop":
        return PE_FIELD

    # ── done / cancel ────────────────────────────────────────────────────────
    if data == "rpe_done":
        await query.message.edit_text("✅ Done.", reply_markup=None)
        context.user_data.clear()
        return ConversationHandler.END

    # ── delete confirm ───────────────────────────────────────────────────────
    if data == "rpe_delconfirm":
        try:
            await _db_delete(rtype, uid)
            await query.message.edit_text(
                f"🗑 <b>Deleted!</b>\n<code>{h(uid)}</code> removed.",
                parse_mode=HTML, reply_markup=None
            )
        except Exception as e:
            await query.message.edit_text(f"❌ Delete failed: {h(str(e))}", parse_mode=HTML)
        context.user_data.clear()
        return ConversationHandler.END

    # ── solution sub-menu ────────────────────────────────────────────────────
    if data == "rpe_sol_back":
        solutions = ud.get("rpe_solutions", [])
        await query.message.edit_text(
            f"✏️ <b>Edit Book</b>\n\n{_summary(rtype, rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard(rtype, uid, solutions)
        )
        return PE_FIELD

    if data.startswith("rpe_sol_") and not data.startswith("rpe_sol_replace_") \
            and not data.startswith("rpe_sol_delete_") and data != "rpe_sol_add":
        sol_uid = data.replace("rpe_sol_", "")
        ud["rpe_sol_uid"] = sol_uid
        sols = ud.get("rpe_solutions", [])
        sol  = next((s for s in sols if s["uid"] == sol_uid), None)
        idx  = next((i+1 for i, s in enumerate(sols) if s["uid"] == sol_uid), "?")
        label = f"Sol #{idx} — <code>{h(sol_uid)}</code>"
        await query.message.edit_text(
            f"📋 <b>Solution Manual</b>\n{label}\n\nWhat to do?",
            parse_mode=HTML,
            reply_markup=_sol_action_keyboard(sol_uid)
        )
        return PE_SOL

    if data == "rpe_sol_add":
        # re-use existing addsolution flow via bot pending flag
        if not hasattr(context.bot, "_sol_pending"):
            context.bot._sol_pending = set()
        context.bot._sol_pending.add(update.effective_user.id)
        ud["rpe_sol_adding"] = True
        await query.message.edit_text(
            f"📋 <b>Add Solution Manual</b>\n\n"
            f"Search for this book via inline:\n",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔍 Search book",
                    switch_inline_query_current_chat=f"book {rec.get('title','')[:30]}"
                )
            ]])
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ── correction sub-menu ──────────────────────────────────────────────────
    if data == "rpe_corr_back":
        corrections = ud.get("rpe_corrections", [])
        await query.message.edit_text(
            f"✏️ <b>Edit Solve</b>\n\n{_summary(rtype, rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard(rtype, uid, corrections=corrections)
        )
        return PE_FIELD

    if data.startswith("rpe_corr_") and not data.startswith("rpe_corr_title_") \
            and not data.startswith("rpe_corr_replace_") \
            and not data.startswith("rpe_corr_delete_") and data != "rpe_corr_add":
        corr_uid = data.replace("rpe_corr_", "")
        ud["rpe_corr_uid"] = corr_uid
        corrs = ud.get("rpe_corrections", [])
        idx   = next((i+1 for i, c in enumerate(corrs) if c["uid"] == corr_uid), "?")
        await query.message.edit_text(
            f"📝 <b>Correction #{idx}</b>\n<code>{h(corr_uid)}</code>\n\nWhat to do?",
            parse_mode=HTML,
            reply_markup=_corr_action_keyboard(corr_uid)
        )
        return PE_CORR

    if data == "rpe_corr_add":
        if not hasattr(context.bot, "_correct_pending"):
            context.bot._correct_pending = set()
        context.bot._correct_pending.add(update.effective_user.id)
        context.user_data["_correct_step"] = "search"
        await query.message.edit_text(
            f"📝 <b>Add Correction</b>\n\nSearch the solve to attach to:",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔍 Search solve",
                    switch_inline_query_current_chat=f"solve {rec.get('title','')[:30]}"
                )
            ]])
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ── field selected ───────────────────────────────────────────────────────
    if data.startswith("rpe_field_"):
        field = data.replace("rpe_field_", "")
        fields = FIELD_CFG.get(rtype, {})
        if field not in fields:
            return PE_FIELD

        emoji, label, ftype = fields[field]
        ud["rpe_field"]  = field
        ud["rpe_ftype"]  = ftype
        ud["rpe_flabel"] = label

        # Current value display
        if field == "tags":
            cur = _tags_display(rec.get("tags"))
        else:
            cur = str(rec.get(field) or "—")

        if ftype in ("text", "tags", "course"):
            await query.message.edit_text(
                f"{emoji} <b>Edit {label}</b>\n\n"
                f"Current: <code>{h(cur)}</code>\n\n"
                f"Send new value:\n<i>Send - to clear</i>",
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖ Cancel", callback_data="rpe_done")
                ]])
            )
            return PE_VALUE

        elif ftype == "file":
            await query.message.edit_text(
                f"{emoji} <b>Replace {label}</b>\n\n"
                f"Send the new file:",
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖ Cancel", callback_data="rpe_done")
                ]])
            )
            return PE_FILE

        elif ftype == "image":
            await query.message.edit_text(
                f"{emoji} <b>Replace {label}</b>\n\n"
                f"Send new image, or - to remove:",
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖ Cancel", callback_data="rpe_done")
                ]])
            )
            return PE_FILE

        elif ftype == "multi":
            # RegPay files or Vidoc messages
            if rtype == "regpay":
                fids = rec.get("file_ids") or []
                if isinstance(fids, str):
                    try:
                        fids = json.loads(fids)
                    except Exception:
                        fids = []
                file_count = len(fids)
                ud["rpe_multi_files"] = list(fids)
            else:  # vidoc messages
                msgs = rec.get("messages") or []
                if isinstance(msgs, str):
                    try:
                        msgs = json.loads(msgs)
                    except Exception:
                        msgs = []
                file_count = len(msgs)
                ud["rpe_multi_msgs"] = list(msgs)

            await query.message.edit_text(
                f"📄 <b>Manage Files</b>\n\nCurrently {file_count} file(s).",
                parse_mode=HTML,
                reply_markup=_multi_file_keyboard(rtype, uid, file_count)
            )
            return PE_MULTI

    return PE_FIELD


# ── State: PE_VALUE — text input ──────────────────────────────────────────────

async def pe_value_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rtype = ud["rpe_rtype"]
    field = ud["rpe_field"]
    ftype = ud["rpe_ftype"]
    label = ud["rpe_flabel"]
    text  = update.message.text.strip()

    if text == "-":
        value = None
    elif ftype == "tags":
        value = _tags_parse(text)   # pass list; _db_update_text handles serialisation per type
    elif ftype == "course":
        # update course_code and auto-fill subject
        code = text.upper()
        info = await _get_course_info(code)
        await _db_update_text(rtype, uid, "course_code", code)
        if info["name"]:
            await _db_update_text(rtype, uid, "subject", info["name"])
        value = None  # already saved above
    else:
        value = text

    if value is not None:
        await _db_update_text(rtype, uid, field, value)

    # Refresh rec and re-show field menu
    rec = await _db_get(rtype, uid)
    ud["rpe_rec"] = rec
    solutions   = ud.get("rpe_solutions")
    corrections = ud.get("rpe_corrections")

    await update.message.reply_text(
        f"✅ <b>{label} updated!</b>\n\n"
        f"{_summary(rtype, rec)}\n\nChoose another field or tap Done:",
        parse_mode=HTML,
        reply_markup=_field_keyboard(rtype, uid, solutions, corrections)
    )
    return PE_FIELD


# ── State: PE_FILE — file / image input ──────────────────────────────────────

async def pe_file_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rtype = ud["rpe_rtype"]
    ftype = ud["rpe_ftype"]
    label = ud["rpe_flabel"]
    msg   = update.message

    if ftype == "image":
        # text "-" = remove cover
        if msg.text and msg.text.strip() == "-":
            await _db_update_cover(rtype, uid, None, None)
            label_done = "Cover removed."
        elif msg.photo or (msg.document and msg.document.mime_type
                           and msg.document.mime_type.startswith("image")):
            file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            wait = await msg.reply_text("⏳ Uploading...")
            cover_url = await upload_to_imgbb(context.bot, file_id)
            await wait.delete()
            await _db_update_cover(rtype, uid, file_id, cover_url or None)
            label_done = "Cover updated!" if cover_url else "Cover saved (imgBB failed)."
        else:
            await msg.reply_text("❌ Send an image or - to remove.")
            return PE_FILE
    else:
        # regular file
        if not msg.document:
            await msg.reply_text("❌ Send a file.")
            return PE_FILE
        ft = "pdf"
        if msg.document.mime_type:
            m = msg.document.mime_type
            if "presentation" in m:
                ft = "pptx"
            elif "word" in m:
                ft = "docx"
        await _db_update_file(rtype, uid, msg.document.file_id, ft)
        label_done = f"{label} replaced!"

    rec = await _db_get(rtype, uid)
    ud["rpe_rec"] = rec
    solutions   = ud.get("rpe_solutions")
    corrections = ud.get("rpe_corrections")

    await msg.reply_text(
        f"✅ <b>{label_done}</b>\n\n"
        f"{_summary(rtype, rec)}\n\nChoose another field or tap Done:",
        parse_mode=HTML,
        reply_markup=_field_keyboard(rtype, uid, solutions, corrections)
    )
    return PE_FIELD


# ── Cancel button from within PE_FILE / PE_VALUE ──────────────────────────────

async def pe_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "rpe_done":
        await query.message.edit_text("✅ Done.", reply_markup=None)
        context.user_data.clear()
        return ConversationHandler.END
    return PE_FIELD


# ── State: PE_SOL — solution sub-menu callbacks ───────────────────────────────

async def pe_sol_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rec   = ud["rpe_rec"]

    if data == "rpe_sol_back":
        solutions = ud.get("rpe_solutions", [])
        await query.message.edit_text(
            f"✏️ <b>Edit Book</b>\n\n{_summary('book', rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard("book", uid, solutions)
        )
        return PE_FIELD

    if data.startswith("rpe_sol_replace_"):
        sol_uid = data.replace("rpe_sol_replace_", "")
        ud["rpe_sol_uid"] = sol_uid
        ud["rpe_sol_action"] = "replace"
        await query.message.edit_text(
            f"🔄 <b>Replace Solution File</b>\n\n"
            f"<code>{h(sol_uid)}</code>\n\nSend the new PDF:",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖ Cancel", callback_data="rpe_sol_back")
            ]])
        )
        return PE_SOL_FILE

    if data.startswith("rpe_sol_delete_"):
        sol_uid = data.replace("rpe_sol_delete_", "")
        from database.book_queries import delete_solution
        await delete_solution(sol_uid)
        # Refresh solutions
        from database.book_queries import get_book_solutions
        solutions = await get_book_solutions(uid)
        ud["rpe_solutions"] = solutions
        await query.message.edit_text(
            f"🗑 Solution <code>{h(sol_uid)}</code> deleted.\n\n"
            f"{_summary('book', rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard("book", uid, solutions)
        )
        return PE_FIELD

    return PE_SOL


async def pe_sol_file_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud  = context.user_data
    if not msg.document:
        await msg.reply_text("❌ Send a PDF file.")
        return PE_SOL_FILE

    sol_uid = ud["rpe_sol_uid"]
    from database.book_queries import replace_solution_file
    await replace_solution_file(sol_uid, msg.document.file_id)

    uid  = ud["rpe_uid"]
    rec  = ud["rpe_rec"]
    from database.book_queries import get_book_solutions
    solutions = await get_book_solutions(uid)
    ud["rpe_solutions"] = solutions

    await msg.reply_text(
        f"✅ Solution file replaced!\n\n"
        f"{_summary('book', rec)}\n\nChoose a field:",
        parse_mode=HTML,
        reply_markup=_field_keyboard("book", uid, solutions)
    )
    return PE_FIELD


# ── State: PE_CORR — correction sub-menu callbacks ───────────────────────────

async def pe_corr_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rec   = ud["rpe_rec"]

    if data == "rpe_corr_back":
        corrections = ud.get("rpe_corrections", [])
        await query.message.edit_text(
            f"✏️ <b>Edit Solve</b>\n\n{_summary('solve', rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard("solve", uid, corrections=corrections)
        )
        return PE_FIELD

    if data.startswith("rpe_corr_title_"):
        corr_uid = data.replace("rpe_corr_title_", "")
        ud["rpe_corr_uid"]    = corr_uid
        ud["rpe_corr_action"] = "title"
        await query.message.edit_text(
            f"📌 <b>Edit Correction Title</b>\n\n"
            f"<code>{h(corr_uid)}</code>\n\nSend new title (or - to clear):",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back", callback_data="rpe_corr_back")
            ]])
        )
        ud["rpe_field"]  = "title"
        ud["rpe_ftype"]  = "text"
        ud["rpe_flabel"] = "Correction Title"
        ud["rpe_mode"]   = "corr_title"
        return PE_VALUE

    if data.startswith("rpe_corr_replace_"):
        corr_uid = data.replace("rpe_corr_replace_", "")
        ud["rpe_corr_uid"]    = corr_uid
        ud["rpe_corr_action"] = "replace"
        await query.message.edit_text(
            f"🔄 <b>Replace Correction File</b>\n\n"
            f"<code>{h(corr_uid)}</code>\n\nSend the new file:",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back", callback_data="rpe_corr_back")
            ]])
        )
        return PE_CORR_FILE

    if data.startswith("rpe_corr_delete_"):
        corr_uid = data.replace("rpe_corr_delete_", "")
        from database.solve_queries import delete_correction
        await delete_correction(corr_uid)
        from database.solve_queries import get_corrections
        corrections = await get_corrections(uid)
        ud["rpe_corrections"] = corrections
        await query.message.edit_text(
            f"🗑 Correction <code>{h(corr_uid)}</code> deleted.\n\n"
            f"{_summary('solve', rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard("solve", uid, corrections=corrections)
        )
        return PE_FIELD

    return PE_CORR


async def pe_corr_file_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud  = context.user_data
    if not msg.document:
        await msg.reply_text("❌ Send a file.")
        return PE_CORR_FILE

    corr_uid = ud["rpe_corr_uid"]
    ft = "document"
    if msg.document.mime_type and "pdf" in msg.document.mime_type:
        ft = "pdf"
    from database.solve_queries import update_correction_file
    await update_correction_file(corr_uid, msg.document.file_id, ft)

    uid = ud["rpe_uid"]
    rec = ud["rpe_rec"]
    from database.solve_queries import get_corrections
    corrections = await get_corrections(uid)
    ud["rpe_corrections"] = corrections

    await msg.reply_text(
        f"✅ Correction file replaced!\n\n"
        f"{_summary('solve', rec)}\n\nChoose a field:",
        parse_mode=HTML,
        reply_markup=_field_keyboard("solve", uid, corrections=corrections)
    )
    return PE_FIELD


# ── State: PE_MULTI — multi-file management (RegPay / Vidoc) ─────────────────

async def pe_multi_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    ud    = context.user_data
    uid   = ud["rpe_uid"]
    rtype = ud["rpe_rtype"]
    rec   = ud["rpe_rec"]

    if data == "rpe_multi_back":
        solutions   = ud.get("rpe_solutions")
        corrections = ud.get("rpe_corrections")
        await query.message.edit_text(
            f"✏️ <b>Edit {rtype.title()}</b>\n\n{_summary(rtype, rec)}\n\nChoose a field:",
            parse_mode=HTML,
            reply_markup=_field_keyboard(rtype, uid, solutions, corrections)
        )
        return PE_FIELD

    if data == "rpe_multi_add":
        ud["rpe_multi_action"] = "add"
        if rtype == "vidoc":
            prompt = "📝 Send a message or file to add:\n<i>Text, document, photo, video — anything goes.</i>"
        else:
            prompt = "📎 Send a file to add:"
        await query.message.edit_text(
            prompt,
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back", callback_data="rpe_multi_back")
            ]])
        )
        return PE_MULTI  # PE_MULTI handles both text and files

    if data == "rpe_multi_replace":
        ud["rpe_multi_action"] = "replace_all"
        ud["rpe_multi_new"]    = []
        if rtype == "vidoc":
            prompt = "📝 Send new messages one by one (text or file).\nTap <b>Done</b> or send /done when finished."
        else:
            prompt = "📎 Send files one by one. Tap <b>Done</b> or /done when finished."
        await query.message.edit_text(
            prompt,
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done", callback_data="rpe_multi_done")
            ]])
        )
        return PE_MULTI

    if data == "rpe_multi_remove":
        # Show numbered list with meaningful previews
        if rtype == "regpay":
            fids = ud.get("rpe_multi_files", [])
            lines = [f"{i+1}. File (id: ...{fid[-8:]})" for i, fid in enumerate(fids)]
        else:
            msgs = ud.get("rpe_multi_msgs", [])
            lines = []
            for i, m in enumerate(msgs):
                mtype = m.get("type", "?")
                if mtype == "text":
                    preview = (m.get("content") or "")[:50]
                    preview = preview.replace("\n", " ")
                    lines.append(f"{i+1}. [text] {preview or '—'}")
                else:
                    fid = m.get("file_id", "")
                    lines.append(f"{i+1}. [{mtype}] id: ...{fid[-8:]}")
        if not lines:
            await query.answer("No files to remove.", show_alert=True)
            return PE_MULTI

        rows = [[InlineKeyboardButton(f"🗑 Remove #{i+1}", callback_data=f"rpe_multi_rm_{i}")]
                for i in range(len(lines))]
        rows.append([InlineKeyboardButton("← Back", callback_data="rpe_multi_back")])
        text = "Which file to remove?\n\n" + "\n".join(lines)
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
        return PE_MULTI

    if data.startswith("rpe_multi_rm_"):
        idx = int(data.replace("rpe_multi_rm_", ""))
        if rtype == "regpay":
            fids = ud.get("rpe_multi_files", [])
            if 0 <= idx < len(fids):
                fids.pop(idx)
                ud["rpe_multi_files"] = fids
                from database.regpay_queries import update_regpay_files
                await update_regpay_files(uid, fids)
        else:  # vidoc
            msgs = ud.get("rpe_multi_msgs", [])
            if 0 <= idx < len(msgs):
                msgs.pop(idx)
                ud["rpe_multi_msgs"] = msgs
                from database.vidoc_queries import update_vidoc_messages
                await update_vidoc_messages(uid, msgs)

        rec = await _db_get(rtype, uid)
        ud["rpe_rec"] = rec
        file_count = len(ud.get("rpe_multi_files") or ud.get("rpe_multi_msgs") or [])
        await query.message.edit_text(
            f"✅ File removed.\n\nCurrently {file_count} file(s).",
            reply_markup=_multi_file_keyboard(rtype, uid, file_count)
        )
        return PE_MULTI

    if data == "rpe_multi_done":
        # Finalize replace_all
        if ud.get("rpe_multi_action") == "replace_all":
            new_files = ud.get("rpe_multi_new", [])
            if rtype == "regpay":
                from database.regpay_queries import update_regpay_files
                await update_regpay_files(uid, new_files)
                ud["rpe_multi_files"] = new_files
            else:
                from database.vidoc_queries import update_vidoc_messages
                await update_vidoc_messages(uid, new_files)
                ud["rpe_multi_msgs"] = new_files
        rec = await _db_get(rtype, uid)
        ud["rpe_rec"] = rec
        file_count = len(ud.get("rpe_multi_files") or ud.get("rpe_multi_msgs") or [])
        await query.message.edit_text(
            f"✅ Files updated. {file_count} file(s) now.\n\nCurrently {file_count} file(s).",
            reply_markup=_multi_file_keyboard(rtype, uid, file_count)
        )
        return PE_MULTI

    return PE_MULTI


async def pe_multi_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done command while collecting messages in replace_all mode."""
    ud     = context.user_data
    uid    = ud["rpe_uid"]
    rtype  = ud["rpe_rtype"]
    action = ud.get("rpe_multi_action")

    if action == "replace_all":
        new_items = ud.get("rpe_multi_new", [])
        if not new_items:
            await update.message.reply_text("❌ No messages added yet. Send at least one.")
            return PE_MULTI
        if rtype == "regpay":
            from database.regpay_queries import update_regpay_files
            await update_regpay_files(uid, new_items)
            ud["rpe_multi_files"] = new_items
        else:
            from database.vidoc_queries import update_vidoc_messages
            await update_vidoc_messages(uid, new_items)
            ud["rpe_multi_msgs"] = new_items
        count = len(new_items)
    else:
        count = len(ud.get("rpe_multi_files") or ud.get("rpe_multi_msgs") or [])

    rec = await _db_get(rtype, uid)
    ud["rpe_rec"] = rec
    await update.message.reply_text(
        f"✅ Done. {count} message(s) saved.",
        reply_markup=_multi_file_keyboard(rtype, uid, count)
    )
    return PE_MULTI


def _build_vidoc_entry(msg) -> dict:
    """Build a vidoc message entry from a Telegram message — text or file."""
    if msg.document or msg.photo or msg.video or msg.audio:
        if msg.photo:
            fid, ftype = msg.photo[-1].file_id, "photo"
        elif msg.document:
            fid, ftype = msg.document.file_id, "document"
        elif msg.video:
            fid, ftype = msg.video.file_id, "video"
        elif msg.audio:
            fid, ftype = msg.audio.file_id, "audio"
        else:
            return {}
        return {"type": ftype, "file_id": fid, "file_type": ftype}
    else:
        # Text message — preserve formatting entities
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
        return {"type": "text", "content": text, "entities": entity_list}


async def pe_multi_file_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """File or text message received while in multi-file management state."""
    msg    = update.message
    ud     = context.user_data
    uid    = ud["rpe_uid"]
    rtype  = ud["rpe_rtype"]
    action = ud.get("rpe_multi_action", "add")

    if rtype == "regpay":
        # RegPay only accepts files
        if not msg.document:
            await msg.reply_text("❌ Send a file.")
            return PE_MULTI
        file_id = msg.document.file_id
        fids = ud.get("rpe_multi_files", [])
        if action == "add":
            fids.append(file_id)
            ud["rpe_multi_files"] = fids
            from database.regpay_queries import update_regpay_files
            await update_regpay_files(uid, fids)
        elif action == "replace_all":
            new = ud.get("rpe_multi_new", [])
            new.append(file_id)
            ud["rpe_multi_new"] = new
        count = len(ud.get("rpe_multi_files") or ud.get("rpe_multi_new") or [])
    else:
        # Vidoc accepts text AND files
        entry = _build_vidoc_entry(msg)
        if not entry:
            await msg.reply_text("❌ Send a text message or file.")
            return PE_MULTI
        msgs = ud.get("rpe_multi_msgs", [])
        if action == "add":
            msgs.append(entry)
            ud["rpe_multi_msgs"] = msgs
            from database.vidoc_queries import update_vidoc_messages
            await update_vidoc_messages(uid, msgs)
        elif action == "replace_all":
            new = ud.get("rpe_multi_new", [])
            new.append(entry)
            ud["rpe_multi_new"] = new
        count = len(ud.get("rpe_multi_msgs") or ud.get("rpe_multi_new") or [])

    mtype = entry.get("type", "file") if rtype != "regpay" else "file"
    label = f"[{mtype}]" if rtype != "regpay" else "file"

    if action == "replace_all":
        await msg.reply_text(
            f"✅ {label} added ({count} so far). Send more or tap Done.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done", callback_data="rpe_multi_done")
            ]])
        )
    else:
        rec = await _db_get(rtype, uid)
        ud["rpe_rec"] = rec
        await msg.reply_text(
            f"✅ {label} added. Now {count} message(s).",
            reply_markup=_multi_file_keyboard(rtype, uid, count)
        )
    return PE_MULTI


# ── ConversationHandler factory ───────────────────────────────────────────────

def resource_picker_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            # start_edit/start_delete pre-populate user_data and send the menu
            # The first button tap (any rpe_ pattern) starts the conversation
            CallbackQueryHandler(pe_field_cb,  pattern="^rpe_field_"),
            CallbackQueryHandler(pe_field_cb,  pattern="^rpe_done$"),
            CallbackQueryHandler(pe_field_cb,  pattern="^rpe_delconfirm$"),
            CallbackQueryHandler(pe_field_cb,  pattern="^rpe_noop$"),
            CallbackQueryHandler(pe_init_cb,   pattern="^rpe_init_"),   # fallback for old button
        ],
        states={
            PE_FIELD: [
                CallbackQueryHandler(pe_field_cb, pattern="^rpe_"),
            ],
            PE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pe_value_msg),
                CallbackQueryHandler(pe_cancel_cb, pattern="^rpe_done$"),
                CallbackQueryHandler(pe_corr_cb,   pattern="^rpe_corr_back$"),
            ],
            PE_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO |
                    (filters.TEXT & ~filters.COMMAND),
                    pe_file_msg
                ),
                CallbackQueryHandler(pe_cancel_cb, pattern="^rpe_done$"),
            ],
            PE_MULTI: [
                CallbackQueryHandler(pe_multi_cb, pattern="^rpe_multi_"),
                CommandHandler("done", pe_multi_done_cmd),
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO |
                    filters.AUDIO | (filters.TEXT & ~filters.COMMAND),
                    pe_multi_file_msg
                ),
            ],
            PE_SOL: [
                CallbackQueryHandler(pe_sol_cb, pattern="^rpe_sol_"),
            ],
            PE_SOL_FILE: [
                MessageHandler(filters.Document.ALL, pe_sol_file_msg),
                CallbackQueryHandler(pe_sol_cb, pattern="^rpe_sol_back$"),
            ],
            PE_CORR: [
                CallbackQueryHandler(pe_corr_cb, pattern="^rpe_corr_"),
            ],
            PE_CORR_FILE: [
                MessageHandler(filters.Document.ALL, pe_corr_file_msg),
                CallbackQueryHandler(pe_corr_cb, pattern="^rpe_corr_back$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(pe_cancel_cb, pattern="^rpe_done$"),
        ],
        conversation_timeout=600,
        per_message=False,
        allow_reentry=True,
    )