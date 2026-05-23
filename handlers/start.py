"""
Start handler — /start command, persistent Reply KB navigation, group redirect.

Reply KB layout (always visible in DM):
  [Resources]  [Academics]
  [Upload]     [Report]
  [Subscriptions] [Waiver]
  [Admin]  <- admin only
"Resources"   -> inline KB message (switch_inline_query, 2 per row)
"Academics"   -> Reply KB replaces with academics sub-menu
"Waiver" -> Reply KB replaces with Waiverulator KB
"Admin"       -> Reply KB replaces with admin sub-menu

No emoji anywhere — text and ASCII only.
Keyboard toggle handled by Telegram natively (is_persistent=False).
"""

import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config.settings import settings
from middleware.membership import should_respond
from database import queries

logger = logging.getLogger(__name__)


# ── Button label constants ─────────────────────────────────────────────────────

BTN_RESOURCES = "Resources"
BTN_ACADEMICS = "Academics"
BTN_UPLOAD    = "Upload"
BTN_REPORT    = "Report"
BTN_SUBS      = "Subscriptions"
BTN_WAIVER    = "Waiver"
BTN_ASK_AI    = "✦ Ask AI"
BTN_ADMIN     = "Admin"


BTN_CALENDAR  = "Academic Calendar"
BTN_ADVISOR   = "Advisor Info"
BTN_ROUTINES  = "Routines"
BTN_REGPAY    = "Reg & Payment"
BTN_MAIN_MENU = "[::] Menu"
BTN_BACK_NAV  = "« Back"

BTN_ADM_ADD       = "Add"
BTN_ADM_EDIT      = "Edit"
BTN_ADM_DELETE    = "Delete"
BTN_ADM_LIST      = "List"
BTN_ADM_BROADCAST = "Broadcast"
BTN_ADM_OVERVIEW  = "Overview"
BTN_ADM_SEMESTER  = "Semester"
BTN_ADM_EXAM      = "Exam"
BTN_EXAM          = "Exams"

BTN_ADD_BOOK     = "Book"
BTN_ADD_NOTE     = "Note"
BTN_ADD_PSQ      = "PSQ"
BTN_ADD_SOLVE    = "Solve"
BTN_ADD_VIDOC    = "Vidoc"
BTN_ADD_UTIL     = "Utility"
BTN_ADD_WAIVER   = "Waiver"
BTN_ADD_REGPAY   = "RegPay"
BTN_ADD_SYLLABUS = "Syllabus"
BTN_ADD_OUTLINE  = "Outline"
BTN_ADD_ROUTINE  = "Routine"
BTN_ADD_CAL      = "Calendar"
BTN_ADD_ADVISOR  = "Advisor"
BTN_ADD_FEE      = "Fee"
BTN_BACK         = "< Back"

# Edit sub-menu
BTN_EDIT_BOOK     = "Edit Book"
BTN_EDIT_NOTE     = "Edit Note"
BTN_EDIT_PSQ      = "Edit PSQ"
BTN_EDIT_SOLVE    = "Edit Solve"
BTN_EDIT_VIDOC    = "Edit Vidoc"
BTN_EDIT_UTIL     = "Edit Utility"
BTN_EDIT_WAIVER   = "Edit Waiver"
BTN_EDIT_REGPAY   = "Edit RegPay"

# Delete sub-menu
BTN_DEL_BOOK      = "Del Book"
BTN_DEL_NOTE      = "Del Note"
BTN_DEL_PSQ       = "Del PSQ"
BTN_DEL_SOLVE     = "Del Solve"
BTN_DEL_VIDOC     = "Del Vidoc"
BTN_DEL_UTIL      = "Del Utility"
BTN_DEL_WAIVER    = "Del Waiver"
BTN_DEL_REGPAY    = "Del RegPay"

# List sub-menu
BTN_LST_BOOK      = "List Books"
BTN_LST_NOTE      = "List Notes"
BTN_LST_PSQ       = "List PSQs"
BTN_LST_SOLVE     = "List Solves"
BTN_LST_VIDOC     = "List Vidocs"
BTN_LST_UTIL      = "List Utils"
BTN_LST_WAIVER    = "List Waivers"
BTN_LST_REGPAY    = "List RegPays"
BTN_LST_BOOK_SOL  = "List Solutions"
BTN_LST_CAL       = "List Calendars"
BTN_LST_ADVISOR   = "List Advisors"
BTN_LST_FEE       = "List Fees"
BTN_LST_SYLLABUS  = "List Syllabuses"
BTN_LST_OUTLINE   = "List Outlines"
BTN_LST_ROUTINE   = "List Routines"

# Semester sub-menu
BTN_SEM_LIST      = "Sem List"
BTN_SEM_CREATE    = "Sem Create"
BTN_SEM_RESTORE   = "Sem Restore"
BTN_SEM_RENAME    = "Sem Rename"
BTN_SEM_DELETE    = "Sem Delete"
BTN_SEM_CURRENT   = "Sem Current"


# Full set — used in dm_text_handler to detect reply button presses
ALL_REPLY_BUTTONS = {
    BTN_RESOURCES, BTN_ACADEMICS, BTN_UPLOAD, BTN_REPORT,
    BTN_SUBS, BTN_WAIVER, BTN_ASK_AI, BTN_ADMIN,
    BTN_CALENDAR, BTN_ADVISOR, BTN_ROUTINES, BTN_REGPAY, BTN_MAIN_MENU, BTN_BACK_NAV,
    BTN_ADM_ADD, BTN_ADM_EDIT, BTN_ADM_DELETE,
    BTN_ADM_BROADCAST, BTN_ADM_OVERVIEW, BTN_ADM_SEMESTER, BTN_ADM_EXAM,
    BTN_EXAM,
    BTN_ADD_BOOK, BTN_ADD_NOTE, BTN_ADD_PSQ, BTN_ADD_SOLVE,
    BTN_ADD_VIDOC, BTN_ADD_UTIL, BTN_ADD_WAIVER, BTN_ADD_REGPAY,
    BTN_ADD_SYLLABUS, BTN_ADD_OUTLINE, BTN_ADD_ROUTINE,
    BTN_ADD_CAL, BTN_ADD_ADVISOR, BTN_ADD_FEE,
    # Waiver percentage buttons
    "10%", "15%", "20%", "25%", "30%", "50%", "Custom %",

}


# ── Reply Keyboards ────────────────────────────────────────────────────────────

def main_reply_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_RESOURCES), KeyboardButton(BTN_ACADEMICS), KeyboardButton(BTN_EXAM)],
        [KeyboardButton(BTN_UPLOAD),    KeyboardButton(BTN_REPORT)],
        [KeyboardButton(BTN_SUBS),      KeyboardButton(BTN_WAIVER)],
        [KeyboardButton(BTN_ASK_AI)],
    ]
    if is_admin:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=False)


def academics_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_CALENDAR), KeyboardButton(BTN_ADVISOR)],
        [KeyboardButton(BTN_ROUTINES), KeyboardButton(BTN_REGPAY)],
        [KeyboardButton(BTN_BACK_NAV),  KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def waiver_calc_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("10%"), KeyboardButton("15%"), KeyboardButton("20%")],
        [KeyboardButton("25%"), KeyboardButton("30%"), KeyboardButton("50%")],
        [KeyboardButton("Custom %")],
        [KeyboardButton(BTN_BACK_NAV), KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def admin_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_ADM_ADD),       KeyboardButton(BTN_ADM_EDIT)],
        [KeyboardButton(BTN_ADM_DELETE),    KeyboardButton(BTN_ADM_OVERVIEW)],
        [KeyboardButton(BTN_ADM_EXAM),      KeyboardButton(BTN_ADM_SEMESTER)],
        [KeyboardButton(BTN_ADM_BROADCAST)],
        [KeyboardButton(BTN_BACK_NAV),      KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def add_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_ADD_BOOK),     KeyboardButton(BTN_ADD_NOTE)],
        [KeyboardButton(BTN_ADD_PSQ),      KeyboardButton(BTN_ADD_SOLVE)],
        [KeyboardButton(BTN_ADD_VIDOC),    KeyboardButton(BTN_ADD_UTIL)],
        [KeyboardButton(BTN_ADD_WAIVER),   KeyboardButton(BTN_ADD_REGPAY)],
        [KeyboardButton(BTN_ADD_SYLLABUS), KeyboardButton(BTN_ADD_OUTLINE)],
        [KeyboardButton(BTN_ADD_ROUTINE),  KeyboardButton(BTN_ADD_CAL)],
        [KeyboardButton(BTN_ADD_ADVISOR),  KeyboardButton(BTN_ADD_FEE)],
        [KeyboardButton(BTN_BACK_NAV), KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)



def edit_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_EDIT_BOOK),    KeyboardButton(BTN_EDIT_NOTE)],
        [KeyboardButton(BTN_EDIT_PSQ),     KeyboardButton(BTN_EDIT_SOLVE)],
        [KeyboardButton(BTN_EDIT_VIDOC),   KeyboardButton(BTN_EDIT_UTIL)],
        [KeyboardButton(BTN_EDIT_WAIVER),  KeyboardButton(BTN_EDIT_REGPAY)],
        [KeyboardButton(BTN_BACK_NAV),     KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def delete_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_DEL_BOOK),    KeyboardButton(BTN_DEL_NOTE)],
        [KeyboardButton(BTN_DEL_PSQ),     KeyboardButton(BTN_DEL_SOLVE)],
        [KeyboardButton(BTN_DEL_VIDOC),   KeyboardButton(BTN_DEL_UTIL)],
        [KeyboardButton(BTN_DEL_WAIVER),  KeyboardButton(BTN_DEL_REGPAY)],
        [KeyboardButton(BTN_BACK_NAV),    KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def list_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_LST_BOOK),     KeyboardButton(BTN_LST_NOTE)],
        [KeyboardButton(BTN_LST_PSQ),      KeyboardButton(BTN_LST_SOLVE)],
        [KeyboardButton(BTN_LST_VIDOC),    KeyboardButton(BTN_LST_UTIL)],
        [KeyboardButton(BTN_LST_WAIVER),   KeyboardButton(BTN_LST_REGPAY)],
        [KeyboardButton(BTN_LST_BOOK_SOL), KeyboardButton(BTN_LST_SYLLABUS)],
        [KeyboardButton(BTN_LST_OUTLINE),  KeyboardButton(BTN_LST_ROUTINE)],
        [KeyboardButton(BTN_LST_CAL),      KeyboardButton(BTN_LST_ADVISOR)],
        [KeyboardButton(BTN_LST_FEE)],
        [KeyboardButton(BTN_BACK_NAV),     KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


def semester_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(BTN_SEM_LIST),    KeyboardButton(BTN_SEM_CURRENT)],
        [KeyboardButton(BTN_SEM_CREATE),  KeyboardButton(BTN_SEM_RESTORE)],
        [KeyboardButton(BTN_SEM_RENAME),  KeyboardButton(BTN_SEM_DELETE)],
        [KeyboardButton(BTN_BACK_NAV),    KeyboardButton(BTN_MAIN_MENU)],
    ], resize_keyboard=True, is_persistent=False)


# ── Inline KB — Resources only (switch_inline_query, 2 per row) ───────────────



def _edit_delete_type_keyboard(action: str) -> InlineKeyboardMarkup:
    """
    Inline keyboard shown when admin taps Edit or Delete.
    Each button uses switch_inline_query_current_chat to pre-fill
    the inline search with the action prefix, e.g. "edit:note ".
    Admin taps a type → input box fills → inline results appear instantly.
    """
    prefix = f"{action}:"
    types = [
        ("Book",     f"{prefix}book "),
        ("Note",     f"{prefix}note "),
        ("PSQ",      f"{prefix}psq "),
        ("Solve",    f"{prefix}solve "),
        ("Vidoc",    f"{prefix}vidoc "),
        ("Syllabus", f"{prefix}syllabus "),
        ("Outline",  f"{prefix}outline "),
        ("Routine",  f"{prefix}routine "),
        ("Utility",  f"{prefix}util "),
        ("Slide",    f"{prefix}slide "),
        ("Waiver",   f"{prefix}waiver "),
        ("RegPay",   f"{prefix}regpay "),
        ("Cal",      f"{prefix}cal "),
        ("Advisor",  f"{prefix}advisor "),
    ]
    rows = []
    row  = []
    for label, query in types:
        row.append(InlineKeyboardButton(label, switch_inline_query_current_chat=query))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def resources_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Books",          switch_inline_query_current_chat="book "),
            InlineKeyboardButton("Notes",          switch_inline_query_current_chat="note "),
        ],
        [
            InlineKeyboardButton("Past Questions", switch_inline_query_current_chat="question "),
            InlineKeyboardButton("Solutions",      switch_inline_query_current_chat="solution "),
        ],
        [
            InlineKeyboardButton("Videos & Docs",  switch_inline_query_current_chat="video "),
            InlineKeyboardButton("Slides",         switch_inline_query_current_chat="slide "),
        ],
        [
            InlineKeyboardButton("Syllabuses",     switch_inline_query_current_chat="syllabus "),
            InlineKeyboardButton("Outlines",       switch_inline_query_current_chat="outline "),
        ],
        [
            InlineKeyboardButton("Utilities",      switch_inline_query_current_chat="utility "),
        ],
        [
            InlineKeyboardButton("🔍 Search anything…", switch_inline_query_current_chat=""),
        ],
    ])


# ── GROUP REDIRECT ─────────────────────────────────────────────────────────────

async def group_redirect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called for ANY message in the group.
    - Only responds to /drcrow command in allowed topics
    - Everything else → completely ignore
    """
    msg = update.effective_message
    if msg is None:
        return

    # Don't reply to the bot's own messages
    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    topic_id = msg.message_thread_id

    # Ignore messages outside allowed topics
    if topic_id not in settings.ALLOWED_TOPIC_IDS:
        return

    # Only respond to /drcrow command — ignore everything else
    text = (msg.text or "").strip().lower()
    is_drcrow = (
        text == "/drcrow"
        or text == f"/drcrow@{context.bot.username.lower()}"
    )
    if not is_drcrow:
        return

    await msg.reply_text(
"This topic is for announcements only.\nDM the bot for resources and help.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Open Dr. Crow",
                url=f"https://t.me/{context.bot.username}?start=hi"
            )
        ]])
    )


async def cmd_drcrow_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /drcrow command handler.
    - In DM → behaves like /start
    - In group → handled by group_redirect above
    """
    if update.effective_chat.type == "private":
        await cmd_start(update, context)


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await should_respond(update, context.bot):
        return

    user = update.effective_user
    await queries.upsert_user(
        user.id,
        getattr(user, "username", None) or "",
        user.full_name or ""
    )
    await queries.log_event("start", user_id=user.id)

    db_user  = await queries.get_user(user.id)
    rank     = db_user["rank"]   if db_user else "Egg"
    points   = db_user["points"] if db_user else 0
    is_admin = settings.is_admin(user.id)

    context.user_data.pop("waiver_calc", None)

    await update.message.reply_text(
        f"Welcome, {user.first_name}.",
        reply_markup=main_reply_kb(is_admin=is_admin)
    )
    await update.message.reply_text(
        "🔍",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔍  Search all resources",
                switch_inline_query_current_chat=""
            )
        ]])
    )


# ── Reply KB router ────────────────────────────────────────────────────────────

async def handle_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Central router for all Reply KB button presses."""
    text     = update.message.text.strip()
    user     = update.effective_user
    is_admin = settings.is_admin(user.id)

    # ── Main menu ──────────────────────────────────────────────────────────────

    if text == BTN_RESOURCES:
        await update.message.reply_text(
            "<b>Resources</b>\n\nChoose a category:",
            parse_mode="HTML",
            reply_markup=resources_inline_kb()
        )

    elif text == BTN_ACADEMICS:
        context.user_data["nav_state"] = "academics"
        await update.message.reply_text(
            "<b>Academics</b>\n\nChoose a section:",
            parse_mode="HTML",
            reply_markup=academics_reply_kb()
        )



    elif text == BTN_SUBS:
        from handlers.subscription import open_subscriptions
        await open_subscriptions(update, context)

    elif text == BTN_WAIVER:
        context.user_data["nav_state"] = "waiver"
        await _start_waiver_calc(update, context)

    elif text == BTN_ADMIN:
        if not is_admin:
            return
        context.user_data["nav_state"] = "admin"
        await update.message.reply_text(
            "<b>Admin Panel</b>\n\nChoose an action:",
            parse_mode="HTML",
            reply_markup=admin_reply_kb()
        )

    # ── Academics sub-menu — direct deliver ────────────────────────────────────

    elif text == BTN_CALENDAR:
        await _deliver_by_category(update, context, "cal", "Academic Calendar")

    elif text == BTN_ADVISOR:
        from handlers.advisor import handle_advisor_info
        await handle_advisor_info(update, context)

    elif text == BTN_ROUTINES:
        await _deliver_by_category(update, context, "routine", "Routines")

    elif text == BTN_REGPAY:
        await _deliver_regpay(update, context)

    # ── Waiver KB inputs ──────────────────────────────────────────────────

    elif text in ("10%", "15%", "20%", "25%", "30%", "50%"):
        await _waiver_pct_selected(update, context, float(text.replace("%", "")))

    elif text == "Custom %":
        context.user_data.setdefault("waiver_calc", {})["awaiting"] = "custom_pct"
        await update.message.reply_text(
            "Enter your waiver percentage (0-100):\nExample: 40"
        )

    elif text == BTN_BACK_NAV:
        nav = context.user_data.get("nav_state", "main")
        # Sub-menus that go back to admin panel
        if nav in ("add", "edit", "delete", "list", "semester") and is_admin:
            context.user_data["nav_state"] = "admin"
            await update.message.reply_text(
                "<b>Admin Panel</b>",
                parse_mode="HTML",
                reply_markup=admin_reply_kb()
            )
        elif nav == "admin" and is_admin:
            context.user_data["nav_state"] = "main"
            await update.message.reply_text(
                "<b>Menu</b>",
                parse_mode="HTML",
                reply_markup=main_reply_kb(is_admin=is_admin)
            )
        else:
            # academics, waiver, or any other — go to main
            context.user_data.pop("waiver_calc", None)
            context.user_data["nav_state"] = "main"
            await update.message.reply_text(
                "<b>Menu</b>",
                parse_mode="HTML",
                reply_markup=main_reply_kb(is_admin=is_admin)
            )

    # ── Admin sub-menu ─────────────────────────────────────────────────────────

    elif text == BTN_ADM_ADD and is_admin:
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Book",       callback_data="adm_add_book"),
                InlineKeyboardButton("Note",       callback_data="adm_add_note"),
                InlineKeyboardButton("PSQ",        callback_data="adm_add_psq"),
            ],
            [
                InlineKeyboardButton("Solve",      callback_data="adm_add_solve"),
                InlineKeyboardButton("Vidoc",      callback_data="adm_add_vidoc"),
                InlineKeyboardButton("Solution",   callback_data="adm_add_solution"),
            ],
            [
                InlineKeyboardButton("Correction", callback_data="adm_add_correct"),
                InlineKeyboardButton("Syllabus",   callback_data="adm_add_syllabus"),
                InlineKeyboardButton("Outline",    callback_data="adm_add_outline"),
            ],
            [
                InlineKeyboardButton("Routine",    callback_data="adm_add_routine"),
                InlineKeyboardButton("Utility",    callback_data="adm_add_util"),
                InlineKeyboardButton("Slides",     callback_data="adm_add_slide"),
                InlineKeyboardButton("Waiver",     callback_data="adm_add_waiver"),
            ],
            [
                InlineKeyboardButton("RegPay",     callback_data="adm_add_regpay"),
                InlineKeyboardButton("Cal",        callback_data="adm_add_cal"),
                InlineKeyboardButton("Advisor",    callback_data="adm_add_advisor"),
            ],
        ])
        await update.message.reply_text(
            "<b>Add — choose resource type:</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    elif text == BTN_ADM_EDIT and is_admin:
        await update.message.reply_text(
            "<b>Edit — choose resource type:</b>",
            parse_mode="HTML",
            reply_markup=_edit_delete_type_keyboard("edit")
        )

    elif text == BTN_ADM_DELETE and is_admin:
        await update.message.reply_text(
            "<b>Delete — choose resource type:</b>",
            parse_mode="HTML",
            reply_markup=_edit_delete_type_keyboard("delete")
        )


    elif text == BTN_ADM_BROADCAST and is_admin:
        pass  # Handled by broadcast_conversation() entry_point Regex filter

    elif text == BTN_ADM_OVERVIEW and is_admin:
        from handlers.admin import show_analytics_text
        await show_analytics_text(update, context)

    elif text == BTN_ADM_EXAM and is_admin:
        from handlers.exam import handle_exam_admin_menu
        await handle_exam_admin_menu(update, context)

    elif text == BTN_EXAM:
        from handlers.exam import handle_exam_member
        await handle_exam_member(update, context)

    elif text == BTN_ADM_SEMESTER and is_admin:
        pass  # Handled by semester_conversation() entry_point

    # ── Edit sub-menu ──────────────────────────────────────────────────────

    elif text in (
        BTN_EDIT_BOOK, BTN_EDIT_NOTE, BTN_EDIT_PSQ, BTN_EDIT_SOLVE,
        BTN_EDIT_VIDOC, BTN_EDIT_UTIL, BTN_EDIT_WAIVER, BTN_EDIT_REGPAY
    ) and is_admin:
        _EDIT_MAP = {
            BTN_EDIT_BOOK:   "editbook",
            BTN_EDIT_NOTE:   "editnote",
            BTN_EDIT_PSQ:    "editpsq",
            BTN_EDIT_SOLVE:  "editsolve",
            BTN_EDIT_VIDOC:  "editvidoc",
            BTN_EDIT_UTIL:   "editutil",
            BTN_EDIT_WAIVER: "editwaiver",
            BTN_EDIT_REGPAY: "editregpay",
        }
        cmd = _EDIT_MAP[text]
        context.user_data["pending_add_cmd"] = cmd
        await update.message.reply_text(
            f"<b>{text}</b>\n\n"
            f"Send the UID:\n"
            f"<code>/{cmd} &lt;uid&gt;</code>",
            parse_mode="HTML"
        )

    # ── Delete sub-menu ────────────────────────────────────────────────────

    elif text in (
        BTN_DEL_BOOK, BTN_DEL_NOTE, BTN_DEL_PSQ, BTN_DEL_SOLVE,
        BTN_DEL_VIDOC, BTN_DEL_UTIL, BTN_DEL_WAIVER, BTN_DEL_REGPAY
    ) and is_admin:
        _DEL_MAP = {
            BTN_DEL_BOOK:   "deletebook",
            BTN_DEL_NOTE:   "deletenote",
            BTN_DEL_PSQ:    "deletepsq",
            BTN_DEL_SOLVE:  "deletesolve",
            BTN_DEL_VIDOC:  "deletevidoc",
            BTN_DEL_UTIL:   "deleteutil",
            BTN_DEL_WAIVER: "deletewaiver",
            BTN_DEL_REGPAY: "deleteregpay",
        }
        cmd = _DEL_MAP[text]
        context.user_data["pending_add_cmd"] = cmd
        await update.message.reply_text(
            f"<b>{text}</b>\n\n"
            f"Send the UID:\n"
            f"<code>/{cmd} &lt;uid&gt;</code>",
            parse_mode="HTML"
        )

    # ── List sub-menu ──────────────────────────────────────────────────────

    elif text in (
        BTN_LST_BOOK, BTN_LST_NOTE, BTN_LST_PSQ, BTN_LST_SOLVE,
        BTN_LST_VIDOC, BTN_LST_UTIL, BTN_LST_WAIVER, BTN_LST_REGPAY,
        BTN_LST_BOOK_SOL, BTN_LST_CAL, BTN_LST_ADVISOR, BTN_LST_FEE,
        BTN_LST_SYLLABUS, BTN_LST_OUTLINE, BTN_LST_ROUTINE
    ) and is_admin:
        _LIST_CMD = {
            BTN_LST_BOOK:     "listbooks",
            BTN_LST_NOTE:     "listnotes",
            BTN_LST_PSQ:      "listpsqs",
            BTN_LST_SOLVE:    "listsolves",
            BTN_LST_VIDOC:    "listvidocs",
            BTN_LST_UTIL:     "listutils",
            BTN_LST_WAIVER:   "listwaivers",
            BTN_LST_REGPAY:   "listregpays",
            BTN_LST_BOOK_SOL: "listsolutions",
            BTN_LST_CAL:      "listcals",
            BTN_LST_ADVISOR:  "listadvisors",
            BTN_LST_FEE:      "listfees",
            BTN_LST_SYLLABUS: "listsyllabuses",
            BTN_LST_OUTLINE:  "listoutlines",
            BTN_LST_ROUTINE:  "listroutines",
        }
        cmd = _LIST_CMD[text]
        # Simulate the list command
        context.args = []
        await _run_list_cmd(update, context, cmd)

    # ── Semester sub-menu ──────────────────────────────────────────────────

    elif text == BTN_SEM_LIST and is_admin:
        context.args = []
        from handlers.admin_commands import cmd_semester
        await cmd_semester(update, context)

    elif text == BTN_SEM_CURRENT and is_admin:
        sem = await queries.get_current_semester()
        if sem:
            await update.message.reply_text(
                f"<b>Current Semester</b>\n\n"
                f"<code>{sem['uid']} — {sem['name']}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("No active semester.")

    elif text in (BTN_SEM_CREATE, BTN_SEM_RESTORE, BTN_SEM_RENAME, BTN_SEM_DELETE) and is_admin:
        _SEM_USAGE = {
            BTN_SEM_CREATE:  "/semester &lt;uid&gt; &lt;name&gt; | CSE315, CSE317",
            BTN_SEM_RESTORE: "/semester #&lt;uid&gt;",
            BTN_SEM_RENAME:  "/semester rename &lt;uid&gt; &lt;new name&gt;",
            BTN_SEM_DELETE:  "/semester kill &lt;uid&gt;",
        }
        await update.message.reply_text(
            f"<b>{text}</b>\n\n"
            f"<code>{_SEM_USAGE[text]}</code>",
            parse_mode="HTML"
        )

    # ── Add sub-menu ───────────────────────────────────────────────────────────

    elif text in (
        BTN_ADD_BOOK, BTN_ADD_NOTE, BTN_ADD_PSQ, BTN_ADD_SOLVE,
        BTN_ADD_VIDOC, BTN_ADD_UTIL, BTN_ADD_WAIVER, BTN_ADD_REGPAY,
        BTN_ADD_SYLLABUS, BTN_ADD_OUTLINE, BTN_ADD_ROUTINE,
        BTN_ADD_CAL, BTN_ADD_ADVISOR, BTN_ADD_FEE
    ) and is_admin:
        _CMD_MAP = {
            BTN_ADD_BOOK:     "addbook",
            BTN_ADD_NOTE:     "addnote",
            BTN_ADD_PSQ:      "addpsq",
            BTN_ADD_SOLVE:    "addsolve",
            BTN_ADD_VIDOC:    "addvidoc",
            BTN_ADD_UTIL:     "addutil",
            BTN_ADD_WAIVER:   "addwaiver",
            BTN_ADD_REGPAY:   "addregpay",
            BTN_ADD_SYLLABUS: "addsyllabus",
            BTN_ADD_OUTLINE:  "addoutline",
            BTN_ADD_ROUTINE:  "addroutine",
            BTN_ADD_CAL:      "addcal",
            BTN_ADD_ADVISOR:  "addadvisor",
            BTN_ADD_FEE:      "addfee",
        }
        cmd = _CMD_MAP[text]
        context.user_data["pending_add_cmd"] = cmd
        await update.message.reply_text(
            f"<b>{text}</b>\n\n"
            f"Send the UID for this entry:\n"
            f"<code>/{cmd} &lt;uid&gt;</code>\n\n"
            f"Or type just the UID and I'll run the command.\n"
            f"Example: <code>nm01b</code>",
            parse_mode="HTML"
        )

    # ── Navigation ─────────────────────────────────────────────────────────────


    elif text == BTN_MAIN_MENU:
        context.user_data.pop("waiver_calc", None)
        context.user_data["nav_state"] = "main"
        await update.message.reply_text(
            "<b>Menu</b>",
            parse_mode="HTML",
            reply_markup=main_reply_kb(is_admin=is_admin)
        )


# ── Waiverulator ──────────────────────────────────────────────────────────

async def _start_waiver_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.waiver_queries import get_waivers_paginated, get_waivers_count

    count = await get_waivers_count()
    if count == 0:
        await update.message.reply_text(
            "No waiver data available yet.",
            reply_markup=main_reply_kb(is_admin=settings.is_admin(update.effective_user.id))
        )
        return

    waivers = await get_waivers_paginated(offset=0, limit=1)
    w = waivers[0]

    context.user_data["waiver_calc"] = {
        "uid":          w["uid"],
        "semester":     w["semester_name"],
        "tuition_fee":  w["tuition_fee"],
        "semester_fee": w["semester_fee"],
        "step":         "pct",
        "awaiting":     None,
    }

    # Deliver policy file + URL first
    from handlers.waiver import deliver_waiver_files_only
    await deliver_waiver_files_only(update.effective_chat.id, w["uid"], context.bot)

    await update.message.reply_text(
        f"<b>Waiverulator</b>\nSemester: {w['semester_name']}\n\nSelect your waiver percentage or tap Custom %:",
        parse_mode="HTML",
        reply_markup=waiver_calc_reply_kb()
    )


async def _waiver_pct_selected(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pct: float
):
    calc = context.user_data.get("waiver_calc")
    if not calc:
        await update.message.reply_text(
            "Start the calculator again with the Waiver button."
        )
        return

    calc["waiver_pct"] = pct
    calc["step"]       = "reg_paid"
    calc["awaiting"]   = None
    context.user_data["waiver_calc"] = calc

    await update.message.reply_text(
        f"Waiver: {pct:.0f}%\n\n"
        f"How much did you pay at registration? (in BDT)\n"
        f"Example: 5000"
    )


async def handle_waiver_calc_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Handle free-text input during Waiver flow.
    Called from dm_text_handler before LLM fallback.
    Returns True if handled.
    """
    calc = context.user_data.get("waiver_calc")
    if not calc:
        return False

    text     = update.message.text.strip() if update.message.text else ""
    step     = calc.get("step")
    awaiting = calc.get("awaiting")

    if awaiting == "custom_pct":
        try:
            pct = float(text.replace("%", ""))
            if not (0 <= pct <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a valid percentage (0-100).\nExample: 40"
            )
            return True
        await _waiver_pct_selected(update, context, pct)
        return True

    if step == "reg_paid":
        try:
            paid = int(text.replace(",", "").replace("৳", "").replace(" ", ""))
            if paid < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a valid amount.\nExample: 5000"
            )
            return True

        tuition   = calc["tuition_fee"]
        semester  = calc["semester_fee"]
        pct       = calc["waiver_pct"]
        sem_name  = calc["semester"]

        waiver_amt     = int(tuition * pct / 100)
        tuition_after  = tuition - waiver_amt
        other_fees     = semester - tuition
        semester_after = other_fees + tuition_after
        remaining      = max(0, semester_after - paid)

        def _fmt(n): return f"{n:,}"
        W = 12  # fixed number column width

        _waiver_line = f"Waiver {pct:.0f}%   {('-' + _fmt(waiver_amt)).rjust(W)} BDT"
        result = (
            f"<b>Waiver Result</b>\n"
            f"<code>Semester : {sem_name}</code>\n\n"
            f"<b>Tuition Fee</b>\n"
            f"<code>"
            f"Base         {_fmt(tuition).rjust(W)} BDT\n"
            f"{_waiver_line}\n"
            f"Payable      {_fmt(tuition_after).rjust(W)} BDT"
            f"</code>\n\n"
            f"<b>Semester Fee</b>\n"
            f"<code>"
            f"Base         {_fmt(semester).rjust(W)} BDT\n"
            f"Payable      {_fmt(semester_after).rjust(W)} BDT"
            f"</code>\n\n"
            f"<b>Payment Summary</b>\n"
            f"<code>"
            f"Paid         {_fmt(paid).rjust(W)} BDT\n"
            f"Remaining    {_fmt(remaining).rjust(W)} BDT"
            f"</code>"
        )

        is_admin = settings.is_admin(update.effective_user.id)
        await update.message.reply_text(
            result,
            parse_mode="HTML",
            reply_markup=main_reply_kb(is_admin=is_admin)
        )
        context.user_data.pop("waiver_calc", None)
        return True

    return False


# ── Academics direct deliver ───────────────────────────────────────────────────

async def _deliver_by_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category: str,
    label: str
):
    """
    Fetch all utilities of a category and send them directly.
    Used for Academic Calendar, Advisor Info, Routines.
    """
    from database.utility_queries import get_utilities_by_category
    from handlers.utility import deliver_utility

    chat_id = update.effective_chat.id
    items   = await get_utilities_by_category(category, offset=0, limit=20)

    if not items:
        await update.message.reply_text(f"<b>{label}</b>\n\nNothing uploaded yet.", parse_mode="HTML")
        return

    await update.message.reply_text(f"<b>{label}</b>", parse_mode="HTML")

    for item in items:
        await deliver_utility(chat_id, item["uid"], context.bot)


async def _deliver_regpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deliver reg & payment resources from utilities table (regpay + fee categories)."""
    from database.utility_queries import get_utilities_by_category
    from handlers.utility import deliver_utility

    chat_id = update.effective_chat.id

    # New regpay resources (utilities table, category=regpay)
    regpay_items = await get_utilities_by_category("regpay", offset=0, limit=20)
    # Legacy fee items
    fee_items    = await get_utilities_by_category("fee",    offset=0, limit=10)

    all_items = regpay_items + fee_items

    if not all_items:
        await update.message.reply_text("<b>Reg & Payment</b>\n\nNothing uploaded yet.", parse_mode="HTML")
        return

    await update.message.reply_text("<b>Reg & Payment</b>", parse_mode="HTML")
    for item in all_items:
        await deliver_utility(chat_id, item["uid"], context.bot)



async def handle_admin_add_input(
    update,
    context
) -> bool:
    """
    Handle uid input after Add button pressed.
    Called from dm_text_handler. Returns True if handled.
    """
    cmd = context.user_data.get("pending_add_cmd")
    if not cmd:
        return False

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return False

    # Treat input as uid — simulate the command
    uid = text.lower().replace(" ", "")
    context.user_data.pop("pending_add_cmd", None)

    # Inject as args and call the conversation entry
    context.args = [uid]

    _ENTRY_MAP = {
        # Add
        "addbook":     ("handlers.book",    "addbook_start"),
        "addnote":     ("handlers.note",    "addnote_start"),
        "addpsq":      ("handlers.psq",     "addpsq_start"),
        "addsolve":    ("handlers.solve",   "addsolve_start"),
        "addvidoc":    ("handlers.vidoc",   "addvidoc_start"),
        "addutil":     ("handlers.utility", "addutil_start"),
        "addwaiver":   ("handlers.waiver",  "addwaiver_start"),
        "addregpay":   ("handlers.regpay",  "addregpay_start"),
        "addsyllabus": ("handlers.utility", "addsyllabus_start"),
        "addoutline":  ("handlers.utility", "addoutline_start"),
        "addroutine":  ("handlers.utility", "addroutine_start"),
        "addcal":      ("handlers.utility", "addcal_start"),
        "addadvisor":  ("handlers.utility", "addadvisor_start"),
        "addfee":      ("handlers.utility", "addfee_start"),
        # Edit
        "editbook":    ("handlers.book_edit",  "editbook_start"),
        "editnote":    ("handlers.note",       "editnote_start"),
        "editpsq":     ("handlers.psq",        "editpsq_start"),
        "editsolve":   ("handlers.solve",      "editsolve_start"),
        "editvidoc":   ("handlers.vidoc",      "editvidoc_start"),
        "editutil":    ("handlers.utility",    "editutil_start"),
        "editwaiver":  ("handlers.waiver",     "editwaiver_start"),
        "editregpay":  ("handlers.regpay",     "editregpay_start"),
        # Delete
        "deletebook":   ("handlers.book_edit",  "deletebook_start"),
        "deletenote":   ("handlers.note",       "deletenote_start"),
        "deletepsq":    ("handlers.psq",        "deletepsq_start"),
        "deletesolve":  ("handlers.solve",      "deletesolve_start"),
        "deletevidoc":  ("handlers.vidoc",      "deletevidoc_start"),
        "deleteutil":   ("handlers.utility",    "deleteutil_start"),
        "deletewaiver": ("handlers.waiver",     "deletewaiver_start"),
        "deleteregpay": ("handlers.regpay",     "deleteregpay_start"),
    }

    entry = _ENTRY_MAP.get(cmd)
    if not entry:
        return False

    import importlib
    mod = importlib.import_module(entry[0])
    fn  = getattr(mod, entry[1], None)
    if fn:
        await fn(update, context)
    return True



async def _run_list_cmd(update, context, cmd: str):
    """Run a list command directly without needing a CommandHandler."""
    _LIST_FN = {
        "listbooks":     ("handlers.book_edit",  "listbooks_cmd"),
        "listnotes":     ("handlers.note",        "listnotes_cmd"),
        "listpsqs":      ("handlers.psq",         "listpsqs_cmd"),
        "listsolves":    ("handlers.solve",       "listsolves_cmd"),
        "listvidocs":    ("handlers.vidoc",       "listvidocs_cmd"),
        "listutils":     ("handlers.utility",     "listutils_cmd"),
        "listwaivers":   ("handlers.waiver",      "listwaivers_cmd"),
        "listregpays":   ("handlers.regpay",      "listregpays_cmd"),
        "listsolutions": ("handlers.book_edit",   "listsolutions_cmd"),
        "listcals":      ("handlers.utility",     "listcals_cmd"),
        "listadvisors":  ("handlers.utility",     "listadvisors_cmd"),
        "listfees":      ("handlers.utility",     "listfees_cmd"),
        "listsyllabuses":("handlers.utility",     "_make_list_cmd"),
        "listoutlines":  ("handlers.utility",     "_make_list_cmd"),
        "listroutines":  ("handlers.utility",     "_make_list_cmd"),
    }
    import importlib
    entry = _LIST_FN.get(cmd)
    if not entry:
        await update.message.reply_text(f"Unknown list command: {cmd}")
        return
    mod = importlib.import_module(entry[0])
    if cmd in ("listsyllabuses", "listoutlines", "listroutines"):
        cat = cmd.replace("list", "").replace("es", "").replace("s", "")
        fn = getattr(mod, "_make_list_cmd")(cat)
    else:
        fn = getattr(mod, entry[1], None)
    if fn:
        await fn(update, context)


# ── Admin command references ───────────────────────────────────────────────────

_CMD_REF_ADD = (
    "<b>Add</b>\n\n"

    "<b>Resources</b>\n"
    "<code>/addbook &lt;uid&gt;</code>\n"
    "<i>file, title, authors, edition, subject, course codes, cover, tags</i>\n\n"

    "<code>/addnote &lt;uid&gt;</code>\n"
    "<i>file, title, subject, course code, cover, tags</i>\n\n"

    "<code>/addpsq &lt;uid&gt;</code>\n"
    "<i>file, cover, tags</i>\n\n"

    "<code>/addsolve &lt;uid&gt;</code>\n"
    "<i>file, title, subject, course code, cover, tags</i>\n\n"

    "<code>/addvidoc &lt;uid&gt;</code>\n"
    "<i>subject, course code, video/doc messages, cover, tags</i>\n\n"

    "<code>/addsolution &lt;uid&gt;</code>\n"
    "<i>solution manual for a book (book uid required)</i>\n\n"

    "<code>/addcorrect &lt;uid&gt;</code>\n"
    "<i>correction for a solve (solve uid required)</i>\n\n"

    "<b>Academic</b>\n"
    "<code>/addcal &lt;uid&gt;</code>\n"
    "<i>file, url, tags</i>\n\n"

    "<code>/addadvisor &lt;uid&gt;</code>\n"
    "<i>file, url, tags</i>\n\n"

    "<code>/addfee &lt;uid&gt;</code>\n"
    "<i>file, url, tags</i>\n\n"

    "<code>/addsyllabus &lt;uid&gt;</code>\n"
    "<i>file, message text, url, tags</i>\n\n"

    "<code>/addoutline &lt;uid&gt;</code>\n"
    "<i>file, message text, url, tags</i>\n\n"

    "<code>/addroutine &lt;uid&gt;</code>\n"
    "<i>file, message text, url, tags</i>\n\n"

    "<b>Tools</b>\n"
    "<code>/addwaiver &lt;uid&gt;</code>\n"
    "<i>file, url, semester name, tuition fee, semester fee, tags</i>\n\n"

    "<code>/addregpay &lt;uid&gt;</code>\n"
    "<i>files (multiple), thumbnail, tags</i>\n\n"

    "<code>/addutil &lt;uid&gt;</code>\n"
    "<i>file, message text, url, tags</i>\n\n"

    "<b>Exam</b>\n"
    "<code>/addexam &lt;name&gt; | &lt;YYYY-MM-DD&gt; | CSE311, CSE317</code>\n"
    "<i>example: /addexam Spring Final | 2026-04-25 | CSE311,CSE317</i>"
)

_CMD_REF_EDIT = (
    "<b>Edit</b>\n\n"

    "<b>Resources</b>\n"
    "<code>/editbook &lt;uid&gt;</code>\n"
    "<code>/editnote &lt;uid&gt;</code>\n"
    "<code>/editpsq &lt;uid&gt;</code>\n"
    "<code>/editsolve &lt;uid&gt;</code>\n"
    "<code>/editvidoc &lt;uid&gt;</code>\n"
    "<code>/editsolution &lt;uid&gt;</code>\n"
    "<code>/editcorrect &lt;uid&gt;</code>\n\n"

    "<b>Academic</b>\n"
    "<code>/editcal &lt;uid&gt;</code>\n"
    "<code>/editadvisor &lt;uid&gt;</code>\n"
    "<code>/editfee &lt;uid&gt;</code>\n"
    "<code>/editsyllabus &lt;uid&gt;</code>\n"
    "<code>/editoutline &lt;uid&gt;</code>\n"
    "<code>/editroutine &lt;uid&gt;</code>\n\n"

    "<b>Tools</b>\n"
    "<code>/editwaiver &lt;uid&gt;</code>\n"
    "<code>/editregpay &lt;uid&gt;</code>\n"
    "<code>/editutil &lt;uid&gt;</code>"
)

_CMD_REF_DELETE = (
    "<b>Delete</b>\n\n"

    "<b>Resources</b>\n"
    "<code>/deletebook &lt;uid&gt;</code>\n"
    "<code>/deletenote &lt;uid&gt;</code>\n"
    "<code>/deletepsq &lt;uid&gt;</code>\n"
    "<code>/deletesolve &lt;uid&gt;</code>\n"
    "<code>/deletevidoc &lt;uid&gt;</code>\n"
    "<code>/deletesolution &lt;uid&gt;</code>\n"
    "<code>/deletecorrect &lt;uid&gt;</code>\n\n"

    "<b>Academic</b>\n"
    "<code>/deletecal &lt;uid&gt;</code>\n"
    "<code>/deleteadvisor &lt;uid&gt;</code>\n"
    "<code>/deletefee &lt;uid&gt;</code>\n"
    "<code>/deletesyllabus &lt;uid&gt;</code>\n"
    "<code>/deleteoutline &lt;uid&gt;</code>\n"
    "<code>/deleteroutine &lt;uid&gt;</code>\n\n"

    "<b>Tools</b>\n"
    "<code>/deletewaiver &lt;uid&gt;</code>\n"
    "<code>/deleteregpay &lt;uid&gt;</code>\n"
    "<code>/deleteutil &lt;uid&gt;</code>\n\n"

    "<i>All deletes are permanent and cannot be undone.</i>"
)

_CMD_REF_LIST = (
    "<b>List</b>\n\n"

    "<b>Resources</b>\n"
    "<code>/listbooks</code>\n"
    "<code>/listnotes</code>\n"
    "<code>/listpsqs</code>\n"
    "<code>/listsolves</code>\n"
    "<code>/listvidocs</code>\n"
    "<code>/listsolutions</code>\n\n"

    "<b>Academic</b>\n"
    "<code>/listcals</code>\n"
    "<code>/listadvisors</code>\n"
    "<code>/listfees</code>\n"
    "<code>/listsyllabuses</code>\n"
    "<code>/listoutlines</code>\n"
    "<code>/listroutines</code>\n\n"

    "<b>Tools</b>\n"
    "<code>/listwaivers</code>\n"
    "<code>/listregpays</code>\n"
    "<code>/listutils</code>"
)

_CMD_REF_SEMESTER = (
    "<b>Semester</b>\n\n"

    "<code>/semester</code>\n"
    "<i>list all semesters</i>\n\n"

    "<code>/semester &lt;uid&gt; &lt;name&gt; | CSE315 SWE, CSE317 MM</code>\n"
    "<i>create new semester</i>\n"
    "<i>example: /semester sp26 Spring 2026 | CSE315 SWE, CSE317 MM</i>\n\n"

    "<code>/semester #&lt;uid&gt;</code>\n"
    "<i>restore a previous semester as current</i>\n\n"

    "<code>/semester rename &lt;uid&gt; &lt;new name&gt;</code>\n"
    "<i>rename an existing semester</i>\n\n"

    "<code>/semester kill &lt;uid&gt;</code>\n"
    "<i>permanently delete semester and all its resources</i>"
)