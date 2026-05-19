"""
Subscription management handler.
Member selects courses (dynamic from current semester) + categories per course,
plus global topics. Accessible from /start menu button.

State stored in context.user_data["sub_state"]:
{
  "selected_courses": {"CSE322", "CSE315"},
  "course_cats":      {"CSE322": {"notes","books"}, "CSE315": {"solutions"}},
  "global_cats":      {"broadcast", "calendar"},
  "step":             "courses" | "categories" | "global"
}
"""

import logging
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from database.queries import (
    get_courses_current_semester,
    get_distinct_courses_current_semester,
    get_user_subscriptions,
    save_user_subscriptions,
    COURSE_CATEGORIES, GLOBAL_CATEGORIES,
)

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

# ── Labels ────────────────────────────────────────────────────────────────────

_CAT_LABEL = {
    "books":     "Books",
    "notes":     "Notes",
    "solutions": "Solutions",
    "psqs":      "Past Questions",
    "videos":    "Videos & Docs",
    "utilities": "Utilities",
    "syllabus":  "Syllabus",
    "outline":   "Outlines",
    "routine":   "Routines",
    "broadcast": "Broadcast",
    "calendar":  "Calendar",
    "advisor":   "Advisor Info",
    "regpay":    "Reg & Payment",
}

_TICK = "✅"
_DOT  = "⬜"


def _label(key: str, selected: bool) -> str:
    base = _CAT_LABEL.get(key, key)
    return f"{_TICK} {base}" if selected else f"{_DOT} {base}"


# ── Entry ─────────────────────────────────────────────────────────────────────


async def _edit_or_send(query, context, text: str, reply_markup, parse_mode=None):
    """Edit existing message (callback) or edit the stored sub message (reply KB)."""
    if query is not None:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    else:
        bot     = context.bot
        chat_id = context.user_data.get("sub_chat_id")
        msg_id  = context.user_data.get("sub_msg_id")
        if chat_id and msg_id:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode=parse_mode, reply_markup=reply_markup
            )


async def open_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — works from both callback_query and reply KB message."""
    query = update.callback_query
    user  = update.effective_user

    subs = await get_user_subscriptions(user.id)
    selected_courses = set()
    course_cats: dict[str, set] = {}
    global_cats: set = set()

    for sub in subs:
        if sub["course_code"] is None:
            global_cats.add(sub["category"])
        else:
            cc = sub["course_code"]
            selected_courses.add(cc)
            course_cats.setdefault(cc, set()).add(sub["category"])

    context.user_data["sub_state"] = {
        "selected_courses": selected_courses,
        "course_cats":      course_cats,
        "global_cats":      global_cats,
        "step":             "courses",
    }

    if query is None:
        # Entry from reply KB — send a new message, store its id for future edits
        sent = await update.message.reply_text("Loading subscriptions...")
        context.user_data["sub_msg_id"] = sent.message_id
        context.user_data["sub_chat_id"] = sent.chat_id
        await _show_courses_step(None, context, bot=context.bot)
    else:
        await _show_courses_step(query, context)


# ── Step 1: Course selection ──────────────────────────────────────────────────

async def _show_courses_step(query, context: ContextTypes.DEFAULT_TYPE, bot=None):
    course_list = await get_courses_current_semester()  # [{code, title}, ...]
    state   = context.user_data["sub_state"]
    selected = state["selected_courses"]

    if not course_list:
        await _edit_or_send(query, context,
            "No courses in current semester.\n"
            "You can still set general subscriptions.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("General Topics ->", callback_data="sub_step_global"),
                InlineKeyboardButton("Close",             callback_data="sub_close"),
            ]])
        )
        return

    buttons = []
    row = []
    for i, c in enumerate(course_list):
        code  = c.get("code", "")
        title = c.get("title", "")
        label = f"{code} - {title}" if title else code
        is_sel = code in selected
        row.append(InlineKeyboardButton(
            f"{_TICK if is_sel else _DOT} {label}",
            callback_data=f"sub_course_{code}"
        ))
        if len(row) == 2 or i == len(course_list) - 1:
            buttons.append(row)
            row = []

    # Add "Pick categories" button for each selected course
    for code in sorted(selected):
        cats = state["course_cats"].get(code, set())
        cat_count = f" ({len(cats)})" if cats else " — tap to pick"
        buttons.append([InlineKeyboardButton(
            f"[+] {code}{cat_count}",
            callback_data=f"sub_editcat_{code}"
        )])

    buttons.append([
        InlineKeyboardButton("General Topics ->", callback_data="sub_step_global"),
        InlineKeyboardButton("Save",              callback_data="sub_save"),
    ])
    buttons.append([InlineKeyboardButton("Close", callback_data="sub_close")])

    sel_count = len(selected)
    await _edit_or_send(query, context,
        f"<b>Subscriptions</b>\n"
        f"<code>Courses — {sel_count} selected</code>",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ── Step 2: Category selection for a specific course ─────────────────────────

async def _show_category_step(query, context: ContextTypes.DEFAULT_TYPE, course_code: str):
    state    = context.user_data["sub_state"]
    selected = state["course_cats"].get(course_code, set())

    buttons = []
    row = []
    for i, cat in enumerate(COURSE_CATEGORIES):
        is_sel = cat in selected
        row.append(InlineKeyboardButton(
            _label(cat, is_sel),
            callback_data=f"sub_cat_{course_code}_{cat}"
        ))
        if len(row) == 2 or i == len(COURSE_CATEGORIES) - 1:
            buttons.append(row)
            row = []

    buttons.append([
        InlineKeyboardButton("< Back", callback_data="sub_step_courses"),
        InlineKeyboardButton("Save",            callback_data="sub_save"),
    ])

    sel_count = len(selected)
    await _edit_or_send(query, context,
        f"<b>{h(course_code)}</b>\n"
        f"<code>Categories — {sel_count} selected</code>",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ── Step 3: Global topics ─────────────────────────────────────────────────────

async def _show_global_step(query, context: ContextTypes.DEFAULT_TYPE):
    state    = context.user_data["sub_state"]
    selected = state["global_cats"]

    buttons = []
    for cat in GLOBAL_CATEGORIES:
        is_sel = cat in selected
        buttons.append([InlineKeyboardButton(
            _label(cat, is_sel),
            callback_data=f"sub_global_{cat}"
        )])

    buttons.append([
        InlineKeyboardButton("< Back", callback_data="sub_step_courses"),
        InlineKeyboardButton("Save",            callback_data="sub_save"),
    ])

    await _edit_or_send(query, context,
        f"<b>General Subscriptions</b>\n"
        f"<code>Broadcast is on by default</code>",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ── Save ──────────────────────────────────────────────────────────────────────

async def _save_subscriptions(query, context: ContextTypes.DEFAULT_TYPE):
    user  = query.from_user
    state = context.user_data.get("sub_state", {})

    selected_courses = state.get("selected_courses", set())
    course_cats      = state.get("course_cats", {})
    global_cats      = state.get("global_cats", set())

    # Build course_subs list — only courses that have at least 1 category
    course_subs = []
    for course in selected_courses:
        cats = course_cats.get(course, set())
        for cat in cats:
            course_subs.append({"course_code": course, "category": cat})

    global_list = list(global_cats)

    await save_user_subscriptions(
        user_id      = user.id,
        course_subs  = course_subs,
        global_subs  = global_list,
        preserve_global = False,
    )

    # Summary
    course_summary = ""
    for course in sorted(selected_courses):
        cats = course_cats.get(course, set())
        if cats:
            labels = ", ".join(_CAT_LABEL.get(c, c) for c in sorted(cats))
            course_summary += f"• <b>{h(course)}</b>: {h(labels)}\n"

    global_summary = ", ".join(_CAT_LABEL.get(c, c) for c in sorted(global_cats)) or "None"

    if not course_summary and not global_cats:
        summary = "You have no active subscriptions."
    else:
        summary = (
            f"<b>Courses</b>\n{course_summary or 'None'}\n"
            f"<b>General</b>  {h(global_summary)}"
        )

    context.user_data.pop("sub_state", None)

    await _edit_or_send(query, context,
        f"<b>Saved</b>\n\n{summary}",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Edit Again",    callback_data="sub_edit_again"),
            InlineKeyboardButton("Close",       callback_data="sub_close"),
        ]])
    )


# ── Main callback dispatcher ──────────────────────────────────────────────────

async def handle_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    await query.answer()

    # Ensure state exists (e.g. if bot restarted mid-session)
    if "sub_state" not in context.user_data and data != "sub_close":
        await open_subscriptions(update, context)
        return

    state = context.user_data.get("sub_state", {})

    # ── Navigation ────────────────────────────────────────────────
    if data == "sub_step_courses":
        state["step"] = "courses"
        await _show_courses_step(query, context)

    elif data == "sub_step_global":
        state["step"] = "global"
        await _show_global_step(query, context)

    elif data == "sub_close":
        context.user_data.pop("sub_state", None)
        await query.message.delete()

    elif data == "sub_save":
        await _save_subscriptions(query, context)

    elif data == "sub_edit_again":
        # Re-open subscriptions from saved state
        await open_subscriptions(update, context)

    # ── Course toggle ─────────────────────────────────────────────
    elif data.startswith("sub_course_"):
        course = data[len("sub_course_"):]
        selected = state["selected_courses"]

        if course in selected:
            # Tap on selected course → deselect and remove its categories
            selected.discard(course)
            state["course_cats"].pop(course, None)
            await _show_courses_step(query, context)
        else:
            # First tap → select then immediately go to category step
            selected.add(course)
            state["step"] = "categories"
            await _show_category_step(query, context, course)

    # ── Category toggle ───────────────────────────────────────────
    elif data.startswith("sub_cat_"):
        # format: sub_cat_CSE322_notes
        rest   = data[len("sub_cat_"):]
        # course_code may contain underscores — split from right on last _
        parts  = rest.rsplit("_", 1)
        if len(parts) != 2:
            return
        course, cat = parts
        cats = state["course_cats"].setdefault(course, set())
        if cat in cats:
            cats.discard(cat)
        else:
            cats.add(cat)
        await _show_category_step(query, context, course)

    # ── Global toggle ─────────────────────────────────────────────
    elif data.startswith("sub_editcat_"):
        # Tap "Pick categories" row for a specific course
        course = data[len("sub_editcat_"):]
        state["step"] = "categories"
        await _show_category_step(query, context, course)

    elif data.startswith("sub_global_"):
        cat = data[len("sub_global_"):]
        g   = state["global_cats"]
        if cat in g:
            g.discard(cat)
        else:
            g.add(cat)
        await _show_global_step(query, context)