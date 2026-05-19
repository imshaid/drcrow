"""
Extra admin commands: /addexam, /feature, /deactivate, /broadcast
These are DM-only commands, admin-gated.
"""

import logging
import asyncio
import re
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config.settings import settings
from database import queries

logger = logging.getLogger(__name__)


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not settings.is_admin(user.id):
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_addexam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addexam <name> | <YYYY-MM-DD> | CSE311,CSE317
    Example: /addexam Spring Final | 2026-04-25 | CSE311,CSE317
    """
    text = update.message.text.replace("/addexam", "").strip()
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        await update.message.reply_text(
            "❌ Format: `/addexam <name> | <YYYY-MM-DD> | CSE311,CSE317`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    name, date_str, courses_str = parts
    try:
        exam_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text("❌ Invalid date format. Use YYYY-MM-DD.")
        return

    courses = [c.strip().upper() for c in courses_str.split(",") if c.strip()]
    event_id = await queries.insert_exam_event(name, exam_date, courses)

    await update.message.reply_text(
        f"✅ *Exam event created!* (ID: {event_id})\n\n"
        f"📌 *{name}*\n"
        f"📅 Date: {exam_date}\n"
        f"📚 Courses: {', '.join(courses)}\n\n"
        f"Exam countdown alerts will fire at T-3 and T-1 days automatically.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def cmd_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /feature <resource_id>
    Marks a resource as featured and awards +15 pts to uploader.
    """
    text = update.message.text.replace("/feature", "").strip()
    try:
        resource_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Usage: `/feature <resource_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    resource = await queries.get_resource(resource_id)
    if not resource:
        await update.message.reply_text("❌ Resource not found.")
        return

    await queries.feature_resource(resource_id, True)
    if resource["uploaded_by"]:
        await queries.add_stars(resource["uploaded_by"], 5, "resource_featured")
        try:
            await context.bot.send_message(
                resource["uploaded_by"],
                f"Your resource <b>{resource['title']}</b> has been featured! +5 ⭐",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"⭐ *{resource['title']}* is now featured! Uploader got +15 pts.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /deactivate <resource_id>
    Removes a resource from search results.
    """
    text = update.message.text.replace("/deactivate", "").strip()
    try:
        resource_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Usage: `/deactivate <resource_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    resource = await queries.get_resource(resource_id)
    if not resource:
        await update.message.reply_text("❌ Resource not found or already inactive.")
        return

    await queries.deactivate_resource(resource_id)
    await update.message.reply_text(
        f"🗑 *{resource['title']}* (ID: {resource_id}) has been deactivated.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def cmd_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /semester                                    → list all semesters
    /semester <uid> <name> | course1, course2    → create new semester
    /semester #<uid>                             → restore/bake a previous semester
    /semester rename <uid> <new name>            → rename a semester
    /semester kill <uid>                         → permanently delete semester + all its resources

    Course format: CSE315 SWE, CSE317 MM, CSE321 CN
    (code and title space-separated, courses comma-separated)

    Examples:
      /semester sp26 Spring 2026 | CSE315 SWE, CSE317 MM, CSE321 CN
      /semester #sp26
      /semester rename sp26 Spring 2026 Updated
      /semester kill sp26
    """
    from database.queries import (
        create_semester, activate_semester, rename_semester,
        kill_semester, get_semester_resource_counts,
    )
    msg  = update.message
    text = msg.text.replace("/semester", "").strip()

    # ── No args → list semesters ──────────────────────────────────
    if not text:
        sems = await queries.get_all_semesters()
        if not sems:
            await msg.reply_text(
                "No semesters yet.\n"
                "Create: `/semester <uid> <name> | course1 title, course2 title`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        lines = []
        for s in sems:
            marker = "▶️ *CURRENT*" if s["is_current"] else f"`{s['uid']}`"
            lines.append(f"{marker} — *{s['name']}*")
        await msg.reply_text(
            "*All Semesters:*\n\n" + "\n".join(lines) + "\n\n"
            "_Use `/semester #<uid>` to restore, `/semester kill <uid>` to delete._",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ── Restore/bake by uid ───────────────────────────────────────
    if text.startswith("#"):
        uid = text[1:].strip()
        sem = await activate_semester(uid)
        if not sem:
            await msg.reply_text(f"❌ Semester `{uid}` not found.", parse_mode=ParseMode.MARKDOWN)
            return
        await msg.reply_text(
            f"✅ *{sem['name']}* (`{uid}`) is now the active semester.\n"
            f"_Course subscriptions are NOT reset on restore._",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ── Rename ────────────────────────────────────────────────────
    if text.lower().startswith("rename "):
        parts = text[7:].strip().split(" ", 1)
        if len(parts) != 2:
            await msg.reply_text("❌ Usage: `/semester rename <uid> <new name>`", parse_mode=ParseMode.MARKDOWN)
            return
        uid, new_name = parts[0].strip(), parts[1].strip()
        ok = await rename_semester(uid, new_name)
        if not ok:
            await msg.reply_text(f"❌ Semester `{uid}` not found.", parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.reply_text(f"✅ Renamed to *{new_name}*.", parse_mode=ParseMode.MARKDOWN)
        return

    # ── Kill ──────────────────────────────────────────────────────
    if text.lower().startswith("kill "):
        uid = text[5:].strip()
        result = await kill_semester(uid)
        if "error" in result:
            if result["error"] == "not_found":
                await msg.reply_text(f"❌ Semester `{uid}` not found.", parse_mode=ParseMode.MARKDOWN)
            elif result["error"] == "is_current":
                await msg.reply_text(
                    "❌ Cannot kill the *current* semester.\n"
                    "Restore another semester first, then kill this one.",
                    parse_mode=ParseMode.MARKDOWN
                )
            return
        sem  = result["semester"]
        dels = result["deleted"]
        total = sum(dels.values())
        summary = "  ".join(f"{t}: `{n}`" for t, n in dels.items() if n > 0) or "none"
        await msg.reply_text(
            f"🗑 *{sem['name']}* (`{uid}`) killed.\n\n"
            f"Deleted {total} rows:\n{summary}",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ── Create new semester ───────────────────────────────────────
    # Format: <uid> <name> | course1 title, course2 title, ...
    if "|" not in text:
        await msg.reply_text(
            "❌ Format:\n"
            "`/semester <uid> <name> | CSE315 SWE, CSE317 MM`\n\n"
            "Example:\n"
            "`/semester sp26 Spring 2026 | CSE315 SWE, CSE317 MM, CSE321 CN`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    meta_part, courses_part = text.split("|", 1)
    meta_tokens = meta_part.strip().split(" ", 1)
    if len(meta_tokens) < 2:
        await msg.reply_text(
            "❌ Missing UID or name.\n"
            "Format: `/semester <uid> <name> | courses`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    uid       = meta_tokens[0].strip().lower()
    sem_name  = meta_tokens[1].strip()

    # Parse courses: "CSE315 SWE, CSE317 MM" → [{code, title}, ...]
    courses = []
    for raw in courses_part.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(" ", 1)
        code  = parts[0].strip().upper()
        title = parts[1].strip().upper() if len(parts) > 1 else ""
        if code:
            courses.append({"code": code, "title": title})

    if not uid or not sem_name:
        await msg.reply_text("❌ UID and name cannot be empty.")
        return

    try:
        sem = await create_semester(uid, sem_name, courses)
    except Exception as e:
        if "unique" in str(e).lower():
            await msg.reply_text(
                f"❌ UID `{uid}` already exists. Choose a different one.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await msg.reply_text(f"❌ Error: {e}")
        return

    # Reset course subscriptions
    await queries.reset_course_subscriptions_all()

    course_lines = "\n".join(
        f"  • `{c['code']}` — {c['title']}" for c in courses
    ) or "  _(none)_"

    counts = await get_semester_resource_counts(sem["id"])

    await msg.reply_text(
        f"✅ *{sem_name}* (`{uid}`) created and set as current.\n\n"
        f"📚 *Courses ({len(courses)}):*\n{course_lines}\n\n"
        f"🔔 Course subscriptions reset for all members.\n\n"
        f"🧹 *Cleanup Panel* (previous semester data):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cleanup_panel_keyboard()
    )


def _cleanup_panel_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [
        [
            InlineKeyboardButton("📗 Books",     callback_data="clean_books"),
            InlineKeyboardButton("📝 Notes",     callback_data="clean_notes"),
            InlineKeyboardButton("✅ Solves",    callback_data="clean_solves"),
        ],
        [
            InlineKeyboardButton("📋 PSQs",      callback_data="clean_psqs"),
            InlineKeyboardButton("🎥 VideoDocs", callback_data="clean_vidocs"),
            InlineKeyboardButton("🛠 Utilities", callback_data="clean_utilities"),
        ],
        [
            InlineKeyboardButton("💸 Waivers",   callback_data="clean_waivers"),
            InlineKeyboardButton("🧾 RegPay",    callback_data="clean_regpay"),
            InlineKeyboardButton("📦 Resources", callback_data="clean_resources"),
        ],
        [
            InlineKeyboardButton("🗑 Clear ALL", callback_data="clean_all"),
            InlineKeyboardButton("✖️ Done",      callback_data="clean_done"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


_CLEANUP_TARGETS = {
    "books":     ("SELECT COUNT(*) FROM books",     "DELETE FROM books",     "Books",     ""),
    "notes":     ("SELECT COUNT(*) FROM notes",     "DELETE FROM notes",     "Notes",     ""),
    "solves":    ("SELECT COUNT(*) FROM solves",    "DELETE FROM solves",    "Solves",    "Also deletes linked corrections."),
    "psqs":      ("SELECT COUNT(*) FROM psqs",      "DELETE FROM psqs",      "PSQs",      ""),
    "vidocs":    ("SELECT COUNT(*) FROM vidocs",    "DELETE FROM vidocs",    "VideoDocs", ""),
    "utilities": ("SELECT COUNT(*) FROM utilities", "DELETE FROM utilities", "Utilities", ""),
    "waivers":   ("SELECT COUNT(*) FROM waivers",   "DELETE FROM waivers",   "Waivers",   ""),
    "regpay":    ("SELECT COUNT(*) FROM regpay",    "DELETE FROM regpay",    "RegPay",    ""),
    "resources": (
        "SELECT COUNT(*) FROM resources WHERE is_active = TRUE",
        "UPDATE resources SET is_active = FALSE",
        "Resources",
        "Soft-deactivates — rows kept in DB for history."
    ),
}


@admin_only
async def handle_cleanup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all clean_* callbacks from the semester cleanup panel."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    data  = query.data
    user  = update.effective_user

    await query.answer()

    if data == "clean_done":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Cleanup session closed.")
        return

    pool = await queries.get_pool()

    if data == "clean_all":
        # Confirm step
        await query.message.reply_text(
            "⚠️ *This will permanently delete ALL resources across every table.*\n\n"
            "Are you absolutely sure?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, clear everything", callback_data="clean_all_confirm"),
                InlineKeyboardButton("❌ Cancel",                callback_data="clean_done"),
            ]])
        )
        return

    if data == "clean_all_confirm":
        summary = []
        async with pool.acquire() as conn:
            for key, (count_sql, delete_sql, label, _) in _CLEANUP_TARGETS.items():
                before = await conn.fetchval(count_sql)
                await conn.execute(delete_sql)
                summary.append(f"• {label}: {before} cleared")
        await query.message.reply_text(
            "🗑 *Full cleanup done:*\n\n" + "\n".join(summary),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Single table cleanup — extract key
    key = data.replace("clean_", "")
    if key not in _CLEANUP_TARGETS:
        return

    count_sql, delete_sql, label, note = _CLEANUP_TARGETS[key]

    async with pool.acquire() as conn:
        before = await conn.fetchval(count_sql)
        await conn.execute(delete_sql)
        after  = await conn.fetchval(count_sql)

    note_line = f"\n_{note}_" if note else ""
    await query.message.reply_text(
        f"🗑 *{label}* cleared.\n"
        f"Removed: `{before - after}` rows  |  Remaining: `{after}`"
        f"{note_line}",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick stats summary for admin."""
    pool = await queries.get_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        active_members = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_member = TRUE")
        total_resources = await conn.fetchval("SELECT COUNT(*) FROM resources WHERE is_active = TRUE")
        total_downloads = await conn.fetchval("SELECT COUNT(*) FROM analytics WHERE event_type = 'download'")
        pending_res = await conn.fetchval("SELECT COUNT(*) FROM pending_resources WHERE status = 'pending'")
        pending_reports = await conn.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'pending'")
        pending_anon = await conn.fetchval("SELECT COUNT(*) FROM anon_questions WHERE is_published = FALSE AND answer IS NULL")

    await update.message.reply_text(
        f"📊 *Dr. Crow — Live Stats*\n\n"
        f"👥 Total registered: {total_users}\n"
        f"✅ Active members: {active_members}\n"
        f"📁 Resources: {total_resources}\n"
        f"⬇️ Total downloads: {total_downloads}\n\n"
        f"⏳ *Pending:*\n"
        f"• Resources: {pending_res}\n"
        f"• Reports: {pending_reports}\n"
        f"• Anon questions: {pending_anon}",
        parse_mode=ParseMode.MARKDOWN
    )