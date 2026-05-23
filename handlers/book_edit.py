"""
Book & Solution Manual edit/delete handlers.
Commands: /editbook, /editsolution, /deletebook, /deletesolution
Admin only.
"""

import logging
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from config.settings import settings
from utils.imgbb import upload_to_imgbb
from database.book_queries import (
    get_book, get_book_solutions, update_book_field,
    update_book_file, replace_solution_file,
    delete_book, delete_solution, update_cover_file
)
from handlers.book import _send_book_preview

logger = logging.getLogger(__name__)

HTML = ParseMode.HTML

# ── States: /editbook ───────────────────────────────────────────────────────────
EB_MENU, EB_VALUE, EB_FILE = range(3)

# ── States: /editsolution ──────────────────────────────────────────────────────
ES_UID, ES_FILE = range(2)

# ── States: /deletebook ────────────────────────────────────────────────────────
DB_CONFIRM = 0

# ── States: /deletesolution ────────────────────────────────────────────────────
DS_UID, DS_CONFIRM = range(2)

# Field labels for edit menu
BOOK_FIELDS = {
    "title":       ("📌", "Title"),
    "authors":     ("✍️", "Authors"),
    "edition":     ("📖", "Edition"),
    "subject":     ("📂", "Subject"),
    "course_codes":("📗", "Course Codes"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "PDF File"),
    "cover":       ("🖼", "Cover Image"),
}


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not settings.is_admin(user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _edit_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in BOOK_FIELDS.items():
        row.append(InlineKeyboardButton(
            f"{emoji} {label}", callback_data=f"eb_field_{key}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="eb_cancel")])
    return InlineKeyboardMarkup(buttons)


def _book_summary(book: dict) -> str:
    ed = f" ({h(book['edition'])})" if book.get("edition") else ""
    sols_note = ""
    return (
        f"📚 <b>{h(book['title'])}{ed}</b>\n"
        f"✍️ {h(book['authors'])}\n"
        f"📂 {h(book.get('subject') or 'N/A')}\n"
        f"📗 {h(book.get('course_codes') or 'N/A')}\n"
        f"🆔 <code>{h(book['uid'])}</code>"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /editbook <uid>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editbook_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/editbook &lt;uid&gt;</code>\nExample: <code>/editbook nm01b</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    book = await get_book(uid)

    if not book:
        await update.message.reply_text(
            f"❌ No book found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_book_uid"] = uid
    context.user_data["edit_book"] = book

    await update.message.reply_text(
        f"✏️ <b>Edit Book</b>\n\n"
        f"{_book_summary(book)}\n\n"
        f"Which field do you want to edit?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EB_MENU


async def editbook_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "eb_cancel":
        await query.answer()
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "eb_back":
        await query.answer()
        book = await get_book(context.user_data["edit_book_uid"])
        context.user_data["edit_book"] = book
        await query.message.edit_text(
            f"✏️ <b>Edit Book</b>\n\n"
            f"{_book_summary(book)}\n\n"
            f"Which field do you want to edit?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EB_MENU

    if data.startswith("eb_field_"):
        field = data.replace("eb_field_", "")
        context.user_data["edit_field"] = field
        emoji, label = BOOK_FIELDS[field]
        book = context.user_data["edit_book"]

        await query.answer()

        if field == "cover":
            await query.message.edit_text(
                f"🖼 <b>Update Cover Image</b>\n\n"
                f"Current book: <b>{h(book['title'])}</b>\n\n"
                f"Send new cover image (JPG/PNG) or <code>-</code> to remove cover:\n"
                f"<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EB_FILE

        if field == "file":
            await query.message.edit_text(
                f"📄 <b>Replace PDF</b>\n\n"
                f"Current book: <b>{h(book['title'])}</b>\n\n"
                f"Send the new PDF file now:\n"
                f"<i>/cancel to go back</i>",
                parse_mode=HTML
            )
            return EB_FILE

        # Get current value
        current = book.get(field)
        if field == "tags":
            import json
            try:
                current = " ".join(json.loads(current)) if isinstance(current, str) else " ".join(current or [])
            except Exception:
                current = ""

        await query.message.edit_text(
            f"{emoji} <b>Edit {label}</b>\n\n"
            f"Current: <code>{h(str(current or 'N/A'))}</code>\n\n"
            f"Send the new value:\n"
            f"<i>/cancel to go back</i>",
            parse_mode=HTML
        )
        return EB_VALUE


async def editbook_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    uid = context.user_data.get("edit_book_uid")
    value = update.message.text.strip()

    if field == "tags":
        import json
        parsed = [t.lower() for t in value.split() if t]
        value = json.dumps(parsed)

    await update_book_field(uid, field, value if value != "-" else None)

    book = await get_book(uid)
    context.user_data["edit_book"] = book

    emoji, label = BOOK_FIELDS[field]
    await update.message.reply_text(
        f"✅ <b>{label}</b> updated!\n\n"
        f"{_book_summary(book)}\n\n"
        f"Edit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    return EB_MENU


async def editbook_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid   = context.user_data.get("edit_book_uid")
    field = context.user_data.get("edit_field")

    # Cover image update
    if field == "cover":
        if msg.text and msg.text.strip() == "-":
            await update_cover_file(uid, None, cover_url=None)
            label = "Cover removed."
        elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
            file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("⏳ Uploading cover image...")
            cover_url = await upload_to_imgbb(context.bot, file_id)
            await uploading.delete()
            await update_cover_file(uid, file_id, cover_url=cover_url or None)
            label = "Cover updated!" if cover_url else "Cover saved (imgBB upload failed — check API key)"
        else:
            await msg.reply_text(
                "❌ Send a cover image (JPG/PNG) or <code>-</code> to remove.",
                parse_mode=HTML
            )
            return EB_FILE

        book = await get_book(uid)
        context.user_data["edit_book"] = book
        await msg.reply_text(
            f"✅ <b>{label}</b>\n\n"
            f"{_book_summary(book)}\n\n"
            f"Edit another field?",
            parse_mode=HTML,
            reply_markup=_edit_menu_keyboard()
        )
        return EB_MENU

    # PDF file update
    if not msg.document:
        await msg.reply_text("❌ Please send a PDF file.")
        return EB_FILE

    await update_book_file(uid, msg.document.file_id)
    book = await get_book(uid)
    context.user_data["edit_book"] = book

    await msg.reply_text(
        f"✅ <b>PDF replaced!</b>\n\n"
        f"{_book_summary(book)}\n\n"
        f"Edit another field?",
        parse_mode=HTML,
        reply_markup=_edit_menu_keyboard()
    )
    await _send_book_preview(msg.chat_id, uid, context.bot)
    return EB_MENU


async def editbook_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Edit cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def editbook_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editbook", editbook_start)],
        states={
            EB_MENU: [
                CallbackQueryHandler(editbook_menu_callback, pattern="^eb_")
            ],
            EB_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editbook_get_value),
                CallbackQueryHandler(editbook_menu_callback, pattern="^eb_")
            ],
            EB_FILE: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
                    editbook_get_file
                ),
                CallbackQueryHandler(editbook_menu_callback, pattern="^eb_")
            ],
        },
        fallbacks=[CommandHandler("cancel", editbook_cancel)],
        conversation_timeout=300,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /editsolution <uid>  — file replace only
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def editsolution_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/editsolution &lt;uid&gt;</code>\nExample: <code>/editsolution nm01s01</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()

    # Find the solution and its book
    from database.book_queries import get_solution
    sol = await get_solution(uid)

    if not sol:
        await update.message.reply_text(
            f"❌ No solution manual found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    book = await get_book(sol["book_uid"])
    context.user_data["edit_sol_uid"] = uid
    context.user_data["edit_sol_book"] = book

    ed = f" ({h(book['edition'])})" if book and book.get("edition") else ""
    await update.message.reply_text(
        f"📋 <b>Replace Solution Manual</b>\n\n"
        f"UID: <code>{h(uid)}</code>\n"
        f"Book: <b>{h(book['title'] if book else 'Unknown')}{ed}</b>\n\n"
        f"Send the new PDF file now:\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return ES_FILE


async def editsolution_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg.document:
        await msg.reply_text("❌ Please send a PDF file.")
        return ES_FILE

    uid = context.user_data["edit_sol_uid"]
    book = context.user_data["edit_sol_book"]

    await replace_solution_file(uid, msg.document.file_id)

    ed = f" ({h(book['edition'])})" if book and book.get("edition") else ""
    await msg.reply_text(
        f"✅ <b>Solution manual PDF replaced!</b>\n\n"
        f"UID: <code>{h(uid)}</code>\n"
        f"Book: <b>{h(book['title'] if book else 'Unknown')}{ed}</b>\n\n"
        f"<i>Sending preview...</i>",
        parse_mode=HTML
    )

    if book:
        await _send_book_preview(msg.chat_id, book["uid"], context.bot)

    context.user_data.clear()
    return ConversationHandler.END


async def editsolution_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def editsolution_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editsolution", editsolution_start)],
        states={
            ES_FILE: [MessageHandler(filters.Document.ALL, editsolution_get_file)]
        },
        fallbacks=[CommandHandler("cancel", editsolution_cancel)],
        conversation_timeout=300,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletebook <uid>  — UID typed confirmation
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletebook_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/deletebook &lt;uid&gt;</code>\nExample: <code>/deletebook nm01b</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    book = await get_book(uid)

    if not book:
        await update.message.reply_text(
            f"❌ No book found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    solutions = await get_book_solutions(uid)
    sol_count = len(solutions)
    ed = f" ({h(book['edition'])})" if book.get("edition") else ""

    context.user_data["delete_book_uid"] = uid

    await update.message.reply_text(
        f"⚠️ <b>Delete Book</b>\n\n"
        f"📚 <b>{h(book['title'])}{ed}</b>\n"
        f"✍️ {h(book['authors'])}\n"
        f"🆔 <code>{h(uid)}</code>\n\n"
        f"{'⚠️ This will also delete <b>' + str(sol_count) + ' solution manual(s)</b>.' if sol_count else '📭 No solution manuals attached.'}\n\n"
        f"<b>Type the book UID <code>{h(uid)}</code> to confirm deletion:</b>\n"
        f"<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DB_CONFIRM


async def deletebook_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid = context.user_data.get("delete_book_uid")

    if typed != uid:
        await update.message.reply_text(
            f"❌ UID doesn't match. Type exactly <code>{h(uid)}</code> to confirm:",
            parse_mode=HTML
        )
        return DB_CONFIRM

    solutions = await get_book_solutions(uid)
    sol_count = len(solutions)
    book = await get_book(uid)

    await delete_book(uid)  # CASCADE deletes solutions too

    ed = f" ({h(book['edition'])})" if book and book.get("edition") else ""
    await update.message.reply_text(
        f"🗑 <b>Deleted successfully!</b>\n\n"
        f"📚 <b>{h(book['title'] if book else uid)}{ed}</b>\n"
        f"🆔 <code>{h(uid)}</code>\n"
        f"{'📋 ' + str(sol_count) + ' solution manual(s) also deleted.' if sol_count else ''}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


async def deletebook_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Deletion cancelled. Book is safe.")
    context.user_data.clear()
    return ConversationHandler.END


def deletebook_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletebook", deletebook_start)],
        states={
            DB_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletebook_confirm)]
        },
        fallbacks=[CommandHandler("cancel", deletebook_cancel)],
        conversation_timeout=120,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /deletesolution <uid>  — UID typed confirmation
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def deletesolution_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args

    if not args:
        await update.message.reply_text(
            "Usage: <code>/deletesolution &lt;uid&gt;</code>\nExample: <code>/deletesolution nm01s01</code>",
            parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()

    from database.book_queries import get_solution
    sol = await get_solution(uid)

    if not sol:
        await update.message.reply_text(
            f"❌ No solution manual found with UID <code>{h(uid)}</code>.",
            parse_mode=HTML
        )
        return ConversationHandler.END

    book = await get_book(sol["book_uid"])
    ed = f" ({h(book['edition'])})" if book and book.get("edition") else ""
    context.user_data["delete_sol_uid"] = uid
    context.user_data["delete_sol_book"] = book

    await update.message.reply_text(
        f"⚠️ <b>Delete Solution Manual</b>\n\n"
        f"📋 UID: <code>{h(uid)}</code>\n"
        f"📚 Book: <b>{h(book['title'] if book else 'Unknown')}{ed}</b>\n\n"
        f"<b>Type the solution UID <code>{h(uid)}</code> to confirm deletion:</b>\n"
        f"<i>/cancel to abort</i>",
        parse_mode=HTML
    )
    return DS_CONFIRM


async def deletesolution_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid = context.user_data.get("delete_sol_uid")
    book = context.user_data.get("delete_sol_book")

    if typed != uid:
        await update.message.reply_text(
            f"❌ UID doesn't match. Type exactly <code>{h(uid)}</code> to confirm:",
            parse_mode=HTML
        )
        return DS_CONFIRM

    await delete_solution(uid)

    ed = f" ({h(book['edition'])})" if book and book.get("edition") else ""
    await update.message.reply_text(
        f"🗑 <b>Solution manual deleted!</b>\n\n"
        f"📋 <code>{h(uid)}</code>\n"
        f"📚 Book: <b>{h(book['title'] if book else 'Unknown')}{ed}</b>\n\n"
        f"<i>Sending updated book preview...</i>",
        parse_mode=HTML
    )

    if book:
        await _send_book_preview(
            update.message.chat_id, book["uid"], context.bot
        )

    context.user_data.clear()
    return ConversationHandler.END


async def deletesolution_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Deletion cancelled. Solution manual is safe.")
    context.user_data.clear()
    return ConversationHandler.END


def deletesolution_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("deletesolution", deletesolution_start)],
        states={
            DS_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletesolution_confirm)]
        },
        fallbacks=[CommandHandler("cancel", deletesolution_cancel)],
        conversation_timeout=120,
        per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /listbooks  — paginated, 5 per page
# /listsolutions <book_uid>
# ═══════════════════════════════════════════════════════════════════════════════

PAGE_SIZE = 5


@admin_only
async def listbooks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_books_page(update, context, page=0, edit=False)


async def _show_books_page(update, context, page: int, edit: bool):
    from database.book_queries import get_books_paginated, get_books_count

    total = await get_books_count()
    if total == 0:
        text = "📚 <b>Books</b>\n\n<i>No books uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset = page * PAGE_SIZE
    books = await get_books_paginated(offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"📚 <b>Books</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for b in books:
        ed = f" ({h(b['edition'])})" if b.get("edition") else ""
        sol_count = b.get("solution_count", 0)
        sol_badge = f" [{sol_count}S]" if sol_count else ""
        lines.append(
            f"🆔 <code>{h(b['uid'])}</code>{sol_badge}\n"
            f"   📖 {h(b['title'])}{ed}\n"
            f"   ✍️ {h(b['authors'])}\n"
        )

    text = "\n".join(lines)
    text += f"\n<i>[nS] = n solution manuals</i>"

    # Pagination buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"lb_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="lb_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"lb_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None

    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            text, parse_mode=HTML, reply_markup=keyboard
        )


async def listbooks_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lb_noop":
        await query.answer()
        return
    page = int(query.data.replace("lb_page_", ""))
    await query.answer()
    await _show_books_page(update, context, page=page, edit=True)


@admin_only
async def listsolutions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/listsolutions &lt;book_uid&gt;</code>\n"
            "Example: <code>/listsolutions nm01b</code>",
            parse_mode=HTML
        )
        return

    book_uid = args[0].strip().lower()
    book = await get_book(book_uid)

    if not book:
        await update.message.reply_text(
            f"❌ No book found with UID <code>{h(book_uid)}</code>.",
            parse_mode=HTML
        )
        return

    solutions = await get_book_solutions(book_uid)
    ed = f" ({h(book['edition'])})" if book.get("edition") else ""

    if not solutions:
        await update.message.reply_text(
            f"📚 <b>{h(book['title'])}{ed}</b>\n"
            f"🆔 <code>{h(book_uid)}</code>\n\n"
            f"<i>No solution manuals uploaded yet.</i>",
            parse_mode=HTML
        )
        return

    lines = [
        f"📚 <b>{h(book['title'])}{ed}</b>\n"
        f"🆔 <code>{h(book_uid)}</code>\n\n"
        f"📋 <b>Solution Manuals ({len(solutions)})</b>\n"
    ]
    for i, sol in enumerate(solutions, 1):
        lines.append(
            f"{i}. <code>{h(sol['uid'])}</code>\n"
            f"   📅 {sol['created_at'].strftime('%Y-%m-%d')}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=HTML)