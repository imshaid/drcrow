"""
Utility CRUD — Academic Calendar, Advisor Info, Fee Overview.
All file types supported. Thumbnail via imgBB. Optional URL button + tags.
No body text — just file + button.

Commands:
  /addcal <uid>      /editcal <uid>      /deletecal <uid>      /listcals
  /addadvisor <uid>  /editadvisor <uid>  /deleteadvisor <uid>  /listadvisors
  /addfee <uid>      /editfee <uid>      /deletefee <uid>      /listfees
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
from database.utility_queries import (
    utility_uid_exists, insert_utility, get_utility,
    update_utility_file, update_utility_thumbnail,
    update_utility_url, update_utility_tags,
    delete_utility, get_utilities_by_category,
    count_utilities_by_category, increment_utility_access,
    CATEGORIES
)
from utils.imgbb import upload_to_imgbb

logger    = logging.getLogger(__name__)
HTML      = ParseMode.HTML
PAGE_SIZE = 5

# States: /add*
AU_FILE, AU_THUMB, AU_URL, AU_URL_TITLE, AU_TAGS = range(5)
# States: /edit*
EU_MENU, EU_FILE, EU_THUMB, EU_URL, EU_URL_TITLE, EU_TAGS = range(6)
# States: /delete*
DU_CONFIRM = 0


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not settings.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("🚫 Admin only.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def _detect_file_type(msg) -> tuple:
    """
    Returns (file_id, file_type).
    Photos uploaded via camera/gallery → "photo" type → use send_photo
    Documents (even image files) uploaded as files → proper type → use send_document
    """
    if msg.photo:
        # Sent as photo — use send_photo for delivery
        return msg.photo[-1].file_id, "photo"
    if msg.document:
        mime = msg.document.mime_type or ""
        fn   = (msg.document.file_name or "").lower()
        if "pdf"         in mime:                              return msg.document.file_id, "pdf"
        if "spreadsheet" in mime or fn.endswith((".xlsx",".xls")): return msg.document.file_id, "excel"
        if "presentation" in mime or fn.endswith(".pptx"):    return msg.document.file_id, "pptx"
        if "word"        in mime or fn.endswith(".docx"):     return msg.document.file_id, "docx"
        if mime.startswith("image"):                          return msg.document.file_id, "image_doc"
        return msg.document.file_id, "document"
    return None, None


def _tag_str(tags_raw) -> str:
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    return " ".join([f"#{t}" for t in tags]) if tags else "none"


def _utility_summary(u: dict) -> str:
    cat   = u.get("category", "")
    emoji, label = CATEGORIES.get(cat, ("📄", "Utility"))
    url_line = ""
    if u.get("url"):
        title    = u.get("url_title") or u["url"]
        url_line = f"\n🔗 {h(title)}"
    return (
        f"{emoji} <b>{label}</b>\n"
        f"🆔 <code>{h(u['uid'])}</code>\n"
        f"📄 File: {'✅ ' + (u.get('file_type') or '').upper() if u.get('file_id') else '—'}\n"
        f"🖼 Thumbnail: {'✅' if u.get('thumbnail_url') else '—'}"
        f"{url_line}\n"
        f"🏷 {h(_tag_str(u.get('tags', [])))}"
    )


def _edit_keyboard(cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 File",        callback_data=f"eu_{cat}_file"),
        InlineKeyboardButton("🖼 Thumbnail",   callback_data=f"eu_{cat}_thumb"),
    ], [
        InlineKeyboardButton("🔗 URL",         callback_data=f"eu_{cat}_url"),
        InlineKeyboardButton("🏷 Tags",        callback_data=f"eu_{cat}_tags"),
    ], [
        InlineKeyboardButton("✖ Cancel",       callback_data=f"eu_{cat}_cancel"),
    ]])


# ═══════════════════════════════════════════════════════════════════════════════
# ADD FLOW
# ═══════════════════════════════════════════════════════════════════════════════

def _make_add_start(category: str):
    emoji, label = CATEGORIES[category]

    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/add{category} &lt;uid&gt;</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        uid = args[0].strip().lower()
        if not uid.replace("-", "").isalnum():
            await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
            return ConversationHandler.END

        if await utility_uid_exists(uid):
            await update.message.reply_text(
                f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
            )
            return ConversationHandler.END

        context.user_data["util_uid"]     = uid
        context.user_data["util_cat"]     = category
        context.user_data["_uploader_id"] = update.effective_user.id

        await update.message.reply_text(
            f"{emoji} <b>Add {label}</b>\n\n"
            f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
            f"Step 1/4 — <b>File</b>\n\n"
            f"Send file (PDF, image, Excel, DOCX, PPTX) or <code>-</code> to skip.\n"
            f"<i>/cancel to stop</i>",
            parse_mode=HTML
        )
        return AU_FILE
    return _start


async def _add_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text(
                "❌ Send a file (PDF, image, Excel, DOCX, PPTX) or <code>-</code> to skip.",
                parse_mode=HTML
            )
            return AU_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "Step 2/4 — <b>Thumbnail</b> (optional)\n\n"
        "Send a cover image for inline search preview.\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU_THUMB


async def _add_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text(
            "❌ Send an image or <code>-</code> to skip.", parse_mode=HTML
        )
        return AU_THUMB

    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()

    context.user_data["thumbnail_url"]  = thumb_url or None
    context.user_data["cover_file_id"]  = fid
    if not thumb_url:
        await msg.reply_text("⚠️ imgBB upload failed.")

    await _ask_url(msg)
    return AU_URL


async def _add_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text(
            "Send a cover image or <code>-</code> to skip.", parse_mode=HTML
        )
        return AU_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_url(update.message)
    return AU_URL


async def _ask_url(msg):
    await msg.reply_text(
        "Step 3/4 — <b>URL</b> (optional)\n\n"
        "A button will be shown with this URL.\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )


async def _add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        context.user_data["url"]       = None
        context.user_data["url_title"] = None
        await _ask_tags(update.message)
        return AU_TAGS

    context.user_data["url"] = text
    await update.message.reply_text(
        "Step 3b — <b>Button Title</b>\n\n"
        "What text should the button show?\n"
        "Example: <code>View Official Site</code>\n"
        "Send <code>-</code> to use URL as text.",
        parse_mode=HTML
    )
    return AU_URL_TITLE


async def _add_url_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["url_title"] = None if text == "-" else text
    await _ask_tags(update.message)
    return AU_TAGS


async def _ask_tags(msg):
    await msg.reply_text(
        "Step 4/4 — <b>Search Tags</b>\n\n"
        "Space-separated keywords.\n"
        "Example: <code>spring 2026 calendar academic uap</code>\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )


async def _add_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_utility(
        ud["util_uid"], ud["util_cat"], tags, ud.get("_uploader_id", 0),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        url=ud.get("url"), url_title=ud.get("url_title")
    )

    u = await get_utility(ud["util_uid"])
    await update.message.reply_text(
        f"✅ <b>Registered!</b>\n\n{_utility_summary(u)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def _make_add_conversation(category: str, command: str) -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler(command, _make_add_start(category))],
        states={
            AU_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, _add_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _add_file),
            ],
            AU_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _add_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _add_thumb_skip),
            ],
            AU_URL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, _add_url)],
            AU_URL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _add_url_title)],
            AU_TAGS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, _add_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ═══════════════════════════════════════════════════════════════════════════════

def _make_edit_start(category: str, command: str):
    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/{command} &lt;uid&gt;</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        uid = args[0].strip().lower()
        u   = await get_utility(uid)
        if not u or u.get("category") != category:
            emoji, label = CATEGORIES[category]
            await update.message.reply_text(
                f"❌ No {label} found: <code>{h(uid)}</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        context.user_data["edit_uid"] = uid
        context.user_data["edit_cat"] = category
        context.user_data["edit_u"]   = u

        await update.message.reply_text(
            f"✏️ <b>Edit {CATEGORIES[category][1]}</b>\n\n"
            f"{_utility_summary(u)}\n\nWhat to edit?",
            parse_mode=HTML, reply_markup=_edit_keyboard(category)
        )
        return EU_MENU
    return _start


async def _edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    data   = query.data
    await query.answer()

    # eu_{cat}_{action}
    parts  = data.split("_", 2)
    action = parts[2] if len(parts) > 2 else ""
    cat    = context.user_data.get("edit_cat", "")

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    prompts = {
        "file":  "📄 <b>Replace File</b>\n\nSend new file or <code>-</code> to remove:",
        "thumb": "🖼 <b>Edit Thumbnail</b>\n\nSend new cover image or <code>-</code> to remove:",
        "url":   "🔗 <b>Edit URL</b>\n\nSend new URL or <code>-</code> to remove:",
        "tags":  "🏷 <b>Edit Tags</b>\n\nSend new tags (space-separated) or <code>-</code> to clear:",
    }
    states = {
        "file": EU_FILE, "thumb": EU_THUMB, "url": EU_URL, "tags": EU_TAGS
    }

    if action in prompts:
        context.user_data["edit_field"] = action
        await query.message.edit_text(prompts[action], parse_mode=HTML)
        return states[action]

    return EU_MENU


async def _edit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    cat = context.user_data["edit_cat"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_file(uid, None, None)
        label = "File removed."
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text(
                "❌ Send a file or <code>-</code>.", parse_mode=HTML
            )
            return EU_FILE
        await update_utility_file(uid, file_id, file_type)
        label = f"File updated! ({file_type.upper()})"

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard(cat)
    )
    return EU_MENU


async def _edit_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    cat = context.user_data["edit_cat"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_thumbnail(uid, None, None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_utility_thumbnail(uid, thumb_url or None, fid)
        label = "Thumbnail updated!" if thumb_url else "Thumbnail saved (imgBB failed)"
    else:
        await msg.reply_text(
            "❌ Send an image or <code>-</code>.", parse_mode=HTML
        )
        return EU_THUMB

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard(cat)
    )
    return EU_MENU


async def _edit_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = context.user_data["edit_uid"]
    cat  = context.user_data["edit_cat"]

    if text == "-":
        await update_utility_url(uid, None, None)
        u = await get_utility(uid)
        context.user_data["edit_u"] = u
        await update.message.reply_text(
            f"✅ <b>URL removed.</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
            parse_mode=HTML, reply_markup=_edit_keyboard(cat)
        )
        return EU_MENU

    context.user_data["pending_url"] = text
    await update.message.reply_text(
        "Send <b>button title</b> or <code>-</code> to use URL as text:",
        parse_mode=HTML
    )
    return EU_URL_TITLE


async def _edit_url_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    uid   = context.user_data["edit_uid"]
    cat   = context.user_data["edit_cat"]
    url   = context.user_data.pop("pending_url", None)
    title = None if text == "-" else text
    await update_utility_url(uid, url, title)

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await update.message.reply_text(
        f"✅ <b>URL updated!</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard(cat)
    )
    return EU_MENU


async def _edit_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    uid  = context.user_data["edit_uid"]
    cat  = context.user_data["edit_cat"]
    await update_utility_tags(uid, tags)

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await update.message.reply_text(
        f"✅ <b>Tags updated!</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_keyboard(cat)
    )
    return EU_MENU


def _make_edit_conversation(category: str, command: str) -> ConversationHandler:
    pattern = f"^eu_{category}_"
    return ConversationHandler(
        entry_points=[CommandHandler(command, _make_edit_start(category, command))],
        states={
            EU_MENU: [CallbackQueryHandler(_edit_callback, pattern=pattern)],
            EU_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                    _edit_file
                ),
                CallbackQueryHandler(_edit_callback, pattern=pattern),
            ],
            EU_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    _edit_thumb
                ),
                CallbackQueryHandler(_edit_callback, pattern=pattern),
            ],
            EU_URL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, _edit_url)],
            EU_URL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _edit_url_title)],
            EU_TAGS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, _edit_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE FLOW
# ═══════════════════════════════════════════════════════════════════════════════

def _make_delete_start(category: str, command: str):
    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/{command} &lt;uid&gt;</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        uid = args[0].strip().lower()
        u   = await get_utility(uid)
        if not u or u.get("category") != category:
            await update.message.reply_text(
                f"❌ Not found: <code>{h(uid)}</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        context.user_data["delete_uid"] = uid
        await update.message.reply_text(
            f"⚠️ <b>Delete {CATEGORIES[category][1]}</b>\n\n"
            f"{_utility_summary(u)}\n\n"
            f"<b>Type <code>{h(uid)}</code> to confirm:</b>\n<i>/cancel to abort</i>",
            parse_mode=HTML
        )
        return DU_CONFIRM
    return _start


async def _delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip().lower()
    uid   = context.user_data["delete_uid"]
    if typed != uid:
        await update.message.reply_text(
            f"❌ Type <code>{h(uid)}</code>:", parse_mode=HTML
        )
        return DU_CONFIRM
    await delete_utility(uid)
    await update.message.reply_text(
        f"🗑 <b>Deleted!</b> 🆔 <code>{h(uid)}</code>", parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def _make_delete_conversation(category: str, command: str) -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler(command, _make_delete_start(category, command))],
        states={DU_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, _delete_confirm)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled. 🦅") or ConversationHandler.END
        ))],
        conversation_timeout=120, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LIST
# ═══════════════════════════════════════════════════════════════════════════════

def _make_list_cmd(category: str):
    @admin_only
    async def _list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _show_page(update, context, category, page=0, edit=False)
    return _list


async def _show_page(update, context, category: str, page: int, edit: bool):
    emoji, label = CATEGORIES[category]
    total = await count_utilities_by_category(category)

    if total == 0:
        text = f"{emoji} <b>{label}</b>\n\n<i>Nothing uploaded yet.</i>"
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(text, parse_mode=HTML)
        return

    offset      = page * PAGE_SIZE
    items       = await get_utilities_by_category(category, offset=offset, limit=PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    cb_prefix   = f"lu_{category}"

    lines = [f"{emoji} <b>{label}</b> — Page {page + 1}/{total_pages} ({total} total)\n"]
    for u in items:
        file_badge  = f"📄 {(u.get('file_type') or '').upper()}" if u.get("file_id") else "—"
        thumb_badge = "🖼✅" if u.get("thumbnail_url") else ""
        url_badge   = "🔗" if u.get("url") else ""
        lines.append(
            f"🆔 <code>{h(u['uid'])}</code>  {file_badge}  {thumb_badge}  {url_badge}\n"
            f"   🏷 {h(_tag_str(u.get('tags', [])))}\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"{cb_prefix}_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"{cb_prefix}_noop"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"{cb_prefix}_page_{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    text = "\n".join(lines)

    if edit:
        await update.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=keyboard)


def _make_list_page_callback(category: str):
    cb_prefix = f"lu_{category}"

    async def _cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query.data == f"{cb_prefix}_noop":
            await query.answer()
            return
        page = int(query.data.replace(f"{cb_prefix}_page_", ""))
        await query.answer()
        await _show_page(update, context, category, page=page, edit=True)
    return _cb


# ═══════════════════════════════════════════════════════════════════════════════
# DELIVERY
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_utility_file(bot, chat_id, file_id, file_type, caption=None, keyboard=None, parse_mode=None):
    """Send a single utility file appropriately."""
    is_photo = file_type == "photo"
    try:
        if is_photo:
            await bot.send_photo(chat_id, file_id, caption=caption,
                                 reply_markup=keyboard, parse_mode=parse_mode)
        else:
            await bot.send_document(chat_id, file_id, caption=caption,
                                    reply_markup=keyboard, parse_mode=parse_mode)
    except Exception:
        try:
            await bot.send_document(chat_id, file_id, caption=caption,
                                    reply_markup=keyboard, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Failed to send file {file_id}: {e}")


async def deliver_utility(chat_id: int, uid: str, bot):
    """Send utility file(s) to DM with optional URL button."""
    u = await get_utility(uid)
    if not u:
        await bot.send_message(chat_id, "❌ Content not found.")
        return

    await increment_utility_access(uid)
    _util_cat = (u.get("category") or "utility").lower()
    await award_download(chat_id, _util_cat, u.get("uploaded_by"), uid)

    # URL button
    keyboard = None
    if u.get("url"):
        btn_title = u.get("url_title") or "🔗 Open Link"
        keyboard  = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_title, url=u["url"])
        ]])

    title = u.get("title") or ""

    # Parse file_ids list
    try:
        raw_ids = u.get("file_ids")
        file_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
    except Exception:
        file_ids = []

    # Build list of (file_id, file_type) to send
    files_to_send = []
    if file_ids:
        # Multiple files — first gets caption+keyboard, rest plain
        for i, entry in enumerate(file_ids):
            fid  = entry.get("file_id") if isinstance(entry, dict) else entry
            ftype = entry.get("file_type", "document") if isinstance(entry, dict) else "document"
            files_to_send.append((fid, ftype))
    elif u.get("file_id"):
        files_to_send.append((u["file_id"], u.get("file_type", "document")))

    if files_to_send:
        import asyncio as _aio
        from telegram import InputMediaDocument, InputMediaPhoto

        if len(files_to_send) == 1:
            fid, ftype = files_to_send[0]
            caption_text = h(title) if title else None
            await _send_utility_file(bot, chat_id, fid, ftype,
                                     caption=caption_text, keyboard=keyboard,
                                     parse_mode="HTML" if caption_text else None)
        else:
            chunks = [files_to_send[i:i+10] for i in range(0, len(files_to_send), 10)]
            first_chunk = True
            for chunk in chunks:
                media_group = []
                for idx, (fid, ftype) in enumerate(chunk):
                    caption_text = h(title) if (first_chunk and idx == 0 and title) else None
                    if ftype == "photo":
                        media_group.append(InputMediaPhoto(
                            media=fid,
                            caption=caption_text,
                            parse_mode="HTML" if caption_text else None
                        ))
                    else:
                        media_group.append(InputMediaDocument(
                            media=fid,
                            caption=caption_text,
                            parse_mode="HTML" if caption_text else None
                        ))
                await bot.send_media_group(chat_id, media=media_group)
                first_chunk = False
                if chunk != chunks[-1]:
                    await _aio.sleep(0.5)
            if keyboard:
                await bot.send_message(chat_id, "\U0001f517", reply_markup=keyboard)
    elif u.get("url"):
        cat   = u.get("category", "")
        emoji, label = CATEGORIES.get(cat, ("📄", "Info"))
        text = f"{emoji} <b>{h(title)}</b>" if title else f"{emoji} {label}"
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    elif not u.get("message_text"):
        await bot.send_message(chat_id, "⚠️ No content available.")

    # Send text message (syllabus description)
    msg_text     = u.get("message_text")
    msg_entities = u.get("message_entities")
    if msg_text:
        from telegram import MessageEntity, LinkPreviewOptions
        tg_entities = []
        if msg_entities:
            try:
                raw = json.loads(msg_entities) if isinstance(msg_entities, str) else msg_entities
                for e in raw:
                    try:
                        tg_entities.append(MessageEntity(
                            type=e["type"], offset=e["offset"], length=e["length"],
                            url=e.get("url"), language=e.get("language")
                        ))
                    except Exception:
                        pass
            except Exception:
                pass
        await bot.send_message(
            chat_id, msg_text,
            entities=tg_entities if tg_entities else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC FACTORIES
# ═══════════════════════════════════════════════════════════════════════════════

def addcal_conversation():      return _make_add_conversation("cal",     "addcal")
def addadvisor_conversation():  return _make_add_conversation("advisor", "addadvisor")
def addfee_conversation():      return _make_add_conversation("fee",     "addfee")

def editcal_conversation():     return _make_edit_conversation("cal",     "editcal")
def editadvisor_conversation(): return _make_edit_conversation("advisor", "editadvisor")
def editfee_conversation():     return _make_edit_conversation("fee",     "editfee")

def deletecal_conversation():     return _make_delete_conversation("cal",     "deletecal")
def deleteadvisor_conversation(): return _make_delete_conversation("advisor", "deleteadvisor")
def deletefee_conversation():     return _make_delete_conversation("fee",     "deletefee")

listcals_cmd     = _make_list_cmd("cal")
listadvisors_cmd = _make_list_cmd("advisor")
listfees_cmd     = _make_list_cmd("fee")

listcals_page_callback     = _make_list_page_callback("cal")
listadvisors_page_callback = _make_list_page_callback("advisor")
listfees_page_callback     = _make_list_page_callback("fee")


# ── Syllabus / Outline / Routine public factories ──────────────────────────────
# These reuse the exact same generic flows as cal/advisor/fee
# Commands registered directly in main.py via _make_*_conversation factories


# ═══════════════════════════════════════════════════════════════════════════════
# EXTENDED ADD FLOW — for Syllabus, Course Outline, Exam Routine
# Extra fields: title, subject, course_code (before file step)
# ═══════════════════════════════════════════════════════════════════════════════

AU2_TITLE, AU2_SUBJECT, AU2_COURSE, AU2_FILE, AU2_THUMB, AU2_TAGS = range(6)


def _make_add_extended_start(category: str):
    emoji, label = CATEGORIES[category]

    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/add{category} &lt;uid&gt;</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        uid = args[0].strip().lower()
        if not uid.replace("-", "").isalnum():
            await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
            return ConversationHandler.END

        if await utility_uid_exists(uid):
            await update.message.reply_text(
                f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
            )
            return ConversationHandler.END

        context.user_data["util_uid"]     = uid
        context.user_data["util_cat"]     = category
        context.user_data["_uploader_id"] = update.effective_user.id

        await update.message.reply_text(
            f"{emoji} <b>Add {label}</b>\n\n"
            f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
            f"Step 1/5 — <b>Title</b>\n\n"
            f"Example: <code>CSE311 Syllabus Spring 2026</code>\n"
            f"Send <code>-</code> to skip.\n"
            f"<i>/cancel to stop</i>",
            parse_mode=HTML
        )
        return AU2_TITLE
    return _start


async def _add2_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["title"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 2/5 — <b>Subject</b>\n\n"
        "Example: <code>Database Management Systems</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AU2_SUBJECT


async def _add2_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["subject"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 3/5 — <b>Course Code</b>\n\n"
        "Example: <code>CSE311</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AU2_COURSE


async def _add2_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["course_code"] = None if text == "-" else text.upper()
    await update.message.reply_text(
        "Step 4/5 — <b>File</b> (optional)\n\n"
        "Send file (PDF, image, Excel, DOCX, PPTX) or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU2_FILE


async def _add2_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text(
                "❌ Send a file or <code>-</code> to skip.", parse_mode=HTML
            )
            return AU2_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "Step 5/5 — <b>Thumbnail</b> (optional)\n\n"
        "Send cover image for inline search preview or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU2_THUMB


async def _add2_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return AU2_THUMB
    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid
    await _ask_tags(msg)
    return AU2_TAGS


async def _add2_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text("Send an image or <code>-</code>.", parse_mode=HTML)
        return AU2_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_tags(update.message)
    return AU2_TAGS


async def _add2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import insert_utility
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_utility(
        ud["util_uid"], ud["util_cat"], tags, ud.get("_uploader_id", 0),
        title=ud.get("title"), subject=ud.get("subject"), course_code=ud.get("course_code"),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id")
    )

    u = await get_utility(ud["util_uid"])
    await update.message.reply_text(
        f"✅ <b>Registered!</b>\n\n{_utility_summary(u)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def _make_add_extended_conversation(category: str, command: str) -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler(command, _make_add_extended_start(category))],
        states={
            AU2_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_title)],
            AU2_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_subject)],
            AU2_COURSE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_course)],
            AU2_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, _add2_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_file),
            ],
            AU2_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _add2_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_thumb_skip),
            ],
            AU2_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _add2_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# Edit menu for extended categories also has Title/Subject/Course buttons
EU2_MENU, EU2_VALUE, EU2_FILE, EU2_THUMB = range(4)

EXTENDED_EDIT_FIELDS = {
    "title":       ("📌", "Title"),
    "subject":     ("📂", "Subject"),
    "course_code": ("📗", "Course Code"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "File"),
    "cover":       ("🖼", "Thumbnail"),
}


def _edit_extended_keyboard(cat: str) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in EXTENDED_EDIT_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"eu2_{cat}_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data=f"eu2_{cat}_cancel")])
    return InlineKeyboardMarkup(buttons)


def _make_edit_extended_start(category: str, command: str):
    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/{command} &lt;uid&gt;</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        uid = args[0].strip().lower()
        u   = await get_utility(uid)
        if not u or u.get("category") != category:
            emoji, label = CATEGORIES[category]
            await update.message.reply_text(
                f"❌ No {label} found: <code>{h(uid)}</code>", parse_mode=HTML
            )
            return ConversationHandler.END

        context.user_data["edit_uid"] = uid
        context.user_data["edit_cat"] = category
        context.user_data["edit_u"]   = u

        await update.message.reply_text(
            f"✏️ <b>Edit {CATEGORIES[category][1]}</b>\n\n"
            f"{_utility_summary(u)}\n\nWhat to edit?",
            parse_mode=HTML, reply_markup=_edit_extended_keyboard(category)
        )
        return EU2_MENU
    return _start


async def _edit2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata
    query  = update.callback_query
    data   = query.data
    await query.answer()

    parts  = data.split("_", 2)
    action = parts[2] if len(parts) > 2 else ""
    cat    = context.user_data.get("edit_cat", "")
    u      = context.user_data.get("edit_u", {})
    uid    = context.user_data.get("edit_uid", "")

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    text_fields = {
        "title":       ("📌", "Title"),
        "subject":     ("📂", "Subject"),
        "course_code": ("📗", "Course Code"),
        "tags":        ("🏷", "Tags"),
    }

    if action in text_fields:
        context.user_data["edit_field"] = action
        emoji, label = text_fields[action]
        current = u.get(action) or "N/A"
        await query.message.edit_text(
            f"{emoji} <b>Edit {label}</b>\n\n"
            f"Current: <code>{h(str(current))}</code>\n\n"
            f"Send new value or <code>-</code> to clear:",
            parse_mode=HTML
        )
        return EU2_VALUE

    if action == "file":
        context.user_data["edit_field"] = "file"
        await query.message.edit_text(
            "📄 <b>Replace File</b>\n\nSend new file or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return EU2_FILE

    if action == "cover":
        context.user_data["edit_field"] = "cover"
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\nSend new image or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return EU2_THUMB

    return EU2_MENU


async def _edit2_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata
    field = context.user_data.get("edit_field")
    uid   = context.user_data["edit_uid"]
    cat   = context.user_data["edit_cat"]
    value = update.message.text.strip()

    if field == "tags":
        tags = [t.lower() for t in value.split() if t] if value != "-" else []
        await update_utility_tags(uid, tags)
    elif field == "course_code":
        await update_utility_metadata(
            uid,
            context.user_data["edit_u"].get("title"),
            context.user_data["edit_u"].get("subject"),
            None if value == "-" else value.upper()
        )
    else:
        # title or subject
        u = await get_utility(uid)
        new_vals = {
            "title":   None if value == "-" else value,
            "subject": u.get("subject"),
        }
        if field == "subject":
            new_vals = {"title": u.get("title"), "subject": None if value == "-" else value}
        await update_utility_metadata(uid, new_vals["title"], new_vals["subject"],
                                       u.get("course_code"))

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    _, label = EXTENDED_EDIT_FIELDS[field]
    await update.message.reply_text(
        f"✅ <b>{label} updated!</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_extended_keyboard(cat)
    )
    return EU2_MENU


async def _edit2_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    cat = context.user_data["edit_cat"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_file(uid, None, None)
        label = "File removed."
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return EU2_FILE
        await update_utility_file(uid, file_id, file_type)
        label = f"File updated! ({file_type.upper()})"

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_extended_keyboard(cat)
    )
    return EU2_MENU


async def _edit2_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    cat = context.user_data["edit_cat"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_thumbnail(uid, None, None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_utility_thumbnail(uid, thumb_url or None, fid)
        label = "Thumbnail updated!" if thumb_url else "Saved (imgBB failed)"
    else:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return EU2_THUMB

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_edit_extended_keyboard(cat)
    )
    return EU2_MENU


def _make_edit_extended_conversation(category: str, command: str) -> ConversationHandler:
    pattern = f"^eu2_{category}_"
    return ConversationHandler(
        entry_points=[CommandHandler(command, _make_edit_extended_start(category, command))],
        states={
            EU2_MENU: [CallbackQueryHandler(_edit2_callback, pattern=pattern)],
            EU2_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _edit2_value),
                CallbackQueryHandler(_edit2_callback, pattern=pattern),
            ],
            EU2_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                    _edit2_file
                ),
                CallbackQueryHandler(_edit2_callback, pattern=pattern),
            ],
            EU2_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    _edit2_thumb
                ),
                CallbackQueryHandler(_edit2_callback, pattern=pattern),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SYLLABUS-SPECIFIC ADD FLOW — file + text message support
# ═══════════════════════════════════════════════════════════════════════════════

AS_TITLE, AS_SUBJECT, AS_COURSE, AS_FILE, AS_MESSAGE, AS_THUMB, AS_TAGS = range(7)


@admin_only
async def addsyllabus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/addsyllabus &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    if not uid.replace("-", "").isalnum():
        await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
        return ConversationHandler.END

    if await utility_uid_exists(uid):
        await update.message.reply_text(
            f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["util_uid"]     = uid
    context.user_data["util_cat"]     = "syllabus"
    context.user_data["_uploader_id"] = update.effective_user.id

    await update.message.reply_text(
        f"📋 <b>Add Syllabus</b>\n\n"
        f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
        f"Step 1/6 — <b>Title</b>\n\n"
        f"Example: <code>CSE311 Syllabus Spring 2026</code>\n"
        f"Send <code>-</code> to skip.\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return AS_TITLE


async def _syl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["title"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 2/6 — <b>Subject</b>\n\n"
        "Example: <code>Database Management Systems</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AS_SUBJECT


async def _syl_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["subject"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 3/6 — <b>Course Code</b>\n\n"
        "Example: <code>CSE311</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AS_COURSE


async def _syl_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["course_code"] = None if text == "-" else text.upper()
    await update.message.reply_text(
        "Step 4/6 — <b>File</b> (optional)\n\n"
        "Send file (PDF, image, DOCX, PPTX, Excel) or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AS_FILE


async def _syl_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return AS_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "Step 5/6 — <b>Syllabus Text</b> (optional)\n\n"
        "Send your formatted syllabus message.\n"
        "Bold, links, quotes — all formatting will be preserved.\n\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AS_MESSAGE


async def _syl_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive text message — preserve formatting entities."""
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["message_text"]     = None
        context.user_data["message_entities"] = None
    else:
        text     = msg.text or ""
        entities = msg.entities or []
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
        context.user_data["message_text"]     = text
        context.user_data["message_entities"] = entity_list

    await msg.reply_text(
        "Step 6/6 — <b>Thumbnail</b> (optional)\n\n"
        "Send cover image for inline search preview or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AS_THUMB


async def _syl_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return AS_THUMB
    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid
    await _ask_tags(msg)
    return AS_TAGS


async def _syl_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text("Send an image or <code>-</code>.", parse_mode=HTML)
        return AS_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_tags(update.message)
    return AS_TAGS


async def _syl_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import insert_utility
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_utility(
        ud["util_uid"], "syllabus", tags, ud.get("_uploader_id", 0),
        title=ud.get("title"), subject=ud.get("subject"), course_code=ud.get("course_code"),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        message_text=ud.get("message_text"), message_entities=ud.get("message_entities")
    )

    u = await get_utility(ud["util_uid"])
    await update.message.reply_text(
        f"✅ <b>Syllabus registered!</b>\n\n{_utility_summary(u)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addsyllabus_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addsyllabus", addsyllabus_start)],
        states={
            AS_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_title)],
            AS_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_subject)],
            AS_COURSE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_course)],
            AS_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, _syl_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_file),
            ],
            AS_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_message_text)],
            AS_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _syl_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_thumb_skip),
            ],
            AS_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# Syllabus edit — add "📝 Message" button to edit menu
ESY_MENU, ESY_VALUE, ESY_FILE, ESY_THUMB, ESY_MESSAGE = range(5)

SYLLABUS_EDIT_FIELDS = {
    "title":       ("📌", "Title"),
    "subject":     ("📂", "Subject"),
    "course_code": ("📗", "Course Code"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "File"),
    "cover":       ("🖼", "Thumbnail"),
    "message":     ("📝", "Text Message"),
}


def _syllabus_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in SYLLABUS_EDIT_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"esy_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="esy_cancel")])
    return InlineKeyboardMarkup(buttons)


@admin_only
async def editsyllabus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/editsyllabus &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid = args[0].strip().lower()
    u   = await get_utility(uid)
    if not u or u.get("category") != "syllabus":
        await update.message.reply_text(f"❌ No Syllabus found: <code>{h(uid)}</code>", parse_mode=HTML)
        return ConversationHandler.END

    context.user_data["edit_uid"] = uid
    context.user_data["edit_u"]   = u
    await update.message.reply_text(
        f"✏️ <b>Edit Syllabus</b>\n\n{_utility_summary(u)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_syllabus_edit_keyboard()
    )
    return ESY_MENU


async def _syl_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata, update_utility_message
    query  = update.callback_query
    data   = query.data
    action = data.replace("esy_", "")
    await query.answer()
    uid    = context.user_data["edit_uid"]
    u      = context.user_data["edit_u"]

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["edit_field"] = action

    if action == "message":
        has_msg = bool(u.get("message_text"))
        await query.message.edit_text(
            f"📝 <b>Edit Text Message</b>\n\n"
            f"Current: {'✅ Has message' if has_msg else '— None'}\n\n"
            f"Send new formatted message or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return ESY_MESSAGE

    if action == "file":
        await query.message.edit_text(
            "📄 <b>Replace File</b>\n\nSend new file or <code>-</code> to remove:", parse_mode=HTML
        )
        return ESY_FILE

    if action == "cover":
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\nSend new image or <code>-</code> to remove:", parse_mode=HTML
        )
        return ESY_THUMB

    # Text fields
    current = u.get(action) or "N/A"
    labels = {"title": "📌 Title", "subject": "📂 Subject",
              "course_code": "📗 Course Code", "tags": "🏷 Tags"}
    await query.message.edit_text(
        f"{labels.get(action, action)}\n\nCurrent: <code>{h(str(current))}</code>\n\n"
        f"Send new value or <code>-</code> to clear:",
        parse_mode=HTML
    )
    return ESY_VALUE


async def _syl_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata
    field = context.user_data["edit_field"]
    uid   = context.user_data["edit_uid"]
    value = update.message.text.strip()

    if field == "tags":
        tags = [t.lower() for t in value.split() if t] if value != "-" else []
        await update_utility_tags(uid, tags)
    else:
        u = await get_utility(uid)
        vals = {"title": u.get("title"), "subject": u.get("subject"), "course_code": u.get("course_code")}
        vals[field] = None if value == "-" else (value.upper() if field == "course_code" else value)
        await update_utility_metadata(uid, vals["title"], vals["subject"], vals["course_code"])

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await update.message.reply_text(
        f"✅ Updated!\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_syllabus_edit_keyboard()
    )
    return ESY_MENU


async def _syl_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_message
    msg = update.message
    uid = context.user_data["edit_uid"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_message(uid, None, None)
        label = "Text message removed."
    else:
        text     = msg.text or ""
        entities = msg.entities or []
        entity_list = []
        for e in entities:
            entry = {"type": e.type.value if hasattr(e.type, "value") else str(e.type),
                     "offset": e.offset, "length": e.length}
            if e.url:      entry["url"]      = e.url
            if e.language: entry["language"] = e.language
            entity_list.append(entry)
        await update_utility_message(uid, text, entity_list)
        label = "Text message updated!"

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_syllabus_edit_keyboard()
    )
    return ESY_MENU


async def _syl_edit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_utility_file(uid, None, None)
        label = "File removed."
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return ESY_FILE
        await update_utility_file(uid, file_id, file_type)
        label = f"File updated! ({file_type.upper()})"
    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_syllabus_edit_keyboard()
    )
    return ESY_MENU


async def _syl_edit_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_utility_thumbnail(uid, None, None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_utility_thumbnail(uid, thumb_url or None, fid)
        label = "Thumbnail updated!" if thumb_url else "Saved (imgBB failed)"
    else:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return ESY_THUMB
    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_syllabus_edit_keyboard()
    )
    return ESY_MENU


def editsyllabus_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editsyllabus", editsyllabus_start)],
        states={
            ESY_MENU: [CallbackQueryHandler(_syl_edit_callback, pattern="^esy_")],
            ESY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_edit_value),
                CallbackQueryHandler(_syl_edit_callback, pattern="^esy_"),
            ],
            ESY_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _syl_edit_message),
                CallbackQueryHandler(_syl_edit_callback, pattern="^esy_"),
            ],
            ESY_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                    _syl_edit_file
                ),
                CallbackQueryHandler(_syl_edit_callback, pattern="^esy_"),
            ],
            ESY_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    _syl_edit_thumb
                ),
                CallbackQueryHandler(_syl_edit_callback, pattern="^esy_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY (🔧) — same structure as Syllabus: file + text message + metadata
# Commands: /addutil, /editutil, /deleteutil, /listutils
# ═══════════════════════════════════════════════════════════════════════════════

AU3_TITLE, AU3_SUBJECT, AU3_COURSE, AU3_FILE, AU3_MESSAGE, AU3_THUMB, AU3_TAGS = range(7)


@admin_only
async def addutil_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/addutil &lt;uid&gt;</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    uid = args[0].strip().lower()
    if not uid.replace("-", "").isalnum():
        await update.message.reply_text("❌ UID must be alphanumeric.", parse_mode=HTML)
        return ConversationHandler.END

    if await utility_uid_exists(uid):
        await update.message.reply_text(
            f"❌ <code>{h(uid)}</code> already exists.", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["util_uid"]     = uid
    context.user_data["util_cat"]     = "util_misc"
    context.user_data["_uploader_id"] = update.effective_user.id

    await update.message.reply_text(
        f"🔧 <b>Add Utility</b>\n\n"
        f"🆔 UID: <code>{h(uid)}</code> ✅\n\n"
        f"Step 1/6 — <b>Title</b>\n\n"
        f"Example: <code>Registration Guide Spring 2026</code>\n"
        f"Send <code>-</code> to skip.\n"
        f"<i>/cancel to stop</i>",
        parse_mode=HTML
    )
    return AU3_TITLE


async def _util_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["title"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 2/6 — <b>Subject</b>\n\n"
        "Example: <code>Computer Science and Engineering</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AU3_SUBJECT


async def _util_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["subject"] = None if text == "-" else text
    await update.message.reply_text(
        "Step 3/6 — <b>Course Code</b>\n\n"
        "Example: <code>CSE311</code>\n"
        "Send <code>-</code> to skip.", parse_mode=HTML
    )
    return AU3_COURSE


async def _util_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["course_code"] = None if text == "-" else text.upper()
    await update.message.reply_text(
        "Step 4/6 — <b>File</b> (optional)\n\n"
        "Send file (PDF, image, DOCX, PPTX, Excel) or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU3_FILE


async def _util_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return AU3_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "Step 5/6 — <b>Text Message</b> (optional)\n\n"
        "Send a formatted message. Bold, links, quotes — all preserved.\n\n"
        "Send <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU3_MESSAGE


async def _util_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["message_text"]     = None
        context.user_data["message_entities"] = None
    else:
        text     = msg.text or ""
        entities = msg.entities or []
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
        context.user_data["message_text"]     = text
        context.user_data["message_entities"] = entity_list

    await msg.reply_text(
        "Step 6/6 — <b>Thumbnail</b> (optional)\n\n"
        "Send cover image for inline search preview or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return AU3_THUMB


async def _util_thumb_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fid = msg.photo[-1].file_id if msg.photo else (
        msg.document.file_id if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image") else None
    )
    if not fid:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return AU3_THUMB
    uploading = await msg.reply_text("⏳ Uploading thumbnail...")
    thumb_url = await upload_to_imgbb(context.bot, fid)
    await uploading.delete()
    context.user_data["thumbnail_url"] = thumb_url or None
    context.user_data["cover_file_id"] = fid
    await _ask_tags(msg)
    return AU3_TAGS


async def _util_thumb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "-":
        await update.message.reply_text("Send an image or <code>-</code>.", parse_mode=HTML)
        return AU3_THUMB
    context.user_data["thumbnail_url"] = None
    context.user_data["cover_file_id"] = None
    await _ask_tags(update.message)
    return AU3_TAGS


async def _util_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import insert_utility
    raw  = update.message.text.strip().lower()
    tags = [t for t in raw.split() if t] if raw != "-" else []
    ud   = context.user_data

    await insert_utility(
        ud["util_uid"], "util_misc", tags, ud.get("_uploader_id", 0),
        title=ud.get("title"), subject=ud.get("subject"), course_code=ud.get("course_code"),
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        message_text=ud.get("message_text"), message_entities=ud.get("message_entities")
    )

    u = await get_utility(ud["util_uid"])
    await update.message.reply_text(
        f"✅ <b>Utility registered!</b>\n\n{_utility_summary(u)}",
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def addutil_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addutil", addutil_start)],
        states={
            AU3_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _util_title)],
            AU3_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _util_subject)],
            AU3_COURSE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _util_course)],
            AU3_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, _util_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _util_file),
            ],
            AU3_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _util_message)],
            AU3_THUMB: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _util_thumb_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _util_thumb_skip),
            ],
            AU3_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _util_tags)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


# Edit — dedicated conversation with Text Message support (same as syllabus)
EU3_MENU, EU3_VALUE, EU3_FILE, EU3_THUMB, EU3_MESSAGE = range(5)

UTIL_EDIT_FIELDS = {
    "title":       ("📌", "Title"),
    "subject":     ("📂", "Subject"),
    "course_code": ("📗", "Course Code"),
    "tags":        ("🏷", "Tags"),
    "file":        ("📄", "File"),
    "cover":       ("🖼", "Thumbnail"),
    "message":     ("📝", "Text Message"),
}


def _util_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, (emoji, label) in UTIL_EDIT_FIELDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"eu3_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="eu3_cancel")])
    return InlineKeyboardMarkup(buttons)


@admin_only
async def editutil_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/editutil &lt;uid&gt;</code>", parse_mode=HTML)
        return ConversationHandler.END

    uid = args[0].strip().lower()
    u   = await get_utility(uid)
    if not u or u.get("category") != "util_misc":
        await update.message.reply_text(
            f"❌ No Utility found: <code>{h(uid)}</code>", parse_mode=HTML
        )
        return ConversationHandler.END

    context.user_data["edit_uid"] = uid
    context.user_data["edit_u"]   = u
    await update.message.reply_text(
        f"✏️ <b>Edit Utility</b>\n\n{_utility_summary(u)}\n\nWhat to edit?",
        parse_mode=HTML, reply_markup=_util_edit_keyboard()
    )
    return EU3_MENU


async def _util_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata, update_utility_message
    query  = update.callback_query
    data   = query.data
    action = data.replace("eu3_", "")
    await query.answer()

    uid = context.user_data["edit_uid"]
    u   = context.user_data["edit_u"]
    context.user_data["edit_field"] = action

    if action == "cancel":
        await query.message.edit_text("❌ Edit cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "message":
        has_msg = bool(u.get("message_text"))
        await query.message.edit_text(
            f"📝 <b>Edit Text Message</b>\n\n"
            f"Current: {'✅ Has message' if has_msg else '— None'}\n\n"
            f"Send new formatted message or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return EU3_MESSAGE

    if action == "file":
        await query.message.edit_text(
            "📄 <b>Replace File</b>\n\nSend new file or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return EU3_FILE

    if action == "cover":
        await query.message.edit_text(
            "🖼 <b>Edit Thumbnail</b>\n\nSend new image or <code>-</code> to remove:",
            parse_mode=HTML
        )
        return EU3_THUMB

    labels = {"title": "📌 Title", "subject": "📂 Subject",
              "course_code": "📗 Course Code", "tags": "🏷 Tags"}
    current = u.get(action) or "N/A"
    if action == "tags":
        current = _tag_str(current)
    await query.message.edit_text(
        f"{labels.get(action, action)}\n\n"
        f"Current: <code>{h(str(current))}</code>\n\n"
        f"Send new value or <code>-</code> to clear:",
        parse_mode=HTML
    )
    return EU3_VALUE


async def _util_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_metadata
    field = context.user_data["edit_field"]
    uid   = context.user_data["edit_uid"]
    value = update.message.text.strip()

    if field == "tags":
        tags = [t.lower() for t in value.split() if t] if value != "-" else []
        await update_utility_tags(uid, tags)
    else:
        u = await get_utility(uid)
        vals = {"title": u.get("title"), "subject": u.get("subject"),
                "course_code": u.get("course_code")}
        vals[field] = None if value == "-" else (value.upper() if field == "course_code" else value)
        await update_utility_metadata(uid, vals["title"], vals["subject"], vals["course_code"])

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await update.message.reply_text(
        f"✅ Updated!\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_util_edit_keyboard()
    )
    return EU3_MENU


async def _util_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import update_utility_message
    msg = update.message
    uid = context.user_data["edit_uid"]

    if msg.text and msg.text.strip() == "-":
        await update_utility_message(uid, None, None)
        label = "Text message removed."
    else:
        text     = msg.text or ""
        entities = msg.entities or []
        entity_list = []
        for e in entities:
            entry = {"type": e.type.value if hasattr(e.type, "value") else str(e.type),
                     "offset": e.offset, "length": e.length}
            if e.url:      entry["url"]      = e.url
            if e.language: entry["language"] = e.language
            entity_list.append(entry)
        await update_utility_message(uid, text, entity_list)
        label = "Text message updated!"

    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_util_edit_keyboard()
    )
    return EU3_MENU


async def _util_edit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_utility_file(uid, None, None)
        label = "File removed."
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("❌ Send a file or <code>-</code>.", parse_mode=HTML)
            return EU3_FILE
        await update_utility_file(uid, file_id, file_type)
        label = f"File updated! ({file_type.upper()})"
    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_util_edit_keyboard()
    )
    return EU3_MENU


async def _util_edit_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data["edit_uid"]
    if msg.text and msg.text.strip() == "-":
        await update_utility_thumbnail(uid, None, None)
        label = "Thumbnail removed."
    elif msg.photo or (msg.document and msg.document.mime_type
                       and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("⏳ Uploading...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        await update_utility_thumbnail(uid, thumb_url or None, fid)
        label = "Thumbnail updated!" if thumb_url else "Saved (imgBB failed)"
    else:
        await msg.reply_text("❌ Send an image or <code>-</code>.", parse_mode=HTML)
        return EU3_THUMB
    u = await get_utility(uid)
    context.user_data["edit_u"] = u
    await msg.reply_text(
        f"✅ <b>{label}</b>\n\n{_utility_summary(u)}\n\nEdit another field?",
        parse_mode=HTML, reply_markup=_util_edit_keyboard()
    )
    return EU3_MENU


def editutil_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("editutil", editutil_start)],
        states={
            EU3_MENU: [CallbackQueryHandler(_util_edit_callback, pattern="^eu3_")],
            EU3_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _util_edit_value),
                CallbackQueryHandler(_util_edit_callback, pattern="^eu3_"),
            ],
            EU3_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _util_edit_message),
                CallbackQueryHandler(_util_edit_callback, pattern="^eu3_"),
            ],
            EU3_FILE: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
                    _util_edit_file
                ),
                CallbackQueryHandler(_util_edit_callback, pattern="^eu3_"),
            ],
            EU3_THUMB: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
                    _util_edit_thumb
                ),
                CallbackQueryHandler(_util_edit_callback, pattern="^eu3_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300, per_message=False
    )


def deleteutil_conversation():
    return _make_delete_conversation("util_misc", "deleteutil")


listutils_cmd           = _make_list_cmd("util_misc")
listutils_page_callback = _make_list_page_callback("util_misc")

# ═══════════════════════════════════════════════════════════════════════════════
# NEW ADD FLOWS — Syllabus, Outline, Routine (clean, no manual UID)
# ═══════════════════════════════════════════════════════════════════════════════

import re as _re

_STOP = {"a","an","the","of","and","or","for","in","to","with","on","at","by","from","as","is","it","its"}


async def _util_generate_uid(category: str) -> str:
    """Generate UID: category + serial. e.g. syllabus01, outline02"""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT uid FROM utilities WHERE category = $1", category
        )
    max_serial = 0
    pattern = _re.compile(rf"^{_re.escape(category)}(\d+)$")
    for row in rows:
        m = pattern.match(row["uid"])
        if m:
            max_serial = max(max_serial, int(m.group(1)))
    return f"{category}{max_serial + 1:02d}"


async def _util_get_course_info(course_codes_str: str) -> list[dict]:
    """Fetch course name + abbr for given codes from current semester."""
    if not course_codes_str:
        return []
    codes = [c.strip().upper() for c in course_codes_str.split(",") if c.strip()]
    from database.queries import get_current_semester
    import json as _j
    sem = await get_current_semester()
    if not sem:
        return [{"code": c, "name": "", "abbr": ""} for c in codes]
    courses = _j.loads(sem["courses"]) if isinstance(sem["courses"], str) else (sem["courses"] or [])
    course_map = {c["code"].upper(): c for c in courses}
    return [{"code": code, "name": course_map.get(code, {}).get("name", ""),
             "abbr": course_map.get(code, {}).get("abbr", "")} for code in codes]


def _util_auto_tags(title, course_codes_str, category):
    tags = {category}
    if course_codes_str:
        for code in course_codes_str.split(","):
            tags.add(code.strip().lower())
    if title:
        for w in _re.sub(r"[^a-z0-9\s]", " ", title.lower()).split():
            if w not in _STOP and len(w) > 1:
                tags.add(w)
    return sorted(tags)


def _parse_ro_info(text: str) -> dict:
    """Parse newline format for routine/outline:
    Line 1: Title (optional — if looks like course codes, treat as course only)
    Line 2 (or 1): Course codes
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    course_pattern = _re.compile(r"^[A-Za-z]{2,4}\d{3,4}(,\s*[A-Za-z]{2,4}\d{3,4})*$")
    if len(lines) == 1 and course_pattern.match(lines[0]):
        return {"title": None, "course_codes": lines[0].upper()}
    elif len(lines) >= 2 and course_pattern.match(lines[-1]):
        return {"title": "\n".join(lines[:-1]), "course_codes": lines[-1].upper()}
    else:
        return {"title": "\n".join(lines) if lines else None, "course_codes": None}


# ── States ───────────────────────────────────────────────────────────────────
SYL2_FILE, SYL2_COURSE, SYL2_DESC, SYL2_COVER, SYL2_TAGS = range(5)
RO_FILE, RO_INFO, RO_COVER, RO_TAGS = range(4)


# ── SYLLABUS ─────────────────────────────────────────────────────────────────

@admin_only
async def addsyllabus2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    context.user_data["util_cat"]         = "syllabus"
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Syllabus</b>\n\n"
        "<i>Step 1 of 5</i>\n\n"
        "Send the syllabus file (PDF, DOCX, image), or <code>-</code> if no file.",
        parse_mode=HTML
    )
    return SYL2_FILE


async def addsyllabus2_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.text and msg.text.strip() == "-":
        context.user_data["file_id"]   = None
        context.user_data["file_type"] = None
    else:
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("Send a file or <code>-</code> to skip.", parse_mode=HTML)
            return SYL2_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

    await msg.reply_text(
        "<i>Step 2 of 5</i>\n\n"
        "Send the course code:\n"
        "<code>CSE315</code>",
        parse_mode=HTML
    )
    return SYL2_COURSE


async def addsyllabus2_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if not text:
        await update.message.reply_text("Course code is required.")
        return SYL2_COURSE

    context.user_data["course_codes"] = text
    courses = await _util_get_course_info(text)
    context.user_data["courses"] = courses
    subject = ", ".join(c["name"] for c in courses if c["name"]) or text

    context.user_data["title"] = f"{subject} Syllabus"

    await update.message.reply_text(
        f"<i>Step 3 of 5</i>\n\n"
        f"Course: {h(subject)}\n\n"
        f"Send the syllabus description/content.\n"
        f"All formatting (bold, bullets, links) will be preserved.\n\n"
        f"Send /done to skip.",
        parse_mode=HTML
    )
    return SYL2_DESC


async def addsyllabus2_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or ""
    entities = msg.entities or []
    entity_list = []
    for e in entities:
        entry = {
            "type": e.type.value if hasattr(e.type, "value") else str(e.type),
            "offset": e.offset, "length": e.length,
        }
        if e.url: entry["url"] = e.url
        if e.language: entry["language"] = e.language
        entity_list.append(entry)

    context.user_data["message_text"]     = text
    context.user_data["message_entities"] = entity_list

    await msg.reply_text(
        "<i>Step 4 of 5</i>\n\n"
        "Send cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return SYL2_COVER


async def addsyllabus2_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip description step via /done."""
    context.user_data["message_text"]     = ""
    context.user_data["message_entities"] = []
    msg = update.message
    await msg.reply_text(
        "<i>Step 4 of 5</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return SYL2_COVER


async def addsyllabus2_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text.strip() == "-":
        context.user_data["cover_file_id"] = None
        context.user_data["thumbnail_url"] = None
    elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
        fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        uploading = await msg.reply_text("Uploading cover...")
        thumb_url = await upload_to_imgbb(context.bot, fid)
        await uploading.delete()
        context.user_data["cover_file_id"] = fid
        context.user_data["thumbnail_url"] = thumb_url
    else:
        await msg.reply_text("Send a cover image or <code>-</code>.", parse_mode=HTML)
        return SYL2_COVER

    course_codes = context.user_data.get("course_codes", "")
    title        = context.user_data.get("title", "")
    auto_tags    = _util_auto_tags(title, course_codes, "syllabus")
    context.user_data["auto_tags"] = auto_tags

    await msg.reply_text(
        f"<i>Step 5 of 5</i>\n\n"
        f"<b>Auto-generated tags:</b>\n"
        f"<code>{h(' '.join(auto_tags))}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return SYL2_TAGS


async def addsyllabus2_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return SYL2_TAGS


async def addsyllabus2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import insert_utility
    ud   = context.user_data
    tags = ud.get("custom_tags") or ud.get("auto_tags", [])

    file_has  = bool(ud.get("file_id"))
    text_has  = bool(ud.get("message_text"))
    if not file_has and not text_has:
        await update.message.reply_text(
            "Syllabus must have at least a file or description. Please restart."
        )
        context.user_data.clear()
        return ConversationHandler.END

    uid = await _util_generate_uid("syllabus")
    course_codes = ud.get("course_codes")
    courses      = ud.get("courses", [])
    subject      = ", ".join(c["name"] for c in courses if c["name"]) or course_codes or ""

    from database.queries import get_current_semester
    sem    = await get_current_semester()
    sem_id = sem["id"] if sem else None

    await insert_utility(
        uid, "syllabus", tags, ud.get("_uploader_id", 0),
        title=ud.get("title"), subject=subject, course_code=course_codes,
        file_id=ud.get("file_id"), file_type=ud.get("file_type"),
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        message_text=ud.get("message_text"), message_entities=ud.get("message_entities")
    )

    sem_name = h(sem["name"]) if sem else "—"
    tag_str  = " ".join([f"#{t}" for t in tags]) or "none"
    course_display = ", ".join(
        f"{c['code']} ({c['abbr']})" if c.get("abbr") else c["code"]
        for c in courses
    ) if courses else (course_codes or "—")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Edit",   callback_data=f"esy_edit_{uid}"),
        InlineKeyboardButton("Delete", callback_data=f"esy_delete_{uid}"),
    ]])

    await update.message.reply_text(
        f"<b>Syllabus added.</b>\n\n"
        f"<b>{h(ud.get('title', ''))}</b>\n\n"
        f"<code>UID      : {h(uid)}\n"
        f"Course   : {h(course_display)}\n"
        f"Semester : {sem_name}</code>\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML,
        reply_markup=keyboard
    )
    context.user_data.clear()
    return ConversationHandler.END


def addsyllabus2_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addsyllabus", addsyllabus2_start),
            CallbackQueryHandler(addsyllabus2_start, pattern="^adm_add_syllabus$"),
        ],
        states={
            SYL2_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, addsyllabus2_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsyllabus2_file),
            ],
            SYL2_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addsyllabus2_course)],
            SYL2_DESC: [
                CommandHandler("done", addsyllabus2_skip_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsyllabus2_desc),
            ],
            SYL2_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addsyllabus2_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsyllabus2_cover),
            ],
            SYL2_TAGS: [
                CommandHandler("done", addsyllabus2_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addsyllabus2_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )


# ── ROUTINE / OUTLINE (shared factory) ───────────────────────────────────────

def _make_ro_conversation(category: str) -> ConversationHandler:
    emoji, label = CATEGORIES.get(category, ("📄", category.capitalize()))

    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        context.user_data["_in_conversation"] = True
        context.user_data["_uploader_id"]     = update.effective_user.id
        context.user_data["util_cat"]         = category
        msg = update.effective_message
        await msg.reply_text(
            f"<b>Add {label}</b>\n\n"
            f"<i>Step 1 of 4</i>\n\n"
            f"Send the {label.lower()} file.",
            parse_mode=HTML
        )
        return RO_FILE

    async def _file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("Please send a PDF, image, or document file.")
            return RO_FILE
        context.user_data["file_id"]   = file_id
        context.user_data["file_type"] = file_type

        await msg.reply_text(
            "<i>Step 2 of 4</i>\n\n"
            "Send course code(s), and optionally a title on the first line:\n\n"
            "Course code only:\n"
            "<code>CSE315, CSE317</code>\n\n"
            "With title:\n"
            f"<code>Spring 2026 {label}\n"
            "CSE315, CSE317</code>\n\n"
            "Send <code>-</code> if no course.",
            parse_mode=HTML
        )
        return RO_INFO

    async def _info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "-":
            info = {"title": None, "course_codes": None}
        else:
            info = _parse_ro_info(text)

        courses = await _util_get_course_info(info.get("course_codes") or "")
        subject = ", ".join(c["name"] for c in courses if c["name"]) or (info.get("course_codes") or "")

        title = info.get("title") or (f"{subject} {label}" if subject else label)
        uid   = await _util_generate_uid(category)
        auto_tags = _util_auto_tags(title, info.get("course_codes") or "", category)

        context.user_data.update({
            "uid":          uid,
            "title":        title,
            "course_codes": info.get("course_codes"),
            "courses":      courses,
            "subject":      subject,
            "auto_tags":    auto_tags,
        })

        course_line = ", ".join(
            f"{c['code']} — {c['name']} ({c['abbr']})" for c in courses
        ) if courses else (info.get("course_codes") or "None")

        await update.message.reply_text(
            f"<i>Step 3 of 4</i>\n\n"
            f"<b>Preview:</b>\n"
            f"UID    : <code>{uid}</code>\n"
            f"Title  : {h(title)}\n"
            f"Course : {h(course_line)}\n\n"
            f"Send cover image, or <code>-</code> to skip.",
            parse_mode=HTML
        )
        return RO_COVER

    async def _cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg.text and msg.text.strip() == "-":
            context.user_data["cover_file_id"] = None
            context.user_data["thumbnail_url"] = None
        elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("Uploading cover...")
            thumb_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            context.user_data["cover_file_id"] = fid
            context.user_data["thumbnail_url"] = thumb_url
        else:
            await msg.reply_text("Send a cover image or <code>-</code>.", parse_mode=HTML)
            return RO_COVER

        auto_tags = context.user_data.get("auto_tags", [])
        await msg.reply_text(
            f"<i>Step 4 of 4</i>\n\n"
            f"<b>Auto-generated tags:</b>\n"
            f"<code>{h(' '.join(auto_tags))}</code>\n\n"
            f"Send your own tags to replace, then /done.\n"
            f"Or /done now to confirm as-is.",
            parse_mode=HTML
        )
        return RO_TAGS

    async def _tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw = update.message.text.strip().lower()
        if raw:
            context.user_data["custom_tags"] = [t for t in raw.split() if t]
            await update.message.reply_text("Tags updated. Send /done to confirm.")
        return RO_TAGS

    async def _tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from database.utility_queries import insert_utility
        ud   = context.user_data
        tags = ud.get("custom_tags") or ud.get("auto_tags", [])
        uid  = ud["uid"]

        from database.queries import get_current_semester
        sem    = await get_current_semester()

        await insert_utility(
            uid, category, tags, ud.get("_uploader_id", 0),
            title=ud.get("title"), subject=ud.get("subject"),
            course_code=ud.get("course_codes"),
            file_id=ud.get("file_id"), file_type=ud.get("file_type"),
            thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        )

        sem_name = h(sem["name"]) if sem else "—"
        tag_str  = " ".join([f"#{t}" for t in tags]) or "none"
        courses  = ud.get("courses", [])
        course_display = ", ".join(
            f"{c['code']} ({c['abbr']})" if c.get("abbr") else c["code"]
            for c in courses
        ) if courses else (ud.get("course_codes") or "—")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Edit",   callback_data=f"eu2_edit_{uid}"),
            InlineKeyboardButton("Delete", callback_data=f"eu2_delete_{uid}"),
        ]])

        await update.message.reply_text(
            f"<b>{label} added.</b>\n\n"
            f"<b>{h(ud.get('title', ''))}</b>\n\n"
            f"<code>UID      : {h(uid)}\n"
            f"Course   : {h(course_display)}\n"
            f"Semester : {sem_name}</code>\n\n"
            f"Tags: {h(tag_str)}",
            parse_mode=HTML,
            reply_markup=keyboard
        )
        context.user_data.clear()
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler(f"add{category}", _start),
            CallbackQueryHandler(_start, pattern=f"^adm_add_{category}$"),
        ],
        states={
            RO_FILE:  [MessageHandler(filters.Document.ALL | filters.PHOTO, _file)],
            RO_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _info)],
            RO_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cover),
            ],
            RO_TAGS: [
                CommandHandler("done", _tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )


def addoutline_conversation() -> ConversationHandler:
    return _make_ro_conversation("outline")


def addroutine_conversation() -> ConversationHandler:
    return _make_ro_conversation("routine")

# ── NEW ADD FLOW — Cal, Advisor, RegPay (multi-file, auto UID) ────────────────
CAL_FILES, CAL_INFO, CAL_COVER, CAL_TAGS = range(4)


def _make_cal_conversation(category: str) -> ConversationHandler:
    emoji, label   = CATEGORIES.get(category, ("📄", category.capitalize()))
    is_advisor     = category == "advisor"
    info_prompt    = (
        f"Send advisor name and optionally a link (on a new line):\n\n"
        f"<code>Dr. John Smith\nhttps://profile.link</code>\n\n"
        f"Link is optional — send just the name if no link."
    ) if is_advisor else (
        f"Send a title, or <code>-</code> to skip."
    )

    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        context.user_data["_in_conversation"] = True
        context.user_data["_uploader_id"]     = update.effective_user.id
        context.user_data["util_cat"]         = category
        context.user_data["_files"]           = []
        msg = update.effective_message
        await msg.reply_text(
            f"<b>Add {label}</b>\n\n"
            f"<i>Step 1</i>\n\n"
            f"Send file(s) one by one (PDF, image, DOCX, Excel).\n"
            f"Send /done when finished, or <code>-</code> if no file.",
            parse_mode=HTML
        )
        return CAL_FILES

    async def _files(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.text and msg.text.strip() == "-":
            # No files
            await msg.reply_text(
                f"<i>Step 2</i>\n\n{info_prompt}",
                parse_mode=HTML
            )
            return CAL_INFO

        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text("Send a file, /done to finish, or <code>-</code> if no file.", parse_mode=HTML)
            return CAL_FILES

        files = context.user_data.get("_files", [])
        files.append({"file_id": file_id, "file_type": file_type})
        context.user_data["_files"] = files
        await msg.reply_text(f"File {len(files)} saved. Send more or /done to finish.")
        return CAL_FILES

    async def _files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"<i>Step 2</i>\n\n{info_prompt}",
            parse_mode=HTML
        )
        return CAL_INFO

    async def _info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if is_advisor:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # Detect URLs vs plain text
            url_lines   = [l for l in lines if l.startswith("http://") or l.startswith("https://")]
            name_lines  = [l for l in lines if not (l.startswith("http://") or l.startswith("https://"))]
            title = " ".join(name_lines) if name_lines else None
            url   = url_lines[0] if url_lines else None
            context.user_data["title"]     = title
            context.user_data["url"]       = url
            context.user_data["url_title"] = f"View {title}'s Profile" if (url and title) else ("Open Profile" if url else None)
        else:
            context.user_data["title"] = None if text == "-" else text
            context.user_data["url"]   = None

        _default_tags = {
            "cal":     ["academic", "calendar"],
            "advisor": ["advisor"],
            "regpay":  ["registration", "payment", "fee"],
        }
        auto_tags = _default_tags.get(category, [category])
        context.user_data["auto_tags"] = auto_tags

        await update.message.reply_text(
            "<i>Step 3</i>\n\nSend cover image, or <code>-</code> to skip.",
            parse_mode=HTML
        )
        return CAL_COVER

    async def _cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg.text and msg.text.strip() == "-":
            context.user_data["cover_file_id"] = None
            context.user_data["thumbnail_url"] = None
        elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("Uploading cover...")
            thumb_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            context.user_data["cover_file_id"] = fid
            context.user_data["thumbnail_url"] = thumb_url
        else:
            await msg.reply_text("Send a cover image or <code>-</code>.", parse_mode=HTML)
            return CAL_COVER

        auto_tags = context.user_data.get("auto_tags", [])
        await msg.reply_text(
            f"<i>Step 4</i>\n\n"
            f"<b>Auto-generated tags:</b>\n"
            f"<code>{h(' '.join(auto_tags))}</code>\n\n"
            f"Send your own tags to replace, then /done.\n"
            f"Or /done now to confirm as-is.",
            parse_mode=HTML
        )
        return CAL_TAGS

    async def _tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw = update.message.text.strip().lower()
        if raw:
            context.user_data["custom_tags"] = [t for t in raw.split() if t]
            await update.message.reply_text("Tags updated. Send /done to confirm.")
        return CAL_TAGS

    async def _tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from database.utility_queries import insert_utility
        ud    = context.user_data
        tags  = ud.get("custom_tags") or ud.get("auto_tags", [])
        uid   = await _util_generate_uid(category)
        files = ud.get("_files", [])

        # first file for backward compat
        first_fid  = files[0]["file_id"]   if files else None
        first_ftype = files[0]["file_type"] if files else None

        from database.queries import get_current_semester
        sem = await get_current_semester()

        await insert_utility(
            uid, category, tags, ud.get("_uploader_id", 0),
            title=ud.get("title"),
            file_id=first_fid, file_type=first_ftype,
            file_ids=files,
            thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
            url=ud.get("url"), url_title=ud.get("url_title"),
        )

        sem_name = h(sem["name"]) if sem else "—"
        tag_str  = " ".join([f"#{t}" for t in tags]) or "none"
        title    = ud.get("title") or label
        url_line = f"\nLink: {h(ud['url'])}" if ud.get("url") else ""

        await update.message.reply_text(
            f"<b>{label} added.</b>\n\n"
            f"<b>{h(title)}</b>{url_line}\n\n"
            f"<code>UID      : {h(uid)}\n"
            f"Files    : {len(files)}\n"
            f"Semester : {sem_name}</code>\n\n"
            f"Tags: {h(tag_str)}",
            parse_mode=HTML,
        )
        context.user_data.clear()
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler(f"add{category}", _start),
            CallbackQueryHandler(_start, pattern=f"^adm_add_{category}$"),
        ],
        states={
            CAL_FILES: [
                CommandHandler("done", _files_done),
                MessageHandler(filters.Document.ALL | filters.PHOTO, _files),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _files),
            ],
            CAL_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _info)],
            CAL_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cover),
            ],
            CAL_TAGS: [
                CommandHandler("done", _tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )


def addcal2_conversation():    return _make_cal_conversation("cal")
def addregpay2_conversation(): return _make_cal_conversation("regpay")


# ═══════════════════════════════════════════════════════════════════════════════
# ADVISOR ADD FLOW — extends _make_cal_conversation with a CSV step
# Steps: files → info → cover → tags → CSV
# ═══════════════════════════════════════════════════════════════════════════════

ADV_FILES, ADV_INFO, ADV_COVER, ADV_TAGS, ADV_CSV = range(5)


def addadvisor2_conversation() -> ConversationHandler:
    category = "advisor"
    emoji, label = CATEGORIES["advisor"]

    @admin_only
    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        context.user_data["_in_conversation"] = True
        context.user_data["_uploader_id"]     = update.effective_user.id
        context.user_data["util_cat"]         = category
        context.user_data["_files"]           = []
        msg = update.effective_message
        await msg.reply_text(
            f"<b>Add {label}</b>\n\n"
            f"<i>Step 1 of 5</i>\n\n"
            f"Send the advisor list file(s) (PDF, image, Excel) one by one.\n"
            f"Send /done when finished, or <code>-</code> if no file.",
            parse_mode=HTML
        )
        return ADV_FILES

    async def _files(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.text and msg.text.strip() == "-":
            await msg.reply_text(
                "<i>Step 2 of 5</i>\n\n"
                "Send advisor name and optionally a link on the next line:\n\n"
                "<code>Advisor Info Spring 2026\nhttps://profile.link</code>\n\n"
                "Or send <code>-</code> to skip title.",
                parse_mode=HTML
            )
            return ADV_INFO

        file_id, file_type = _detect_file_type(msg)
        if not file_id:
            await msg.reply_text(
                "Send a file, /done to finish, or <code>-</code> if no file.",
                parse_mode=HTML
            )
            return ADV_FILES

        files = context.user_data.get("_files", [])
        files.append({"file_id": file_id, "file_type": file_type})
        context.user_data["_files"] = files
        await msg.reply_text(f"File {len(files)} saved. Send more or /done to finish.")
        return ADV_FILES

    async def _files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "<i>Step 2 of 5</i>\n\n"
            "Send a title and optionally a link (on a new line):\n\n"
            "<code>Advisor Info Spring 2026\nhttps://diu.edu.bd/advisor</code>\n\n"
            "Send <code>-</code> to skip.",
            parse_mode=HTML
        )
        return ADV_INFO

    async def _info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text  = update.message.text.strip()
        if text == "-":
            context.user_data["title"]     = None
            context.user_data["url"]       = None
            context.user_data["url_title"] = None
        else:
            lines     = [l.strip() for l in text.splitlines() if l.strip()]
            url_lines = [l for l in lines if l.startswith("http://") or l.startswith("https://")]
            name_lines= [l for l in lines if not (l.startswith("http://") or l.startswith("https://"))]
            title     = " ".join(name_lines) if name_lines else None
            url       = url_lines[0] if url_lines else None
            context.user_data["title"]     = title
            context.user_data["url"]       = url
            context.user_data["url_title"] = "Open Link" if url else None

        await update.message.reply_text(
            "<i>Step 3 of 5</i>\n\nSend cover image, or <code>-</code> to skip.",
            parse_mode=HTML
        )
        return ADV_COVER

    async def _cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg.text and msg.text.strip() == "-":
            context.user_data["cover_file_id"] = None
            context.user_data["thumbnail_url"] = None
        elif msg.photo or (msg.document and msg.document.mime_type
                           and msg.document.mime_type.startswith("image")):
            fid = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            uploading = await msg.reply_text("Uploading cover...")
            thumb_url = await upload_to_imgbb(context.bot, fid)
            await uploading.delete()
            context.user_data["cover_file_id"] = fid
            context.user_data["thumbnail_url"] = thumb_url
        else:
            await msg.reply_text("Send a cover image or <code>-</code>.", parse_mode=HTML)
            return ADV_COVER

        await msg.reply_text(
            "<i>Step 4 of 5</i>\n\n"
            "Auto tags: <code>advisor</code>\n\n"
            "Send your own tags to replace, then /done.\n"
            "Or /done now to confirm as-is.",
            parse_mode=HTML
        )
        return ADV_TAGS

    async def _tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw = update.message.text.strip().lower()
        if raw:
            context.user_data["custom_tags"] = [t for t in raw.split() if t]
            await update.message.reply_text("Tags updated. Send /done to confirm.")
        return ADV_TAGS

    async def _tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Save utility entry, then ask for CSV."""
        from database.utility_queries import insert_utility
        from database.queries import get_current_semester

        ud    = context.user_data
        tags  = ud.get("custom_tags") or ["advisor"]
        uid   = await _util_generate_uid(category)
        files = ud.get("_files", [])

        first_fid   = files[0]["file_id"]   if files else None
        first_ftype = files[0]["file_type"] if files else None

        sem    = await get_current_semester()
        sem_id = sem["id"] if sem else None

        await insert_utility(
            uid, category, tags, ud.get("_uploader_id", 0),
            title=ud.get("title"),
            file_id=first_fid, file_type=first_ftype,
            file_ids=files,
            thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
            url=ud.get("url"), url_title=ud.get("url_title"),
        )

        context.user_data["_advisor_uid"]    = uid
        context.user_data["_advisor_sem_id"] = sem_id

        await update.message.reply_text(
            f"File saved. UID: <code>{h(uid)}</code>\n\n"
            f"<i>Step 5 of 5</i>\n\n"
            f"<b>Upload advisor assignments CSV</b>\n\n"
            f"Required columns (header row):\n"
            f"<code>advisor_name, designation, id_from, id_to, room, schedule, email, phone</code>\n\n"
            f"Example rows:\n"
            f"<code>Tanvirul Islam, Lecturer, 241-15-034, 241-15-044, KT-317, "
            f"9.00am-1.00pm 2.00pm-3.45pm, tanvirulislam.cse@diu.edu.bd, 01571321093</code>\n\n"
            f"Send the CSV file, or /skip to finish without assignments.",
            parse_mode=HTML
        )
        return ADV_CSV

    async def _csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Parse and store advisor CSV."""
        from database.advisor_queries import parse_advisor_csv, insert_advisor_assignments

        msg    = update.message
        sem_id = context.user_data.get("_advisor_sem_id")
        uploader = context.user_data.get("_uploader_id", 0)

        if not (msg.document and msg.document.file_name
                and msg.document.file_name.lower().endswith(".csv")):
            await msg.reply_text(
                "Please send a .csv file, or /skip to finish without assignments.",
                parse_mode=HTML
            )
            return ADV_CSV

        tg_file  = await context.bot.get_file(msg.document.file_id)
        content  = bytes(await tg_file.download_as_bytearray())
        rows, errors = parse_advisor_csv(content)

        if errors and not rows:
            err_text = "\n".join(errors[:5])
            await msg.reply_text(
                f"CSV parsing failed:\n<code>{h(err_text)}</code>\n\n"
                f"Fix and resend, or /skip.",
                parse_mode=HTML
            )
            return ADV_CSV

        await insert_advisor_assignments(rows, sem_id, uploader)

        summary = f"Advisor assignments saved: {len(rows)} row(s)."
        if errors:
            err_preview = "\n".join(errors[:3])
            summary += f"\n\nWarnings:\n<code>{h(err_preview)}</code>"
            if len(errors) > 3:
                summary += f"\n... and {len(errors) - 3} more."

        await msg.reply_text(
            f"Done.\n\n{summary}",
            parse_mode=HTML
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def _csv_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Finished. No assignments uploaded.")
        context.user_data.clear()
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler("addadvisor", _start),
            CallbackQueryHandler(_start, pattern="^adm_add_advisor$"),
        ],
        states={
            ADV_FILES: [
                CommandHandler("done", _files_done),
                MessageHandler(filters.Document.ALL | filters.PHOTO, _files),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _files),
            ],
            ADV_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _info)],
            ADV_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, _cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cover),
            ],
            ADV_TAGS: [
                CommandHandler("done", _tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _tags_input),
            ],
            ADV_CSV: [
                CommandHandler("skip", _csv_skip),
                MessageHandler(filters.Document.ALL, _csv),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=600,  # longer timeout for CSV upload
        per_message=False,
        allow_reentry=True,
    )

# ═══════════════════════════════════════════════════════════════════════════════
# NEW ADD FLOW — Utility (util_misc), auto UID, multi-file, info parsing
# ═══════════════════════════════════════════════════════════════════════════════

UTIL2_FILES, UTIL2_INFO, UTIL2_DESC, UTIL2_COVER, UTIL2_TAGS = range(5)

_UTIL_TYPES = {"mid", "final", "quiz", "assignment", "lab", "ct", "presentation", "project"}
_COURSE_PAT = _re.compile(r"^[A-Za-z]{2,4}\d{3,4}$")


def _parse_util_info(text: str) -> dict:
    """Auto-detect from lines:
    - Course code pattern → course_code
    - Known type keyword → type_tag
    - URL → url; next non-url, non-number line → url_title
    - Everything else (first occurrence) → title
    """
    lines      = [l.strip() for l in text.strip().splitlines() if l.strip()]
    title      = None
    course     = None
    type_tag   = None
    url        = None
    url_title  = None
    url_idx    = None

    for i, line in enumerate(lines):
        if line.startswith("http://") or line.startswith("https://"):
            url     = line
            url_idx = i
            continue
        if url_idx is not None and i == url_idx + 1:
            # Line immediately after URL → button text
            if not (line.startswith("http://") or line.startswith("https://")):
                url_title = line
                continue
        if _COURSE_PAT.match(line):
            course = line.upper()
            continue
        if line.lower() in _UTIL_TYPES:
            type_tag = line.lower()
            continue
        if title is None:
            title = line

    return {
        "title":      title,
        "course":     course,
        "type_tag":   type_tag,
        "url":        url,
        "url_title":  url_title,
    }


async def _util2_generate_uid() -> str:
    pool = await get_pool_util()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid FROM utilities WHERE category = $1", "util_misc")
    max_n = 0
    for row in rows:
        m = _re.match(r"^util(\d+)$", row["uid"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"util{max_n + 1:02d}"


async def get_pool_util():
    from database.db import get_pool
    return await get_pool()


@admin_only
async def addutil2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    context.user_data["_files"]           = []
    msg = update.effective_message
    await msg.reply_text(
        "<b>Add Utility</b>\n\n"
        "<i>Step 1</i>\n\n"
        "Send file(s) one by one (any format).\n"
        "Send /done when finished, or <code>-</code> if no file.",
        parse_mode=HTML
    )
    return UTIL2_FILES


async def addutil2_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.text and msg.text.strip() == "-":
        await msg.reply_text(
            "<i>Step 2</i>\n\n"
            "Send info (each on a new line), or <code>-</code> to skip:\n\n"
            "<code>Registration Guide\n"
            "CSE315\n"
            "mid\n"
            "https://link.com\n"
            "View Link</code>",
            parse_mode=HTML
        )
        return UTIL2_INFO

    file_id, file_type = _detect_file_type(msg)
    if not file_id:
        await msg.reply_text("Send a file, /done to finish, or <code>-</code> if no file.", parse_mode=HTML)
        return UTIL2_FILES

    files = context.user_data.get("_files", [])
    files.append({"file_id": file_id, "file_type": file_type})
    context.user_data["_files"] = files
    await msg.reply_text(f"File {len(files)} saved. Send more or /done to finish.")
    return UTIL2_FILES


async def addutil2_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<i>Step 2</i>\n\n"
        "Send info (each on a new line), or <code>-</code> to skip:\n\n"
        "<code>Registration Guide\n"
        "CSE315\n"
        "mid\n"
        "https://link.com\n"
        "View Link</code>",
        parse_mode=HTML
    )
    return UTIL2_INFO


async def addutil2_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        info = {"title": None, "course": None, "type_tag": None, "url": None, "url_title": None}
    else:
        info = _parse_util_info(text)

    context.user_data.update(info)

    # Build auto tags
    tags = {"utility"}
    if info.get("course"):
        courses = await _util_get_course_info(info["course"])
        tags.add(info["course"].lower())
        for co in courses:
            if co.get("name"):
                for w in _re.sub(r"[^a-z0-9\s]", " ", co["name"].lower()).split():
                    if w not in _STOP and len(w) > 1:
                        tags.add(w)
            if co.get("abbr"):
                tags.add(co["abbr"].lower())
        context.user_data["_courses"] = courses
    if info.get("type_tag"):
        tags.add(info["type_tag"])
    if info.get("title"):
        for w in _re.sub(r"[^a-z0-9\s]", " ", info["title"].lower()).split():
            if w not in _STOP and len(w) > 1:
                tags.add(w)
    context.user_data["auto_tags"] = sorted(tags)

    await update.message.reply_text(
        "<i>Step 3</i>\n\n"
        "Send description/content (formatting preserved).\n"
        "Send /done to skip.",
        parse_mode=HTML
    )
    return UTIL2_DESC


async def addutil2_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text     = msg.text or ""
    entities = msg.entities or []
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

    context.user_data["message_text"]     = text
    context.user_data["message_entities"] = entity_list

    await msg.reply_text(
        "<i>Step 4</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return UTIL2_COVER


async def addutil2_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        return UTIL2_COVER

    auto_tags = context.user_data.get("auto_tags", ["utility"])
    await msg.reply_text(
        f"<i>Step 5</i>\n\n"
        f"<b>Auto tags:</b> <code>{h(' '.join(auto_tags))}</code>\n\n"
        f"Send your own tags to replace, then /done.\n"
        f"Or /done now to confirm as-is.",
        parse_mode=HTML
    )
    return UTIL2_TAGS


async def addutil2_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw:
        context.user_data["custom_tags"] = [t for t in raw.split() if t]
        await update.message.reply_text("Tags updated. Send /done to confirm.")
    return UTIL2_TAGS


async def addutil2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.utility_queries import insert_utility
    ud    = context.user_data
    tags  = ud.get("custom_tags") or ud.get("auto_tags", ["utility"])
    uid   = await _util2_generate_uid()
    files = ud.get("_files", [])

    first_fid   = files[0]["file_id"]   if files else None
    first_ftype = files[0]["file_type"] if files else None

    # Use cached courses from info step
    courses = ud.get("_courses") or await _util_get_course_info(ud.get("course") or "")
    subject = courses[0]["name"] if courses else None

    await insert_utility(
        uid, "util_misc", tags, ud.get("_uploader_id", 0),
        title=ud.get("title"),
        subject=subject,
        course_code=ud.get("course"),
        file_id=first_fid, file_type=first_ftype,
        file_ids=files,
        thumbnail_url=ud.get("thumbnail_url"), cover_file_id=ud.get("cover_file_id"),
        url=ud.get("url"), url_title=ud.get("url_title"),
        message_text=ud.get("message_text"), message_entities=ud.get("message_entities"),
    )

    tag_str    = " ".join([f"#{t}" for t in tags])
    title_line = h(ud.get("title") or "Utility")
    type_line  = f"\nType   : {h(ud['type_tag'])}" if ud.get("type_tag") else ""
    course_line= f"\nCourse : {h(ud['course'])}" if ud.get("course") else ""
    url_line   = f"\nLink   : {h(ud['url'])}" if ud.get("url") else ""

    await update.message.reply_text(
        f"<b>Utility added.</b>\n\n"
        f"<b>{title_line}</b>\n\n"
        f"<code>UID    : {h(uid)}"
        f"{course_line}{type_line}</code>"
        f"{url_line}\n\n"
        f"Tags: {h(tag_str)}",
        parse_mode=HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


def addutil2_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addutil", addutil2_start),
            CallbackQueryHandler(addutil2_start, pattern="^adm_add_util$"),
        ],
        states={
            UTIL2_FILES: [
                CommandHandler("done", addutil2_files_done),
                MessageHandler(filters.Document.ALL | filters.PHOTO, addutil2_files),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addutil2_files),
            ],
            UTIL2_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addutil2_info)],
            UTIL2_DESC:  [
                CommandHandler("done", _util2_skip_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addutil2_desc),
            ],
            UTIL2_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addutil2_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addutil2_cover),
            ],
            UTIL2_TAGS: [
                CommandHandler("done", addutil2_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addutil2_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )


async def _util2_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip description step via /done."""
    context.user_data["message_text"]     = None
    context.user_data["message_entities"] = None
    await update.message.reply_text(
        "<i>Step 4</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return UTIL2_COVER

# ═══════════════════════════════════════════════════════════════════════════════
# SLIDES — Duplicate of util_misc with category="slides"
# ═══════════════════════════════════════════════════════════════════════════════

async def addslide2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    context.user_data["_uploader_id"]     = update.effective_user.id
    context.user_data["util_cat"]         = "slides"
    msg = update.effective_message
    await msg.reply_text(
        "<i>Add Slide — Step 1</i>\n\n"
        "Send slide files (PDF, image, etc.) one by one.\n"
        "Send /done when finished.",
        parse_mode=HTML
    )
    return UTIL2_FILES


async def addslide2_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_files(update, context)


async def addslide2_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_files_done(update, context)


async def _generate_slide_uid() -> str:
    from database.db import get_pool as _get_pool
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid FROM utilities WHERE category = $1", "slides")
    import re as _re
    max_n = 0
    for row in rows:
        m = _re.match(r"^sl(\d+)$", row["uid"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"sl{max_n + 1:02d}"


async def addslide2_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    # Reuse addutil2_info logic but set category to slides
    return await addutil2_info(update, context)


async def addslide2_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_desc(update, context)


async def _slide2_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["message_text"]     = None
    context.user_data["message_entities"] = None
    await update.message.reply_text(
        "<i>Step 4</i>\n\nSend cover image, or <code>-</code> to skip.",
        parse_mode=HTML
    )
    return UTIL2_COVER


async def addslide2_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_cover(update, context)


async def addslide2_tags_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_tags_input(update, context)


async def addslide2_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["util_cat"] = "slides"
    return await addutil2_tags(update, context)


def addslide2_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addslide", addslide2_start),
            CallbackQueryHandler(addslide2_start, pattern="^adm_add_slide$"),
        ],
        states={
            UTIL2_FILES: [
                CommandHandler("done", addslide2_files_done),
                MessageHandler(filters.Document.ALL | filters.PHOTO, addslide2_files),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addslide2_files),
            ],
            UTIL2_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addslide2_info)],
            UTIL2_DESC:  [
                CommandHandler("done", _slide2_skip_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addslide2_desc),
            ],
            UTIL2_COVER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addslide2_cover),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addslide2_cover),
            ],
            UTIL2_TAGS: [
                CommandHandler("done", addslide2_tags),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addslide2_tags_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: (
            u.message.reply_text("Cancelled.") or ConversationHandler.END
        ))],
        conversation_timeout=300,
        per_message=False,
        allow_reentry=True
    )


# Edit/delete/list slides reuse utility infrastructure
editslide_conversation   = lambda: _make_edit_extended_conversation("slides", "editslide")
deleteslide_conversation = lambda: _make_delete_conversation("slides", "deleteslide")
listslides_cmd           = _make_list_cmd("slides")
listslides_page_callback = _make_list_page_callback("slides")