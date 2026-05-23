"""
Admin handler — full control panel for admins only.
Admin IDs come from env var, never from DB.
"""

import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config.settings import settings
from middleware.membership import should_respond
from database import queries

logger = logging.getLogger(__name__)


def admin_only(func):
    """Decorator: reject non-admins silently."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not settings.is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer("🚫 Admin only.", show_alert=True)
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Dr. Crow — Admin Panel*\n\nWhat would you like to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_admin_main_keyboard()
    )



_CMD_REFS = {
    "upload": (
        "📤 *Upload Commands*\n\n"
        "*Resources:*\n"
        "`/addbook` — Add a book\n"
        "`/addnote` — Add a note\n"
        "`/addsolve` — Add a solution\n"
        "`/addpsq` — Add past questions\n"
        "`/addvidoc` — Add video/doc\n"
        "`/addutil` — Add a utility\n"
        "`/addwaiver` — Add waiver info\n"
        "`/addregpay` — Add reg/payment info\n"
        "`/addsolution` — Add solution manual\n\n"
        "*Exam:*\n"
        "`/addexam <uid> | <name> | <CODE TITLE> | <YYYY-MM-DD HH:MM> | <show from> | <note>`\n"
        "Example: `/addexam mt-cse315 | Mid Term | CSE315 SWE | 2026-06-15 10:00 | 2026-06-10 00:00 | Focus ch 3-5`\n\n"
        "Each command starts a step-by-step conversation."
    ),
    "edit": (
        "✏️ *Edit Commands*\n\n"
        "*Resources:*\n"
        "`/editbook <uid>` — Edit book fields\n"
        "`/editnote <uid>` — Edit note fields\n"
        "`/editsolve <uid>` — Edit solve fields\n"
        "`/editpsq <uid>` — Edit PSQ fields\n"
        "`/editvidoc <uid>` — Edit video/doc\n"
        "`/editutil <uid>` — Edit utility\n"
        "`/editwaiver <uid>` — Edit waiver\n"
        "`/editregpay <uid>` — Edit reg/payment\n\n"
        "*Exam:*\n"
        "`/editexam <uid> | <field> | <new value>`\n"
        "Fields: name, date, time, show-from, note\n\n"
        "Replace `<uid>` with the resource UID."
    ),
    "delete": (
        "🗑 *Delete Commands*\n\n"
        "*Resources:*\n"
        "`/deletebook <uid>`\n"
        "`/deletenote <uid>`\n"
        "`/deletesolve <uid>`\n"
        "`/deletepsq <uid>`\n"
        "`/deletevidoc <uid>`\n"
        "`/deleteutil <uid>`\n"
        "`/deletewaiver <uid>`\n"
        "`/deleteregpay <uid>`\n\n"
        "*Exam:*\n"
        "`/deleteexam <uid>`\n\n"
        "Permanent — cannot be undone."
    ),
    "list": (
        "📋 *List Commands*\n\n"
        "*Resources:*\n"
        "`/listbooks` · `/listnotes` · `/listsolves`\n"
        "`/listpsqs` · `/listvidocs` · `/listutils`\n"
        "`/listwaivers` · `/listregpays` · `/listsolutions`\n"
        "`/listcals` · `/listadvisors` · `/listfees`\n"
        "`/listsyllabuses` · `/listoutlines` · `/listroutines`\n\n"
        "*Exam:*\n"
        "`/listexams` — All exam events with status"
    ),
    "semester": (
        "🗂 *Semester Commands*\n\n"
        "`/semester` — List all semesters\n"
        "`/semester <uid> <name> | CSE315 SWE, CSE317 MM` — Create new\n"
        "`/semester #<uid>` — Restore a previous semester\n"
        "`/semester rename <uid> <new name>` — Rename\n"
        "`/semester kill <uid>` — Delete semester and all its resources\n\n"
        "Example: `/semester sp26 Spring 2026 | CSE315 SWE, CSE317 MM`"
    ),
}


def _admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload",    callback_data="admin_ref_upload"),
            InlineKeyboardButton("✏️ Edit",      callback_data="admin_ref_edit"),
        ],
        [
            InlineKeyboardButton("🗑 Delete",    callback_data="admin_ref_delete"),
            InlineKeyboardButton("📋 List",      callback_data="admin_ref_list"),
        ],
        [
            InlineKeyboardButton("🔍 Find & Edit", callback_data="admin_find_resource"),
        ],
        [
            InlineKeyboardButton("🗂 Semester",  callback_data="admin_ref_semester"),
            InlineKeyboardButton("📊 Overview",  callback_data="admin_analytics"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("✖ Close",      callback_data="admin_close"),
        ],
    ])


@admin_only
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    await query.answer()

    if data == "admin_close":
        await query.message.delete()

    elif data.startswith("admin_ref_"):
        key  = data[len("admin_ref_"):]
        text = _CMD_REFS.get(key, "No reference available.")
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif data == "admin_broadcast":
        pass  # Handled by broadcast_conversation ConversationHandler

    elif data == "admin_exam_mode":
        await _show_exam_mode(update, context)

    elif data.startswith("admin_exam_toggle_"):
        event_id = int(data.split("_")[-1])
        await queries.deactivate_exam_event(event_id)
        await query.edit_message_text("Exam event deactivated.")


    elif data == "admin_find_resource":
        await query.answer()
        await query.message.reply_text(
            "🔍 <b>Find & Edit Resource</b>\n\n"
            "Type <code>/find</code> to search and manage any resource by title, course code, tags, or UID.\n\n"
            "<i>Supports: Book, Note, Solve, PSQ, Vidoc, Syllabus, Utility, Waiver, RegPay, Cal, Advisor, Fee</i>",
            parse_mode="HTML"
        )

    elif data == "admin_semesters":
        await _show_semesters(update, context)

    elif data.startswith("admin_deactivate_res_"):
        resource_id = int(data.split("_")[-1])
        await queries.deactivate_resource(resource_id)
        await query.edit_message_text("Resource deactivated.")

    elif data == "admin_back":
        await query.edit_message_text(
            "*Dr. Crow — Admin Panel*\n\nWhat would you like to do?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_admin_main_keyboard()
        )

async def _show_pending_resources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pending = await queries.get_pending_resources()

    if not pending:
        await query.edit_message_text(
            "✅ No pending resources."
        )
        return

    for p in pending[:5]:  # Show 5 at a time
        tags_raw = p["tags"]
        import json
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
        tag_str = ", ".join(tags) or "none"

        await update.effective_message.reply_text(
            f"📬 *Pending Resource #{p['id']}*\n\n"
            f"From: {p['full_name']} (@{p['username']})\n"
            f"Title: *{p['title']}*\n"
            f"Course: `{p['course_code'] or 'N/A'}`\n"
            f"Category: `{p['category'] or 'N/A'}`\n"
            f"Tags: {tag_str}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{p['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{p['id']}"),
            ]])
        )


async def _approve_resource(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_id: int):
    query = update.callback_query
    admin_id = update.effective_user.id
    pending = await queries.get_pending_resource(pending_id)

    if not pending:
        await query.edit_message_text("❌ Pending resource not found.")
        return

    import json
    tags = json.loads(pending["tags"]) if isinstance(pending["tags"], str) else pending["tags"]
    semester = await queries.get_current_semester()
    semester_id = semester["id"] if semester else None

    resource_id = await queries.insert_resource(
        pending["title"], pending["file_id"], pending["file_type"],
        pending["course_code"], pending["category"], tags,
        semester_id, pending["submitted_by"], admin_id
    )
    await queries.insert_search_index(resource_id, pending["title"])
    await queries.update_pending_status(pending_id, "approved", admin_id)
    # Upload star for approved pending resource (category-based)
    _upload_stars = {
        "book": 15, "solution_manual": 12, "solve": 10, "note": 8,
        "psq": 6, "vidoc": 6, "syllabus": 4, "outline": 4,
        "routine": 3, "cal": 3, "advisor": 3, "fee": 3, "utility": 2,
        "waiver": 2, "regpay": 2,
    }
    _cat = (pending["category"] or "").lower()
    _star_val = _upload_stars.get(_cat, 3)
    await queries.add_stars(pending["submitted_by"], _star_val, f"upload_approved:{_cat}")

    # Notify submitter
    try:
        await context.bot.send_message(
            pending["submitted_by"],
            f"Your resource <b>{pending['title']}</b> has been approved. You earned +{_star_val} ⭐",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    await query.edit_message_text(
        f"Approved. Resource ID: {resource_id} · Submitter +{_star_val} ⭐",
        parse_mode=ParseMode.HTML
    )


async def _reject_resource(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_id: int):
    query = update.callback_query
    admin_id = update.effective_user.id
    pending = await queries.get_pending_resource(pending_id)

    if not pending:
        await query.edit_message_text("❌ Not found.")
        return

    await queries.update_pending_status(pending_id, "rejected", admin_id)

    try:
        await context.bot.send_message(
            pending["submitted_by"],
            f"😔 Your resource *{pending['title']}* was not approved.\n"
            f"Don't give up — keep contributing!",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass

    await query.edit_message_text("🗑 Resource rejected.", parse_mode=ParseMode.MARKDOWN)


async def _show_pending_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reports = await queries.get_pending_reports()
    if not reports:
        await update.effective_message.reply_text(
            "✅ No pending reports."
        )
        return

    for r in reports[:5]:
        await update.effective_message.reply_text(
            f"🚩 *Report #{r['id']}*\n\n"
            f"Resource: *{r['resource_title']}* (ID: {r['resource_id']})\n"
            f"Reporter: {r['full_name']}\n"
            f"Reason: {r['reason']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept (remove resource)", callback_data=f"admin_report_accept_{r['id']}"),
                InlineKeyboardButton("❌ Reject report", callback_data=f"admin_report_reject_{r['id']}"),
            ]])
        )


async def _accept_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: int):
    query = update.callback_query
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        report = await conn.fetchrow("SELECT * FROM reports WHERE id = $1", report_id)

    if not report:
        await query.edit_message_text("❌ Report not found.")
        return

    await queries.update_report_status(report_id, "accepted", update.effective_user.id)
    await queries.deactivate_resource(report["resource_id"])
    await queries.add_stars(report["reporter_id"], 3, "valid_report")
    # Deduct stars from uploader if resource has one
    from database.db import get_pool as _gp
    _pool = await _gp()
    async with _pool.acquire() as _conn:
        _uploader = await _conn.fetchval(
            "SELECT uploaded_by FROM resources WHERE id = $1", report["resource_id"]
        )
    if _uploader:
        await queries.add_stars(_uploader, -5, "report_accepted")
    await query.edit_message_text("Report accepted. Resource removed. Reporter +3 ⭐, uploader -5 ⭐.")


async def _reject_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: int):
    query = update.callback_query
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        report = await conn.fetchrow("SELECT * FROM reports WHERE id = $1", report_id)

    if not report:
        await query.edit_message_text("❌ Not found.")
        return

    await queries.update_report_status(report_id, "rejected", update.effective_user.id)

    false_count = await queries.count_false_reports(report["reporter_id"])
    if false_count >= 3:
        await queries.add_flag(report["reporter_id"], "false_report", "3 false reports", update.effective_user.id)
        await queries.add_stars(report["reporter_id"], -2, "false_report_flag")
        try:
            await context.bot.send_message(
                report["reporter_id"],
                "⚠️ Your report was rejected. Repeated false reports may result in penalties."
            )
        except Exception:
            pass

    await query.edit_message_text("❌ Report rejected.")


async def _show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Overview: row counts + live user stats + weekly stats + leaderboard."""
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        members     = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_member = TRUE")
        subs        = await conn.fetchval("SELECT COUNT(*) FROM subscriptions")

        row_counts  = {}
        for tbl in ("books", "notes", "solves", "psqs", "vidocs", "utilities", "waivers", "regpay"):
            row_counts[tbl] = await conn.fetchval(f"SELECT COUNT(*) FROM {tbl}")
        row_counts["resources"] = await conn.fetchval(
            "SELECT COUNT(*) FROM resources WHERE is_active = TRUE"
        )

    stats = await queries.get_weekly_stats()
    leaderboard = await queries.get_leaderboard(5)
    lb_lines = "\n".join(
        f"{i}. {u['full_name'] or u['username'] or 'Unknown'} — {u['stars'] or 0:.1f} ⭐"
        for i, u in enumerate(leaderboard, 1)
    ) or "No data yet"

    text = (
        "<b>Overview</b>\n\n"
        "<b>Members</b>\n"
        f"<code>"
        f"Total    : {total_users}\n"
        f"Active   : {members}\n"
        f"Subs     : {subs}"
        f"</code>\n\n"
        "<b>Content</b>\n"
        f"<code>"
        f"Books    : {row_counts['books']}\n"
        f"Notes    : {row_counts['notes']}\n"
        f"Solves   : {row_counts['solves']}\n"
        f"PSQs     : {row_counts['psqs']}\n"
        f"Vidocs   : {row_counts['vidocs']}\n"
        f"Utils    : {row_counts['utilities']}\n"
        f"Waivers  : {row_counts['waivers']}\n"
        f"RegPay   : {row_counts['regpay']}"
        f"</code>\n\n"
        "<b>This Week</b>\n"
        f"<code>"
        f"Downloads: {stats['downloads']}\n"
        f"Uploads  : {stats['uploads']}\n"
        f"Active   : {stats['active_users']}"
        f"</code>\n\n"
        f"<b>Top 5</b>\n<code>{lb_lines}</code>"
    )

    await update.effective_message.reply_text(text, parse_mode="HTML")




async def show_analytics_text(update, context):
    """Public wrapper for _show_analytics — called from Reply KB Overview button."""
    await _show_analytics(update, context)

async def _show_anon_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    questions = await queries.get_pending_anon_questions()
    if not questions:
        await update.effective_message.reply_text(
            "No pending anonymous questions."
        )
        return
    for q in questions[:5]:
        await update.effective_message.reply_text(
            f"*Anonymous Question #{q['id']}*\n\n{q['question']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Answer", callback_data=f"admin_anon_answer_{q['id']}"),
            ]])
        )


async def _show_exam_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active exam events with deactivate option (admin view of exam mode)."""
    from datetime import date
    events = await queries.get_active_exam_events()
    today  = date.today()

    if not events:
        await update.effective_message.reply_text(
            "*Exam Mode — No Active Events*\n\n"
            "Create one with:\n"
            "`/addexam <name> | <YYYY-MM-DD> | CSE315,CSE317`\n\n"
            "_Example:_\n"
            "`/addexam Mid Term | 2026-06-15 | CSE315,CSE317,CSE321`\n\n"
            "Once created:\n"
            "• Bot alerts all members at T-3 and T-1 days automatically\n"
            "• Members can tap *Exam Mode* in /start to get course bundles\n"
            "• Use */remind <event\\_id>* to blast manually anytime",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back", callback_data="admin_page_2")
            ]])
        )
        return

    for e in events:
        days_left = (e["exam_date"] - today).days
        countdown = f"{days_left} days left" if days_left > 0 else ("Today!" if days_left == 0 else "Past")
        import json
        courses = json.loads(e["course_codes"]) if isinstance(e["course_codes"], str) else e["course_codes"]
        course_str = ", ".join(courses) if courses else "—"
        await update.effective_message.reply_text(
            f"*{e['name']}* (ID: `{e['id']}`)\n"
            f"📅 {e['exam_date']} — _{countdown}_\n"
            f"📚 {course_str}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔴 Deactivate", callback_data=f"admin_exam_toggle_{e['id']}"),
            ]])
        )


async def _show_manage_exams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show exam management guide."""
    await update.effective_message.reply_text(
        "*Manage Exam Events*\n\n"
        "*Create:*\n"
        "`/addexam <name> | <YYYY-MM-DD> | CSE315,CSE317`\n\n"
        "_Example:_\n"
        "`/addexam Mid Term | 2026-06-15 | CSE315,CSE317,CSE321`\n\n"
        "*How it works:*\n"
        "• Bot auto-alerts all members at T-3 and T-1 days\n"
        "• Members see course bundle buttons in /start → Exam Mode\n"
        "• Each bundle sends all resources for that course\n\n"
        "*Manual blast:*\n"
        "`/remind <event_id>` — triggers alert immediately\n"
        "_Get event IDs from_ *Exam Mode* _button above_\n\n"
        "*Deactivate:*\n"
        "Go to *Exam Mode* → tap 🔴 Deactivate on any event",
        parse_mode=ParseMode.MARKDOWN
    )

async def _show_member_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        members = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_member = TRUE")

    await update.effective_message.reply_text(
        f"<b>Member Statistics</b>\n\n"
        f"<code>Total    : {total}\n"
        f"Active   : {members}</code>",
        parse_mode="HTML",
    )


async def _show_semesters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all semesters with per-semester row counts for every resource table."""
    import json
    from database.queries import get_all_semesters, get_semester_resource_counts

    TABLES = [
        ("books",     "📚"),
        ("notes",     "📝"),
        ("solves",    "✅"),
        ("psqs",      "📋"),
        ("vidocs",    "🎥"),
        ("utilities", "🔧"),
        ("waivers",   "💸"),
        ("regpay",    "🧾"),
    ]

    semesters = await get_all_semesters()
    if not semesters:
        await update.effective_message.reply_text(
            "No semesters found.\nCreate: `/semester <uid> <name> | courses`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = ["🗂 *All Semesters*\n"]
    for sem in semesters:
        counts  = await get_semester_resource_counts(sem["id"])
        marker  = "▶️ *CURRENT*" if sem["is_current"] else f"`{sem['uid']}`"
        created = sem["created_at"].strftime("%Y-%m-%d")

        courses_raw = sem["courses"] or "[]"
        if isinstance(courses_raw, str):
            try: courses_raw = json.loads(courses_raw)
            except: courses_raw = []
        course_str = ", ".join(
            f"{c['code']}({c['title']})" if c.get("title") else c["code"]
            for c in courses_raw
        ) if courses_raw else "_No courses_"

        count_parts = "  ".join(
            f"{emoji}`{counts.get(tbl, 0)}`"
            for tbl, emoji in TABLES
        )

        lines.append(
            f"{'━' * 26}\n"
            f"{marker} — *{sem['name']}*\n"
            f"  📅 {created}\n"
            f"  📚 {course_str}\n"
            f"  {count_parts}\n"
        )

    lines.append(f"{'━' * 26}")
    lines.append(
        "\n_📚Books 📝Notes ✅Solves 📋PSQs 🎥Vidocs 🔧Utils 💸Waivers 🧾RegPay_\n"
        "_`/semester #<uid>` restore · `/semester rename <uid> name` · `/semester kill <uid>`_"
    )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN
    )