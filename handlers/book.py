"""
Book & Solution Manual upload handler.
Commands: /addbook <uid>, /addsolution <book_uid> <sol_uid>
Admin only. UID provided with command — no separate UID step.
"""

import logging
import re
from utils.stars import award_download
import asyncio
from html import escape as h
from telegram import Update, InputMediaDocument, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from config.settings import settings
from database.book_queries import (
    book_uid_exists, insert_book, get_book,
    get_book_solutions, solution_uid_exists, insert_solution,
    increment_book_access, increment_solution_access
)
from database.queries import get_current_semester
from utils.imgbb import upload_to_imgbb

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

STOP_WORDS = {
    "a","an","the","of","and","or","for","in","to","with","on","at","by","from",
    "as","is","it","its","be","are","was","were","been","has","have","had",
    "not","no","nor","but","so","yet","both","either","neither","each","every",
    "all","any","few","more","most","other","some","such","than","too","very",
}


# ── States: /addbook ────────────────────────────────────────────────────────────
AB_FILE, AB_INFO, AB_COVER, AB_TAGS = range(4)

# addsolution uses user_data state machine (no ConversationHandler)
# _sol_step: "search" | "file"


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not settings.is_admin(user.id):
            await update.effective_message.reply_text("Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _parse_info(text: str) -> dict:
    """Parse newline-separated format:
    Line 1: Title
    Line 2: Author1 | Author2
    Line 3: Edition (optional, - to skip)
    Line 4: CSE315, CSE317 (optional, - to skip)
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return {
        "title":   lines[0] if len(lines) > 0 else "",
        "authors": lines[1] if len(lines) > 1 else "",
        "edition": lines[2] if len(lines) > 2 and lines[2] != "-" else None,
        "courses": lines[3] if len(lines) > 3 and lines[3] != "-" else None,
    }


async def _get_course_info(course_codes_str: str) -> list[dict]:
    """Fetch course name + abbr for given codes from current semester."""
    if not course_codes_str:
        return []
    codes = [c.strip().upper() for c in course_codes_str.split(",") if c.strip()]
    from database.queries import get_current_semester
    sem = await get_current_semester()
    if not sem:
        return [{"code": c, "name": "", "abbr": ""} for c in codes]
    import json as _j
    courses = _j.loads(sem["courses"]) if isinstance(sem["courses"], str) else (sem["courses"] or [])
    course_map = {c["code"].upper(): c for c in courses}
    result = []
    for code in codes:
        info = course_map.get(code, {})
        result.append({
            "code": code,
            "name": info.get("name", ""),
            "abbr": info.get("abbr", ""),
        })
    return result


async def _generate_uid(abbr: str) -> str:
    """Generate UID: abbr + zero-padded serial + b. e.g. swe01b"""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Extract max existing serial for this abbr to avoid conflicts
        rows = await conn.fetch(
            "SELECT uid FROM books WHERE uid LIKE $1",
            f"{abbr}%b"
        )
    max_serial = 0
    import re as _re
    pattern = _re.compile(rf"^{_re.escape(abbr)}(\d+)b$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    serial = max_serial + 1
    return f"{abbr}{serial:02d}b"


def _auto_tags(title: str, authors: str, courses: list[dict]) -> list[str]:
    """Generate tags from title words, author names, course info."""
    tags = set()
    tags.add("book")

    # Title words — strip stop words and symbols
    words = re.sub(r"[^a-z0-9\s]", " ", title.lower()).split()
    tags.update(w for w in words if w not in STOP_WORDS and len(w) > 1)

    # Author last names
    for author in re.split(r"[|,]", authors):
        parts = author.strip().split()
        if parts:
            tags.add(parts[-1].lower())

    # Course info
    for c in courses:
        if c["code"]:  tags.add(c["code"].lower())
        if c["abbr"]:  tags.add(c["abbr"].lower())
        if c["name"]:
            for w in c["name"].lower().split():
                if w not in STOP_WORDS and len(w) > 1:
                    tags.add(w)

    return sorted(tags)


# ═══════════════════════════════════════════════════════════════════════════════
# /addbook
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def addbook_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Book</b>\n\n"
        "<i>Step 1 of 4</i>\n\n"
        "Send the book PDF.",
        parse_mode=HTML
    )
    return AB_FILE


async def addbook_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.document:
        await msg.reply_text("Please send a PDF file.")
        return AB_FILE

    context.user_data["file_id"] = msg.document.file_id
    await msg.reply_text(
        "<i>Step 2 of 4</i>\n\n"
        "Send book info — each on a new line:\n\n"
        "<code>Title\n"
        "Author1 | Author2\n"
        "Edition\n"
        "CSE315, CSE317</code>\n\n"
        "Edition and course codes are optional — use <code>-</code> to skip.\n\n"
        "Example:\n"
        "<code>Data Communications and Networking\n"
        "Behrouz A. Forouzan\n"
        "5th\n"
        "CSE321, CSE322</code>",
        parse_mode=HTML
    )
    return AB_INFO


async def addbook_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    info = _parse_info(text)

    if not info["title"] or not info["authors"]:
        await update.message.reply_text(
            "Invalid format. Use:\n\n"
            "<code>Title\nAuthor(s)\nEdition\nCourse codes</code>",
            parse_mode=HTML
        )
        return AB_INFO

    # Fetch course info from DB
    courses = await _get_course_info(info["courses"] or "")

    # Auto-generate UID
    if courses and courses[0]["abbr"]:
        abbr = courses[0]["abbr"].lower()
    else:
        abbr = "gen"
    uid = await _generate_uid(abbr)

    # Auto-generate tags
    auto_tags = _auto_tags(info["title"], info["authors"], courses)

    context.user_data.update({
        "title":    info["title"],
        "authors":  info["authors"],
        "edition":  info["edition"],
        "courses":  courses,
        "course_codes": info["courses"],
        "uid":      uid,
        "auto_tags": auto_tags,
    })

    course_lines = "\n".join(
        f"  {c['code']} — {c['name']} ({c['abbr']})" for c in courses
    ) or "  None"

    await update.message.reply_text(
        f"<i>Step 3 of 4</i>\n\n"
        f"<b>Preview:</b>\n"
        f"UID: <code>{uid}</code>\n"
        f"Title: {h(info['title'])}\n"
        f"Authors: {h(info['authors'])}\n"
        f"Edition: {h(info['edition'] or '—')}\n"
        f"Courses:\n{h(course_lines)}\n\n"
        f"Send the cover image (JPG/PNG), or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AB_COVER


async def addbook_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg.text and msg.text.strip() == "-":
        context.user_data["cover_file_id"] = None
        context.user_data["cover_url"]     = None
    elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
        file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        cover_url = await upload_to_imgbb(context.bot, file_id)
        await uploading.delete()
        context.user_data["cover_file_id"] = file_id
        context.user_data["cover_url"]     = cover_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code> to skip.", parse_mode=HTML)
        return AB_COVER

    auto_tags = context.user_data.get("auto_tags", [])
    tag_str   = " ".join(auto_tags)
    await msg.reply_text(
        f"<i>Step 4 of 4</i>\n\n"
        f"<b>Auto-generated tags:</b>\n"
        f"<code>{h(tag_str)}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return AB_TAGS


async def addbook_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User sends custom tags — store and wait for /done."""
    raw = update.message.text.strip().lower()
    ud  = context.user_data
    if raw:
        ud["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text(
            f"Tags updated. Send /done to confirm.",
        )
    return AB_TAGS


async def addbook_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called on /done — finalize and save."""
    ud   = context.user_data
    tags = ud.get("custom_tags") or ud.get("auto_tags", [])

    uid          = ud["uid"]
    title        = ud["title"]
    authors      = ud["authors"]
    edition      = ud.get("edition")
    course_codes = ud.get("course_codes")
    cover_fid    = ud.get("cover_file_id")
    cover_url    = ud.get("cover_url") or ""
    user         = update.effective_user

    from database.queries import get_current_semester
    semester    = await get_current_semester()
    semester_id = semester["id"] if semester else None

    courses = ud.get("courses", [])
    subject = ", ".join(c["name"] for c in courses if c["name"]) or title

    await insert_book(
        uid, title, authors, edition,
        subject, course_codes, ud["file_id"], tags, user.id, semester_id,
        cover_file_id=cover_fid,
        cover_url=cover_url or None
    )

    from utils.notify import notify_resource
    first_course = (course_codes or "").split(",")[0].strip() or None
    await notify_resource(
        bot=update.get_bot(), category="book",
        course_code=first_course, title=title, uid=uid,
        extra=f"Courses: {course_codes}" if course_codes else ""
    )

    ed_str   = f" ({h(edition)})" if edition else ""
    tag_str  = " ".join([f"#{t}" for t in tags]) or "none"
    sem_name = h(semester["name"]) if semester else "—"

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Edit",   callback_data=f"book_edit_{uid}"),
        InlineKeyboardButton("Delete", callback_data=f"book_delete_{uid}"),
    ]])

    course_display = ", ".join(
        f"{c['code']} ({c['abbr']})" for c in courses
    ) if courses else (course_codes or "—")

    await update.message.reply_text(
        f"<b>Book added.</b>\n\n"
        f"<b>{h(title)}{ed_str}</b>\n"
        f"{h(authors)}\n\n"
        f"<code>UID      : {h(uid)}\n"
        f"Courses  : {h(course_display)}\n"
        f"Semester : {sem_name}</code>\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML,
        reply_markup=keyboard
    )

    await _send_book_preview(update.message.chat_id, uid, context.bot)
    context.user_data.clear()
    return ConversationHandler.END


async def _generate_sol_uid(book_uid: str) -> str:
    """Generate solution UID: book_uid + zero-padded serial + s."""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT uid FROM solution_manuals WHERE book_uid = $1", book_uid
        )
    max_serial = 0
    pattern = re.compile(rf"^{re.escape(book_uid)}(\d+)s$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"{book_uid}{max_serial + 1:02d}s"


@admin_only
async def addsolution_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry — clears state and shows search button."""
    msg  = update.effective_message
    user = update.effective_user

    # Clear any previous sol state
    for k in ["_sol_step", "sol_book_uid", "sol_book", "sol_uid"]:
        context.user_data.pop(k, None)
    context.user_data["_sol_step"] = "search"
    context.user_data["_in_conversation"] = True

    if not hasattr(context.bot, "_sol_pending"):
        context.bot._sol_pending = set()
    context.bot._sol_pending.add(user.id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Search Book", switch_inline_query_current_chat="book ")
    ]])
    await msg.reply_text(
        "<b>Add Solution Manual</b>\n\n"
        "Search for the book to attach the solution to.",
        parse_mode=HTML,
        reply_markup=keyboard
    )


async def addsolution_handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called from _sol_file_handler in main.py when _sol_step == file."""
    if context.user_data.get("_sol_step") != "file":
        return False

    msg = update.message
    if not msg.document:
        await msg.reply_text("Please send a PDF file.")
        return True

    ud       = context.user_data
    sol_uid  = ud["sol_uid"]
    book_uid = ud["sol_book_uid"]
    book     = ud["sol_book"]
    user     = update.effective_user

    await insert_solution(sol_uid, book_uid, msg.document.file_id, user.id)

    ed_str = f" ({h(book['edition'])})" if book.get("edition") else ""
    await msg.reply_text(
        f"<b>Solution manual added.</b>\n\n"
        f"<b>{h(book['title'])}{ed_str}</b>\n"
        f"{h(book['authors'])}\n\n"
        f"<code>UID : {h(sol_uid)}</code>",
        parse_mode=HTML
    )

    await _send_book_preview(msg.chat_id, book_uid, context.bot)

    for k in ["_sol_step", "_in_conversation", "sol_book_uid", "sol_book", "sol_uid"]:
        context.user_data.pop(k, None)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# PREVIEW & DELIVERY
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_book_preview(chat_id: int, book_uid: str, bot):
    book      = await get_book(book_uid)
    solutions = await get_book_solutions(book_uid)

    if not book:
        return

    title   = book["title"]
    authors = book["authors"]
    edition = book.get("edition")
    ed_str  = f" ({edition})" if edition else ""

    book_caption = (
        f"<b>{h(title)}{ed_str}</b>\n"
        f"{h(authors)}"
    )
    sol_caption = (
        f"<b>Solution Manual</b>\n"
        f"{h(title)}{ed_str}\n"
        f"{h(authors)}"
    )

    media = [InputMediaDocument(
        media=book["file_id"],
        caption=book_caption,
        parse_mode="HTML"
    )]
    for sol in solutions:
        media.append(InputMediaDocument(
            media=sol["file_id"],
            caption=sol_caption,
            parse_mode="HTML"
        ))

    for i in range(0, len(media), 10):
        chunk = media[i:i + 10]
        try:
            await bot.send_media_group(chat_id=chat_id, media=chunk)
            if i + 10 < len(media):
                await asyncio.sleep(0.5)
        except TelegramError as e:
            logger.error(f"Media group failed for {book_uid}: {e}")


async def deliver_book(chat_id: int, book_uid: str, bot):
    book      = await get_book(book_uid)
    solutions = await get_book_solutions(book_uid)

    if not book:
        await bot.send_message(chat_id, "Book not found.")
        return

    await increment_book_access(book_uid)
    for sol in solutions:
        await increment_solution_access(sol["uid"])

    await award_download(chat_id, "book", book.get("uploaded_by"), book_uid)
    await _send_book_preview(chat_id, book_uid, bot)


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def addbook_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addbook", addbook_start),
            CallbackQueryHandler(addbook_start, pattern="^adm_add_book$"),
        ],
        states={
            AB_FILE: [MessageHandler(filters.Document.ALL, addbook_file)],
            AB_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbook_info)],
            AB_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addbook_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addbook_cover),
            ],
            AB_TAGS: [
                CommandHandler("done", addbook_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addbook_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )