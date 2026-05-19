"""
semester.py — Inline semester management flow.

Reply KB "Semester" → inline keyboard [Create] [List]

Create flow:
  1. Semester name (e.g. Summer 2026)
  2. Courses — one per line: code | abbr | title
     e.g. CSE315 | CN | Computer Networks
  3. /done → auto-generates uid → creates semester

List flow:
  Shows all semesters with per-semester buttons:
  [Edit] [Set Current]* [Kill]
  * only shown if not already current

Edit flow (inline from List):
  [Edit Name] [Edit Courses]
  → step-by-step input
"""

import json
import logging
import re
from html import escape as h
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CallbackQueryHandler, CommandHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

from config.settings import settings
from database import queries

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

def _mark_handled(update, context):
    """Mark this update_id as handled so dm_text_handler (group=1) skips it."""
    handled = context.bot_data.setdefault("_handled_update_ids", set())
    handled.add(update.update_id)



# ── States ────────────────────────────────────────────────────────────────────
SM_MENU, SM_NAME, SM_COURSES, SM_DATE, SM_EDIT_PICK, SM_EDIT_NAME, SM_EDIT_COURSES, SM_EDIT_DATE = range(8)

# ── UID generation ────────────────────────────────────────────────────────────

def _generate_uid(sem_name: str) -> str:
    """
    Summer 2026 → su26
    Spring 2026 → sp26
    Fall 2026   → fa26
    First 2 letters of first word + last 2 digits of year.
    """
    parts = sem_name.strip().split()
    prefix = parts[0][:2].lower() if parts else "sm"
    year   = ""
    for p in parts:
        if p.isdigit() and len(p) == 4:
            year = p[-2:]
            break
    return f"{prefix}{year}" if year else f"{prefix}00"


# ── Course parsing ────────────────────────────────────────────────────────────

def _parse_courses(text: str) -> tuple[list, list]:
    """
    Parse courses from multi-line input.
    Format per line: CODE | ABBR | TITLE
    e.g. CSE315 | CN | Computer Networks

    Returns (courses_list, error_lines).
    courses_list entries: {code, abbr, title}
    """
    courses = []
    errors  = []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            errors.append(f"Line {i}: <code>{h(line)}</code> — need CODE | ABBR | TITLE")
            continue
        code, abbr, title = parts[0].upper(), parts[1].upper(), parts[2]
        if not code:
            errors.append(f"Line {i}: empty course code")
            continue
        courses.append({"code": code, "abbr": abbr, "name": title})
    return courses, errors



def _parse_date(text: str):
    """Parse dd/mm/yyyy → date object or None."""
    from datetime import datetime
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _courses_display(courses_raw) -> str:
    """Format courses list for display."""
    if isinstance(courses_raw, str):
        try:
            courses_raw = json.loads(courses_raw)
        except Exception:
            return "—"
    if not courses_raw:
        return "—"
    lines = []
    for c in courses_raw:
        code  = c.get("code", "")
        abbr  = c.get("abbr", "")
        name  = c.get("name", c.get("title", ""))
        lines.append(f"<code>{h(code)}</code> ({h(abbr)}) — {h(name)}")
    return "\n".join(lines)


def _sem_summary(sem: dict) -> str:
    cur  = " ✅ CURRENT" if sem.get("is_current") else ""
    uid  = h(sem.get("uid", ""))
    name = h(sem.get("name", ""))
    crs  = _courses_display(sem.get("courses"))
    dates = ""
    if sem.get("start_date"):
        try:
            sd = sem["start_date"]
            dates += f"\nStart: {sd.strftime('%d/%m/%Y') if hasattr(sd, 'strftime') else str(sd)}"
        except Exception:
            pass
    if sem.get("end_date"):
        try:
            ed = sem["end_date"]
            dates += f"  End: {ed.strftime('%d/%m/%Y') if hasattr(ed, 'strftime') else str(ed)}"
        except Exception:
            pass
    return (
        f"<b>{name}</b>{cur}\n"
        f"UID: <code>{uid}</code>{dates}\n"
        f"Courses:\n{crs}"
    )


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Create", callback_data="sm_create"),
            InlineKeyboardButton("List",   callback_data="sm_list"),
        ],
        [InlineKeyboardButton("Close", callback_data="sm_close")],
    ])


def _sem_action_keyboard(uid: str, is_current: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("Edit", callback_data=f"sm_edit_{uid}")]
    if not is_current:
        row.append(InlineKeyboardButton("Set Current", callback_data=f"sm_setcur_{uid}"))
    row.append(InlineKeyboardButton("Kill", callback_data=f"sm_kill_{uid}"))
    return InlineKeyboardMarkup([row])


def _edit_pick_keyboard(uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Name",    callback_data=f"sm_editname_{uid}"),
            InlineKeyboardButton("Dates",   callback_data=f"sm_editdates_{uid}"),
            InlineKeyboardButton("Courses", callback_data=f"sm_editcourses_{uid}"),
        ],
        [InlineKeyboardButton("« Back", callback_data="sm_list")],
    ])


# ── Entry ──────────────────────────────────────────────────────────────────────

async def sm_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply KB 'Semester' pressed."""
    user = update.effective_user
    if not settings.is_admin(user.id):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["_in_conversation"] = True
    await update.message.reply_text(
        "🗂 <b>Semester Management</b>",
        parse_mode=HTML,
        reply_markup=_main_keyboard()
    )
    return SM_MENU


# ── SM_MENU — main menu callbacks ─────────────────────────────────────────────

async def sm_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "sm_close":
        await query.message.edit_text("✖ Closed.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "sm_create":
        await query.message.edit_text(
            "➕ <b>Create Semester</b>\n\n"
            "Step 1/2 — Send the semester name:\n"
            "<i>e.g. Summer 2026</i>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖ Cancel", callback_data="sm_close")
            ]])
        )
        return SM_NAME

    if data == "sm_list":
        await _show_list(query, context)
        return SM_MENU

    # ── Set current ───────────────────────────────────────────────
    if data.startswith("sm_setcur_"):
        uid = data.replace("sm_setcur_", "")
        sem = await queries.activate_semester(uid)
        if sem:
            await query.message.reply_text(
                f"✅ <b>{h(sem['name'])}</b> is now the current semester.",
                parse_mode=HTML
            )
        else:
            await query.answer("Semester not found.", show_alert=True)
        await _show_list(query, context)
        return SM_MENU

    # ── Kill ──────────────────────────────────────────────────────
    if data.startswith("sm_kill_"):
        uid    = data.replace("sm_kill_", "")
        result = await queries.kill_semester(uid)
        if "error" in result:
            if result["error"] == "is_current":
                await query.answer(
                    "❌ Cannot kill current semester.\nSet another as current first.",
                    show_alert=True
                )
            else:
                await query.answer("Semester not found.", show_alert=True)
        else:
            sem   = result["semester"]
            total = sum(result["deleted"].values())
            await query.message.reply_text(
                f"🗑 <b>{h(sem['name'])}</b> killed.\n"
                f"{total} resource(s) deleted.",
                parse_mode=HTML
            )
            await _show_list(query, context)
        return SM_MENU

    # ── Edit — show pick menu ─────────────────────────────────────
    if data.startswith("sm_edit_") and not data.startswith("sm_editname_") \
            and not data.startswith("sm_editcourses_") \
            and not data.startswith("sm_editdates_"):
        uid = data.replace("sm_edit_", "")
        context.user_data["sm_edit_uid"] = uid
        sem = await _get_sem(uid)
        if not sem:
            await query.answer("Not found.", show_alert=True)
            return SM_MENU
        await query.message.edit_text(
            f"✏️ <b>Edit Semester</b>\n\n{_sem_summary(sem)}\n\nWhat to edit?",
            parse_mode=HTML,
            reply_markup=_edit_pick_keyboard(uid)
        )
        return SM_EDIT_PICK

    # ── Edit name ─────────────────────────────────────────────────
    if data.startswith("sm_editname_"):
        uid = data.replace("sm_editname_", "")
        context.user_data["sm_edit_uid"]  = uid
        context.user_data["sm_edit_field"] = "name"
        sem = await _get_sem(uid)
        await query.message.edit_text(
            f"📝 <b>Edit Name</b>\n\n"
            f"Current: <b>{h(sem['name'] if sem else '')}</b>\n\n"
            f"Send new name:\n<i>/cancel to stop</i>",
            parse_mode=HTML
        )
        return SM_EDIT_NAME

    # ── Edit courses ──────────────────────────────────────────────
    if data.startswith("sm_editcourses_"):
        uid = data.replace("sm_editcourses_", "")
        context.user_data["sm_edit_uid"]  = uid
        context.user_data["sm_edit_field"] = "courses"
        sem = await _get_sem(uid)
        cur = _courses_display(sem["courses"] if sem else [])
        await query.message.edit_text(
            f"📚 <b>Edit Courses</b>\n\n"
            f"Current:\n{cur}\n\n"
            f"Send new courses — one per line:\n"
            f"<code>CODE | ABBR | NAME</code>\n"
            f"<i>e.g. CSE315 | CN | Computer Networks</i>\n\n"
            f"Send /done when finished, /cancel to stop.",
            parse_mode=HTML
        )
        context.user_data["sm_courses_buf"] = []
        return SM_EDIT_COURSES

    if data.startswith("sm_editdates_"):
        uid = data.replace("sm_editdates_", "")
        context.user_data["sm_edit_uid"] = uid
        sem = await _get_sem(uid)
        cur_start = ""
        cur_end   = ""
        if sem:
            sd = sem.get("start_date")
            ed = sem.get("end_date")
            if sd:
                cur_start = sd.strftime("%d/%m/%Y") if hasattr(sd, "strftime") else str(sd)
            if ed:
                cur_end = ed.strftime("%d/%m/%Y") if hasattr(ed, "strftime") else str(ed)
        await query.message.edit_text(
            f"📅 <b>Edit Dates</b>\n\n"
            f"Current: {h(cur_start) or '—'} → {h(cur_end) or '—'}\n\n"
            f"Send new dates, one per line:\n"
            f"<code>dd/mm/yyyy  (start)\n"
            f"dd/mm/yyyy  (end)</code>\n\n"
            f"Or <code>-</code> to clear both dates.",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Back", callback_data=f"sm_edit_{uid}")
            ]])
        )
        return SM_EDIT_DATE

    return SM_MENU


async def _show_list(query_or_update, context):
    """Show all semesters with action buttons. Works from callback or message."""
    sems = await queries.get_all_semesters()
    if not sems:
        text = "📋 <b>No semesters yet.</b>\n\nCreate one first."
        try:
            await query_or_update.message.edit_text(text, parse_mode=HTML,
                                                     reply_markup=_main_keyboard())
        except Exception:
            await query_or_update.message.reply_text(text, parse_mode=HTML,
                                                      reply_markup=_main_keyboard())
        return

    for sem in sems:
        sem_dict = dict(sem)
        text = _sem_summary(sem_dict)
        kb   = _sem_action_keyboard(sem_dict["uid"], sem_dict["is_current"])
        try:
            await query_or_update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
        except Exception:
            pass

    # Show main menu again at the bottom
    try:
        await query_or_update.message.edit_text(
            "📋 <b>All Semesters</b>", parse_mode=HTML, reply_markup=_main_keyboard()
        )
    except Exception:
        await query_or_update.message.reply_text(
            "📋 <b>All Semesters</b>", parse_mode=HTML, reply_markup=_main_keyboard()
        )


async def _get_sem(uid: str) -> dict | None:
    sems = await queries.get_all_semesters()
    for s in sems:
        if s["uid"] == uid:
            return dict(s)
    return None


# ── SM_NAME — semester name input ─────────────────────────────────────────────

async def sm_name_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty.")
        return SM_NAME

    uid = _generate_uid(name)
    context.user_data["sm_new_name"] = name
    context.user_data["sm_new_uid"]  = uid

    await update.message.reply_text(
        f"✅ Name: <b>{h(name)}</b>  UID: <code>{h(uid)}</code>\n\n"
        f"Step 2/2 — Send all courses and dates in one message:\n\n"
        f"<code>CODE | ABBR | TITLE\n"
        f"CODE | ABBR | TITLE\n"
        f"dd/mm/yyyy\n"
        f"dd/mm/yyyy</code>\n\n"
        f"Example:\n"
        f"<code>CSE315 | CN | Computer Networks\n"
        f"CSE317 | MM | Microprocessors\n"
        f"CSE321 | OS | Operating Systems\n"
        f"01/06/2026\n"
        f"30/09/2026</code>\n\n"
        f"Dates are optional — skip them if not set yet.",
        parse_mode=HTML
    )
    return SM_COURSES


# ── SM_COURSES — single message with courses + optional dates ─────────────────

async def sm_courses_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin sends ALL courses + optional dates in one message.
    Format:
      CSE315 | CN | Computer Networks
      CSE317 | MM | Microprocessors
      01/06/2026
      30/09/2026
    Dates are detected by dd/mm/yyyy pattern — must come after course lines.
    """
    ud   = context.user_data
    text = update.message.text.strip()

    lines      = [l.strip() for l in text.splitlines() if l.strip()]
    courses    = []
    start_date = None
    end_date   = None
    errors     = []
    date_lines = []

    for i, line in enumerate(lines):
        # Check if line looks like a date
        d = _parse_date(line)
        if d is not None:
            date_lines.append(d)
            continue
        # Try parsing as course
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            errors.append(f"Line {i+1}: <code>{h(line)}</code> — need CODE | ABBR | NAME or dd/mm/yyyy")
            continue
        code, abbr, name_val = parts[0].upper(), parts[1].upper(), parts[2]
        if not code:
            errors.append(f"Line {i+1}: empty course code")
            continue
        courses.append({"code": code, "abbr": abbr, "name": name_val})

    if errors:
        await update.message.reply_text(
            "❌ Some lines have errors:\n\n" + "\n".join(errors) +
            "\n\nFix and resend.",
            parse_mode=HTML
        )
        return SM_COURSES

    if not courses:
        await update.message.reply_text(
            "❌ No valid courses found. Send at least one course line."
        )
        return SM_COURSES

    if len(date_lines) >= 1:
        start_date = date_lines[0]
    if len(date_lines) >= 2:
        end_date = date_lines[1]

    if start_date and end_date and end_date <= start_date:
        await update.message.reply_text("❌ End date must be after start date.")
        return SM_COURSES

    # All good — create semester
    name = ud.get("sm_new_name", "")
    uid  = ud.get("sm_new_uid", "")

    if not name or not uid:
        await update.message.reply_text("❌ Something went wrong. Start over.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        await _create_semester_with_dates(uid, name, courses, start_date, end_date)
    except Exception as e:
        if "unique" in str(e).lower():
            uid = f"{uid}a"
            ud["sm_new_uid"] = uid
            try:
                await _create_semester_with_dates(uid, name, courses, start_date, end_date)
            except Exception as e2:
                await update.message.reply_text(f"❌ Failed: {h(str(e2))}", parse_mode=HTML)
                return SM_COURSES
        else:
            await update.message.reply_text(f"❌ Failed: {h(str(e))}", parse_mode=HTML)
            return SM_COURSES

    await queries.reset_course_subscriptions_all()

    course_lines = "\n".join(
        f"<code>{h(c['code'])}</code> ({h(c.get('abbr',''))}) — {h(c.get('name',''))}"
        for c in courses
    )
    date_info = ""
    if start_date:
        date_info += f"\nStart: {start_date.strftime('%d/%m/%Y')}"
    if end_date:
        date_info += f"  End: {end_date.strftime('%d/%m/%Y')}"

    _mark_handled(update, context)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>{h(name)}</b> (<code>{h(uid)}</code>) created and set as current.\n\n"
        f"Courses ({len(courses)}):\n{course_lines}"
        f"{date_info}\n\n"
        f"Course subscriptions reset.",
        parse_mode=HTML
    )
    return ConversationHandler.END

async def _create_semester_with_dates(uid, name, courses, start_date, end_date):
    """Create semester with optional start/end dates."""
    from database.db import get_pool
    import json as _j
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE semesters SET is_current = FALSE")
            row = await conn.fetchrow("""
                INSERT INTO semesters (uid, name, courses, is_current, start_date, end_date)
                VALUES ($1, $2, $3, TRUE, $4, $5)
                RETURNING *
            """, uid.lower().strip(), name, _j.dumps(courses), start_date, end_date)
        return row


# ── SM_EDIT_PICK — choose what to edit ────────────────────────────────────────

async def sm_edit_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "sm_list":
        await _show_list(query, context)
        return SM_MENU

    # Re-route to menu cb for editname/editcourses
    return await sm_menu_cb(update, context)


# ── SM_EDIT_NAME — new name input ─────────────────────────────────────────────

async def sm_edit_name_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    uid      = context.user_data.get("sm_edit_uid", "")

    if not new_name:
        await update.message.reply_text("❌ Name cannot be empty.")
        return SM_EDIT_NAME

    ok = await queries.rename_semester(uid, new_name)
    if ok:
        await update.message.reply_text(
            f"✅ Renamed to <b>{h(new_name)}</b>.", parse_mode=HTML
        )
    else:
        await update.message.reply_text("❌ Semester not found.")

    _mark_handled(update, context)
    context.user_data.pop("sm_edit_uid", None)
    context.user_data.pop("sm_edit_field", None)
    return ConversationHandler.END


# ── SM_EDIT_COURSES — replace courses ─────────────────────────────────────────

async def sm_edit_courses_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    buf  = context.user_data.get("sm_courses_buf", [])

    courses, errors = _parse_courses(text)
    if errors:
        await update.message.reply_text(
            "❌ Errors:\n\n" + "\n".join(errors) +
            "\n\nFix and resend, or /done to save valid courses.",
            parse_mode=HTML
        )
        return SM_EDIT_COURSES

    buf.extend(courses)
    context.user_data["sm_courses_buf"] = buf
    await update.message.reply_text(
        f"✅ {len(courses)} course(s) added. Total: {len(buf)}. Send more or /done."
    )
    return SM_EDIT_COURSES


async def sm_edit_courses_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done — save updated courses."""
    uid     = context.user_data.get("sm_edit_uid", "")
    courses = context.user_data.get("sm_courses_buf", [])

    if not courses:
        await update.message.reply_text("❌ No valid courses. Send at least one.")
        return SM_EDIT_COURSES

    await _update_semester_courses(uid, courses)
    course_lines = "\n".join(
        f"<code>{h(c['code'])}</code> ({h(c.get('abbr',''))}) — {h(c.get('name', c.get('title','')))}"
        for c in courses
    )
    await update.message.reply_text(
        f"✅ <b>Courses updated</b> ({len(courses)}):\n{course_lines}",
        parse_mode=HTML
    )
    _mark_handled(update, context)
    context.user_data.clear()
    return ConversationHandler.END


async def _update_semester_courses(uid: str, courses: list):
    """Update semester courses in DB."""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE semesters SET courses = $1 WHERE uid = $2",
            json.dumps(courses), uid.lower().strip()
        )


async def sm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ── ConversationHandler ────────────────────────────────────────────────────────


# ── SM_EDIT_DATE — update start/end dates ─────────────────────────────────────

async def sm_edit_date_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new start/end dates and save them."""
    uid  = context.user_data.get("sm_edit_uid", "")
    text = update.message.text.strip()

    from database.db import get_pool
    pool = await get_pool()

    if text == "-":
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE semesters SET start_date = NULL, end_date = NULL WHERE uid = $1",
                uid
            )
        _mark_handled(update, context)
        await update.message.reply_text("✅ Dates cleared.")
        context.user_data.clear()
        return ConversationHandler.END

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    start_date = _parse_date(lines[0]) if len(lines) >= 1 else None
    end_date   = _parse_date(lines[1]) if len(lines) >= 2 else None

    if lines and not start_date:
        await update.message.reply_text(
            f"❌ Invalid start date: <code>{h(lines[0])}</code>\nUse dd/mm/yyyy.",
            parse_mode=HTML
        )
        return SM_EDIT_DATE

    if len(lines) >= 2 and not end_date:
        await update.message.reply_text(
            f"❌ Invalid end date: <code>{h(lines[1])}</code>\nUse dd/mm/yyyy.",
            parse_mode=HTML
        )
        return SM_EDIT_DATE

    if start_date and end_date and end_date <= start_date:
        await update.message.reply_text("❌ End date must be after start date.")
        return SM_EDIT_DATE

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE semesters SET start_date = $1, end_date = $2 WHERE uid = $3",
            start_date, end_date, uid
        )

    info = []
    if start_date: info.append(f"Start: {start_date.strftime('%d/%m/%Y')}")
    if end_date:   info.append(f"End: {end_date.strftime('%d/%m/%Y')}")
    _mark_handled(update, context)
    await update.message.reply_text(
        f"✅ Dates updated.\n" + "  ".join(info),
        parse_mode=HTML
    )
    context.user_data.clear()
    return ConversationHandler.END


def semester_conversation() -> ConversationHandler:
    done_edit  = CommandHandler("done",   sm_edit_courses_done)
    cancel_cmd = CommandHandler("cancel", sm_cancel)

    all_sm_cbs = CallbackQueryHandler(sm_menu_cb, pattern="^sm_")

    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(r"^Semester$"),
                sm_entry
            ),
        ],
        states={
            SM_MENU: [
                all_sm_cbs,
            ],
            SM_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sm_name_msg),
                CallbackQueryHandler(sm_menu_cb, pattern="^sm_close$"),
            ],
            SM_COURSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sm_courses_msg),
            ],
            SM_EDIT_PICK: [
                CallbackQueryHandler(sm_edit_pick_cb, pattern="^sm_"),
            ],
            SM_EDIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sm_edit_name_msg),
            ],
            SM_EDIT_COURSES: [
                done_edit,
                MessageHandler(filters.TEXT & ~filters.COMMAND, sm_edit_courses_msg),
            ],
            SM_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sm_edit_date_msg),
                CallbackQueryHandler(sm_menu_cb, pattern="^sm_edit_"),
            ],
        },
        fallbacks=[cancel_cmd],
        conversation_timeout=600,
        per_message=False,
        allow_reentry=True,
    )