"""handlers/exam.py v4 — event-free exam system"""

import io
import csv
import logging
import asyncio
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config.settings import settings
from middleware.membership import should_respond
from database import queries
from database.regpay_queries import get_setting

logger = logging.getLogger(__name__)

_S_STEP      = "exam_add_step"
_S_COURSE    = "exam_course_code"
_S_TITLE     = "exam_title"
_S_FILE_ID   = "exam_file_id"
_S_FILE_TYPE = "exam_file_type"

STEP_WAIT_COURSE = "wait_course"
STEP_WAIT_TITLE  = "wait_title"
STEP_WAIT_FILE   = "wait_file"
STEP_WAIT_CSV    = "wait_csv"

REQUIRED_COLS = {"date", "start_time", "end_time", "slot", "section", "room"}


def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id)


async def _batch_prefix() -> str:
    try:
        v = await get_setting("batch_prefix")
        return v.strip() if v else ""
    except Exception:
        return ""


def _fmt_time(t: str) -> str:
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            return datetime.strptime(t, fmt).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return t


def _fmt_date(d) -> str:
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%d/%m/%Y") + " (" + d.strftime("%A") + ")"


def _esc(t) -> str:
    t = str(t) if t is not None else ""
    for c in r"\_*[]()~`>#+-=|{}.!":
        t = t.replace(c, f"\\{c}")
    return t


def _parse_time_flexible(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time: {raw!r}")


def _parse_date_flexible(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}")


def _parse_csv(content: str, course_code: str) -> tuple:
    reader = csv.DictReader(io.StringIO(content.strip()))
    if not reader.fieldnames:
        return [], ["CSV is empty or missing header row."]

    headers = {h.strip().lower() for h in reader.fieldnames}
    missing = REQUIRED_COLS - headers
    if missing:
        return [], [f"Missing columns: {', '.join(sorted(missing))}"]

    rows_raw, errors = [], []
    for i, row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            exam_date = _parse_date_flexible(row["date"])
        except ValueError as e:
            errors.append(f"Row {i}: {e}")
            continue
        try:
            start_time = _parse_time_flexible(row["start_time"])
            end_time   = _parse_time_flexible(row["end_time"])
        except ValueError as e:
            errors.append(f"Row {i}: {e}")
            continue

        section = row.get("section", "").upper().strip()
        if "_" in section:
            section = section.split("_", 1)[1]

        rows_raw.append({
            "exam_date":  exam_date,
            "start_time": start_time,
            "end_time":   end_time,
            "slot":       row.get("slot", "").upper().strip(),
            "section":    section,
            "room":       row.get("room", "").strip(),
            "seats":      int(row["seats"]) if (row.get("seats") or "").isdigit() else None,
            "teacher":    row.get("teacher", "").strip() or None,
        })

    if not rows_raw and not errors:
        return [], ["CSV has no data rows."]

    grouped: dict = {}
    for r in rows_raw:
        key = (r["exam_date"], r["start_time"], r["end_time"], r["slot"])
        if key not in grouped:
            grouped[key] = {
                "course_code": course_code.upper(),
                "exam_date":   r["exam_date"],
                "start_time":  r["start_time"],
                "end_time":    r["end_time"],
                "slot_label":  r["slot"],
                "sections":    [],
            }
        grouped[key]["sections"].append({
            "section": r["section"],
            "room":    r["room"],
            "seats":   r["seats"],
            "teacher": r["teacher"],
        })

    return list(grouped.values()), errors


def _exam_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add", callback_data="exam_adm_add"),
         InlineKeyboardButton("List / Delete", callback_data="exam_adm_list")],
        [InlineKeyboardButton("Close", callback_data="exam_adm_close")],
    ])


async def handle_exam_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "*Exam Management*\nChoose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_exam_inline_kb(),
    )


async def handle_exam_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data
    if not _is_admin(update.effective_user.id):
        return

    if data == "exam_adm_add":
        _clear_state(context)
        context.user_data[_S_STEP] = STEP_WAIT_COURSE
        await query.edit_message_text(
            "*Add Exam — Step 1/3*\n\nSend the *course code*.\nExample: `CSE315`\n\n/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "exam_adm_list":
        await _admin_list_schedules(query, context)
    elif data == "exam_adm_close":
        await query.delete_message()
    elif data.startswith("exam_del_"):
        await _admin_delete_schedule(query, context, int(data.split("_")[-1]))
    elif data == "exam_adm_back":
        await query.edit_message_text(
            "*Exam Management*\nChoose an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_exam_inline_kb(),
        )


async def _admin_list_schedules(query, context) -> None:
    schedules = await queries.get_all_exam_schedules()
    if not schedules:
        await query.edit_message_text(
            "No exam schedules yet.\nTap *Add* to create one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_exam_inline_kb(),
        )
        return

    text    = "*Exam Schedules*\nTap a number to delete:\n\n"
    buttons = []
    row_buf = []

    for i, s in enumerate(schedules, start=1):
        status   = "✓" if s["is_active"] else "○"
        d        = s["exam_date"]
        date_str = d.strftime("%d %b") if hasattr(d, "strftime") else str(d)
        sec_cnt  = s["section_count"] or 0
        text    += f"`{i}.` {status} *{s['course_code']}* — {date_str}  ({sec_cnt} sec)\n"
        row_buf.append(InlineKeyboardButton(str(i), callback_data=f"exam_del_{s['id']}"))
        if len(row_buf) == 4:
            buttons.append(row_buf)
            row_buf = []

    if row_buf:
        buttons.append(row_buf)
    buttons.append([InlineKeyboardButton("← Back", callback_data="exam_adm_back")])

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _admin_delete_schedule(query, context, schedule_id: int) -> None:
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT course_code, exam_date FROM exam_schedule WHERE id = $1", schedule_id
        )
    if not row:
        await query.answer("Already deleted.", show_alert=True)
        await _admin_list_schedules(query, context)
        return
    await queries.delete_exam_schedule(schedule_id)
    await query.answer(f"Deleted {row['course_code']} {row['exam_date']}")
    await _admin_list_schedules(query, context)


async def handle_exam_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_admin(update.effective_user.id):
        return False
    step = context.user_data.get(_S_STEP)
    if not step:
        return False

    msg = update.message

    if msg.text and msg.text.strip().lower() == "/cancel":
        _clear_state(context)
        await msg.reply_text("Cancelled.")
        return True

    if step == STEP_WAIT_COURSE:
        if not msg.text or not msg.text.strip():
            await msg.reply_text("Send the course code as text. /cancel to abort.")
            return True
        course_code = msg.text.strip().upper()
        context.user_data[_S_COURSE] = course_code
        context.user_data[_S_STEP]   = STEP_WAIT_TITLE
        await msg.reply_text(
            f"Course: *{course_code}*\n\n"
            "*Step 2/4 — Exam Title*\n\n"
            "Send a short title for this exam.\n"
            "Example: `Mid Term` or `Final Exam`\n\n"
            "/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == STEP_WAIT_TITLE:
        if not msg.text or not msg.text.strip():
            await msg.reply_text("Send the exam title as text. /cancel to abort.")
            return True
        context.user_data[_S_TITLE] = msg.text.strip()
        context.user_data[_S_STEP]  = STEP_WAIT_FILE
        course_code = context.user_data.get(_S_COURSE, "")
        await msg.reply_text(
            f"Title: *{msg.text.strip()}*\n\n"
            "*Step 3/4 — Routine / Seat Plan PDF*\n\n"
            f"Send the PDF or photo for *{course_code}*.\n"
            "Or send /skip to proceed without a file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == STEP_WAIT_FILE:
        file_id, file_type = None, None
        if msg.text and msg.text.strip().lower() == "/skip":
            pass
        elif msg.photo:
            file_id, file_type = msg.photo[-1].file_id, "photo"
        elif msg.document:
            mime = (msg.document.mime_type or "").lower()
            if "pdf" in mime or (msg.document.file_name or "").lower().endswith(".pdf"):
                file_id, file_type = msg.document.file_id, "document"
            else:
                await msg.reply_text("Please send a PDF or photo. Or /skip.")
                return True
        else:
            await msg.reply_text("Please send a PDF, photo, or /skip.")
            return True

        context.user_data[_S_FILE_ID]   = file_id
        context.user_data[_S_FILE_TYPE]  = file_type
        context.user_data[_S_STEP]       = STEP_WAIT_CSV
        course_code = context.user_data.get(_S_COURSE, "")
        await msg.reply_text(
            f"{'File saved.' if file_id else 'Skipped.'}\n\n"
            "*Step 4/4 — Section CSV*\n\n"
            f"Send the `.csv` for *{course_code}*.\n\n"
            "*Required columns:*\n"
            "`date, start_time, end_time, slot, section, room`\n\n"
            "*Optional:* `seats, teacher`\n\n"
            "*Date formats:* `DD-MM-YYYY` or `YYYY-MM-DD`\n"
            "*Time formats:* `HH:MM` or `HH:MM AM/PM`\n\n"
            "*Example:*\n"
            "`26-04-2026,12:00 PM,02:00 PM,B,66_A,208,18,NRM`\n\n"
            "/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == STEP_WAIT_CSV:
        if not msg.document or not (msg.document.file_name or "").lower().endswith(".csv"):
            await msg.reply_text("Please send a .csv file. /cancel to abort.")
            return True

        processing = await msg.reply_text("Parsing CSV...")
        try:
            tg_file = await context.bot.get_file(msg.document.file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            csv_content = buf.getvalue().decode("utf-8-sig")
        except Exception as e:
            await processing.edit_text(f"Download failed: {e}")
            return True

        course_code = context.user_data.get(_S_COURSE, "")
        rows, errors = _parse_csv(csv_content, course_code)

        if errors:
            err_text = "CSV Errors:\n" + "\n".join(f"• {e}" for e in errors[:10])
            if not rows:
                err_text += "\n\nNo valid data. Fix and resend."
            await processing.edit_text(err_text)
            return True

        if not rows:
            await processing.edit_text("No valid data in CSV.")
            return True

        sem_courses = await queries.get_current_semester_courses()
        info        = sem_courses.get(course_code)
        course_name = info["name"] if info else None

        exam_title = context.user_data.get(_S_TITLE, "")

        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            sem_id = await conn.fetchval(
                "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
            )

        file_id   = context.user_data.get(_S_FILE_ID)
        file_type = context.user_data.get(_S_FILE_TYPE)

        try:
            for row in rows:
                await queries.insert_exam_schedule_single(
                    course_code      = row["course_code"],
                    course_name      = course_name,
                    exam_title       = exam_title,
                    exam_date        = row["exam_date"],
                    start_time       = row["start_time"],
                    end_time         = row["end_time"],
                    slot_label       = row["slot_label"],
                    routine_file_id  = file_id,
                    routine_file_type= file_type,
                    semester_id      = sem_id,
                    sections         = row["sections"],
                )
        except Exception as e:
            logger.error(f"Exam insert DB error: {e}")
            await processing.edit_text(f"Database error: {e}")
            return True

        unique_dates   = sorted({str(r["exam_date"]) for r in rows})
        total_sections = sum(len({s["section"] for s in r["sections"]}) for r in rows)
        name_str       = f" ({course_name})" if course_name else ""
        title_str      = f" — {exam_title}" if exam_title else ""

        await processing.edit_text(
            f"*Exam Added*\n\n"
            f"*{course_code}*{name_str}{title_str}\n"
            f"Date(s): {', '.join(unique_dates)}\n"
            f"Slots: {len(rows)}  |  Sections: {total_sections}\n"
            f"File: {'attached' if file_id else 'none'}\n\n"
            f"First exam activates 3 days before {unique_dates[0]}.",
            parse_mode=ParseMode.MARKDOWN,
        )
        _clear_state(context)
        return True

    return False


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in (_S_STEP, _S_COURSE, _S_TITLE, _S_FILE_ID, _S_FILE_TYPE):
        context.user_data.pop(k, None)


async def handle_exam_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await should_respond(update, context.bot):
        return
    from database.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT section FROM users WHERE user_id = $1", update.effective_user.id
        )
    section = user_row["section"] if user_row and user_row["section"] else None
    if not section:
        context.user_data["exam_waiting_section"] = True
        await update.message.reply_text(
            "To show your exam schedule I need your section.\n\n"
            "Send your section letter (A, B, C...):"
        )
        return
    await _send_exam_info(update.message, context, section)


async def handle_member_section_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("exam_waiting_section"):
        return False
    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip().upper()
    if "_" in text:
        text = text.split("_", 1)[1]
    if not text or len(text) > 2 or not text.isalpha():
        await update.message.reply_text("Send a single letter like A, B, or C.")
        return True

    exists = await queries.section_has_active_exam(text)
    if not exists:
        active = await queries.get_active_schedules()
        if not active:
            await update.message.reply_text(
                "No upcoming exam schedule found for the current semester."
            )
        else:
            await update.message.reply_text(
                f"Section *{text}* has no exam data in the current schedule.\n"
                "Please check your section letter or contact your CR.",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data.pop("exam_waiting_section", None)
        return True

    await queries.set_user_section(update.effective_user.id, text)
    context.user_data.pop("exam_waiting_section", None)
    await update.message.reply_text(f"Section *{text}* saved.", parse_mode=ParseMode.MARKDOWN)
    await _send_exam_info(update.message, context, text)
    return True


async def _send_exam_info(message: Message, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    raw = await queries.get_exam_info_for_section(section)
    if not raw:
        await message.reply_text("No active exam schedule right now.")
        return

    has_section_data = any(r["room"] for r in raw)
    if not has_section_data:
        await message.reply_text(
            f"Section *{section}* has no exam data in the current schedule.\n"
            "Please check your section letter or contact your CR.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    schedules: dict = {}
    for r in raw:
        sid = r["schedule_id"]
        if sid not in schedules:
            schedules[sid] = {
                "course_code":       r["course_code"],
                "course_name":       r["course_name"],
                "exam_title":        r["exam_title"] or "",
                "exam_date":         r["exam_date"],
                "start_time":        r["start_time"],
                "end_time":          r["end_time"],
                "slot_label":        r["slot_label"] or "",
                "routine_file_id":   r["routine_file_id"],
                "routine_file_type": r["routine_file_type"],
                "teacher":           r["teacher"],
                "rooms":             [],
            }
        if r["room"]:
            schedules[sid]["rooms"].append({"room": r["room"], "seats": r["seats"]})

    sched_list = list(schedules.values())
    sem_name   = await queries.get_current_semester_name()
    batch_pfx  = await _batch_prefix()
    section_disp = f"{batch_pfx}\\_{section.upper()}" if batch_pfx else section.upper()

    # Header: "Summer 2026 — Mid Term" from first schedule's exam_title
    first_title  = sched_list[0]["exam_title"]
    if sem_name and first_title:
        header_title = f"{sem_name} — {first_title}"
    elif sem_name:
        header_title = sem_name
    else:
        header_title = first_title or "Exam Schedule"

    lines = [
        f"*{_esc(header_title)}*",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"*Section {_esc(batch_pfx + '_' + section.upper() if batch_pfx else section.upper())}*",
        "",
    ]

    sent_file_ids = set()

    # Header block — sent once as part of first caption or standalone
    header_block = "\n".join(lines)

    for idx, s in enumerate(sched_list):
        course  = s["course_name"] or s["course_code"]
        teacher = s["teacher"] or ""
        slot    = s["slot_label"]
        t_start = _fmt_time(s["start_time"]) if s["start_time"] else "?"
        t_end   = _fmt_time(s["end_time"])   if s["end_time"]   else "?"

        d = s["exam_date"]
        if isinstance(d, str):
            from datetime import datetime as _dt
            d = _dt.strptime(d, "%Y-%m-%d").date()
        date_str    = d.strftime("%d/%m/%Y")
        weekday_str = d.strftime("%A")

        teacher_part = f"  ·  *{_esc(teacher)}*" if teacher else ""

        course_lines = [
            "",
            f"*{_esc(date_str)}*  ·  *{_esc(weekday_str)}*",
            f"*{_esc(t_start)}  —  {_esc(t_end)}*  ·  *Slot {_esc(slot)}*",
            f"*{_esc(s['course_code'])}*  —  *{_esc(course)}*{teacher_part}",
        ]

        if s["rooms"]:
            course_lines.append("")
            max_room  = max(len(r["room"]) for r in s["rooms"])
            col_width = max(max_room + 4, 8)

            def _room_sort_key(r):
                import re
                parts = re.split(r'(\d+)', r["room"].upper())
                return [int(p) if p.isdigit() else p for p in parts]

            sorted_rooms = sorted(s["rooms"], key=_room_sort_key)

            header = "Room".ljust(col_width) + "Seats"
            sep    = "─" * (col_width + 5)
            course_lines.append(f"`{header}`")
            course_lines.append(f"`{sep}`")
            for r in sorted_rooms:
                room_pad  = r["room"].ljust(col_width)
                seats_str = str(r["seats"]).zfill(2) if r["seats"] else "—"
                course_lines.append(f"`{room_pad}{seats_str}`")

        course_lines.append("━━━━━━━━━━━━━━━━━━━━━━━")

        # Build caption: header only in first course
        if idx == 0:
            caption = header_block + "\n".join(course_lines)
        else:
            caption = "\n".join(course_lines).lstrip("\n")

        fid = s.get("routine_file_id")
        if fid and fid not in sent_file_ids:
            sent_file_ids.add(fid)
            # Truncate caption if over Telegram's 1024 char limit
            if len(caption) > 1024:
                caption = caption[:1020] + "…"
            try:
                if s["routine_file_type"] == "photo":
                    await context.bot.send_photo(
                        message.chat_id, fid,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                else:
                    await context.bot.send_document(
                        message.chat_id, fid,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
            except Exception as e:
                logger.warning(f"Failed to send file for {s['course_code']}: {e}")
                # Fallback: send text separately
                await message.reply_text(caption, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            # No file — send as plain text message
            await message.reply_text(caption, parse_mode=ParseMode.MARKDOWN_V2)


async def run_exam_scheduler(bot) -> None:
    """Send morning exam notifications — no activation/deactivation needed."""
    import datetime as dt

    now_bdt    = dt.datetime.utcnow() + dt.timedelta(hours=6)
    is_morning = 6 <= now_bdt.hour <= 8
    if not is_morning:
        return

    today        = now_bdt.date()
    sem_name     = await queries.get_current_semester_name()
    batch_pfx    = await _batch_prefix()
    active       = await queries.get_active_schedules()
    today_scheds = [x for x in active if x["exam_date"] == today or str(x["exam_date"]) == str(today)]

    for s in today_scheds:
        users = await queries.get_users_with_section_for_notification(s["id"])
        if not users:
            continue

        course_name = s.get("course_name") or s["course_code"]
        t_start     = _fmt_time(s["start_time"]) if s.get("start_time") else "?"
        t_end       = _fmt_time(s["end_time"])   if s.get("end_time")   else "?"
        slot        = s.get("slot_label") or ""
        header      = sem_name if sem_name else "Exam Today"

        for u in users:
            try:
                section      = u["section"].upper()
                section_disp = f"{batch_pfx}_{section}" if batch_pfx else section
                teacher      = u.get("teacher") or ""
                room         = u.get("room") or ""
                seats        = u.get("seats")
                room_line    = f"\n`{room}{'  ' + str(seats) if seats else ''}`" if room else ""
                teacher_line = f"\n_{teacher}_" if teacher else ""
                text = (
                    f"*{header}*\n"
                    f"*Section {section_disp}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*{course_name}*  |  `{s['course_code']}`{teacher_line}\n"
                    f"`{_fmt_date(today)}`\n"
                    f"`{t_start} -- {t_end}`  |  Slot {slot}"
                    f"{room_line}\n\n"
                    f"_Good luck!_"
                )
                await bot.send_message(u["user_id"], text, parse_mode=ParseMode.MARKDOWN)
                await queries.mark_exam_notified(s["id"], u["user_id"])
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Notify failed user {u['user_id']}: {e}")