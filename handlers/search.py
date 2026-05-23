"""
Search handler — inline queries, DM fallback, report flow.
"""

import logging
import json
import asyncio
from telegram import (
    InlineQueryResultsButton,
    Update, InlineQueryResultArticle,
    InputTextMessageContent, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from middleware.membership import should_respond, check_membership
from services.search import search, search_fast, format_resource_caption
from handlers.book import deliver_book
from handlers.note import deliver_note
from handlers.psq import deliver_psq
from handlers.solve import deliver_solve
from handlers.vidoc import deliver_vidoc
from handlers.utility import deliver_utility, CATEGORIES as UTIL_CATS
from handlers.waiver import deliver_waiver
from handlers.regpay import deliver_regpay, show_profile, send_help
from handlers.upload import handle_admin_pending_message, handle_report_pending_message, admin_report_reject_message
from database import queries

logger = logging.getLogger(__name__)

REPORT_RESOURCE_ID, REPORT_REASON = range(2)

CAT_LABELS = {
    "book": "Books", "note": "Notes", "question": "Past Questions",
    "solution": "Solutions", "video": "Videos & Docs", "utility": "Utilities", "slide": "Slides",
    "syllabus": "Syllabus", "outline": "Course Outlines",
    "routine": "Exam Routines", "calendar": "Academic Calendar",
    "advisor": "Advisor Info", "fee": "Fee Overview",
    "registration": "Registration Info", "payment": "Payment Info",
    "waiver": "Waiverulator",
}


# ── Pick-mode helpers: type-specific fetch and search ────────────────────────

_UTILITY_CAT_MAP = {
    "syllabus": "syllabus",
    "outline":  "outline",
    "routine":  "routine",
    "util":     "util_misc",
    "slide":    "slides",
    "cal":      "cal",
    "advisor":  "advisor",
}

# Maps rtype → (_type label used in result dicts)
_RTYPE_LABEL = {
    "book": "book", "note": "note", "solve": "solve",
    "psq": "psq", "vidoc": "vidoc", "waiver": "waiver",
    "regpay": "regpay",
    "syllabus": "utility", "outline": "utility", "routine": "utility",
    "util": "utility", "slide": "utility", "cal": "utility", "advisor": "utility",
}


async def _fetch_recent_by_type(rtype: str, limit: int = 10) -> list:
    """Fetch most recent records of a specific type — used in pick mode empty query."""
    label = _RTYPE_LABEL.get(rtype, "resource")
    try:
        if rtype == "book":
            from database.book_queries import get_books_paginated
            rows = await get_books_paginated(offset=0, limit=limit)
        elif rtype == "note":
            from database.note_queries import get_notes_paginated
            rows = await get_notes_paginated(offset=0, limit=limit)
        elif rtype == "solve":
            from database.solve_queries import get_solves_paginated
            rows = await get_solves_paginated(offset=0, limit=limit)
        elif rtype == "psq":
            from database.psq_queries import get_psqs_paginated
            rows = await get_psqs_paginated(offset=0, limit=limit)
        elif rtype == "vidoc":
            from database.vidoc_queries import get_vidocs_paginated
            rows = await get_vidocs_paginated(offset=0, limit=limit)
        elif rtype == "waiver":
            from database.waiver_queries import get_waivers_paginated
            rows = await get_waivers_paginated(offset=0, limit=limit)
        elif rtype == "regpay":
            from database.regpay_queries import get_regpay_paginated
            rows = await get_regpay_paginated(offset=0, limit=limit)
        elif rtype in _UTILITY_CAT_MAP:
            from database.utility_queries import get_utilities_by_category
            cat  = _UTILITY_CAT_MAP[rtype]
            rows = await get_utilities_by_category(cat, offset=0, limit=limit)
        else:
            return []
        return [dict(r) | {"_type": label} for r in rows]
    except Exception as e:
        logger.warning("_fetch_recent_by_type(%s) failed: %s", rtype, e)
        return []


async def _search_by_type(rtype: str, query: str, limit: int = 10) -> list:
    """Search within a specific type — used in pick mode with query text."""
    label = _RTYPE_LABEL.get(rtype, "resource")
    try:
        if rtype == "book":
            from database.book_queries import search_books_current_semester
            rows = await search_books_current_semester(query, limit=limit)
        elif rtype == "note":
            from database.note_queries import search_notes_current_semester
            rows = await search_notes_current_semester(query, limit=limit)
        elif rtype == "solve":
            from database.solve_queries import search_solves_current_semester
            rows = await search_solves_current_semester(query, limit=limit)
        elif rtype == "psq":
            from database.psq_queries import search_psqs
            rows = await search_psqs(query, limit=limit)
        elif rtype == "vidoc":
            from database.vidoc_queries import search_vidocs
            rows = await search_vidocs(query, limit=limit)
        elif rtype == "waiver":
            from database.waiver_queries import search_waivers
            rows = await search_waivers(query, limit=limit)
        elif rtype == "regpay":
            from database.regpay_queries import search_regpay
            rows = await search_regpay(query, limit=limit)
        elif rtype in _UTILITY_CAT_MAP:
            from database.utility_queries import search_utilities
            cat  = _UTILITY_CAT_MAP[rtype]
            rows = await search_utilities(query, category=cat, limit=limit)
        else:
            return []
        return [dict(r) | {"_type": label} for r in rows]
    except Exception as e:
        logger.warning("_search_by_type(%s, %r) failed: %s", rtype, query, e)
        return []



async def _deliver_resource_by_uid(bot, user_id: int, uid: str) -> bool:
    """
    Find a resource by UID across all types and deliver it to user_id.
    Returns True if delivered, False if not found.
    """
    from telegram.constants import ParseMode

    # Try each type — order matters (most common first)
    finders = [
        ("note",     _get_note_for_delivery),
        ("book",     _get_book_for_delivery),
        ("solve",    _get_solve_for_delivery),
        ("psq",      _get_psq_for_delivery),
        ("vidoc",    _get_vidoc_for_delivery),
        ("utility",  _get_utility_for_delivery),
        ("waiver",   _get_waiver_for_delivery),
        ("regpay",   _get_regpay_for_delivery),
    ]

    for rtype, finder in finders:
        try:
            result = await finder(bot, user_id, uid)
            if result:
                return True
        except Exception as e:
            logger.debug(f"_deliver_resource_by_uid: {rtype} lookup failed: {e}")

    return False


async def _send_file(bot, user_id: int, file_id: str, file_type: str, caption: str = ""):
    """Send a file using the correct Telegram method based on file_type."""
    kwargs = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    ft = (file_type or "document").lower()
    try:
        if ft in ("photo", "image"):
            await bot.send_photo(user_id, file_id, **kwargs)
        elif ft == "video":
            await bot.send_video(user_id, file_id, **kwargs)
        elif ft == "audio":
            await bot.send_audio(user_id, file_id, **kwargs)
        elif ft == "voice":
            await bot.send_voice(user_id, file_id)
        elif ft == "video_note":
            await bot.send_video_note(user_id, file_id)
        else:
            # pdf, docx, pptx, excel, document, etc.
            await bot.send_document(user_id, file_id, **kwargs)
    except Exception:
        # Fallback: try send_document
        await bot.send_document(user_id, file_id, **kwargs)


async def _get_note_for_delivery(bot, user_id, uid):
    from database.note_queries import get_note
    from html import escape as _h
    rec = await get_note(uid)
    if not rec:
        return False
    caption = f"📝 <b>{_h(rec.get('title',''))}</b>"
    if rec.get('course_code'):
        caption += f"\n{_h(rec['course_code'])}"
    await _send_file(bot, user_id, rec["file_id"], rec.get("file_type", "document"), caption)
    return True


async def _get_book_for_delivery(bot, user_id, uid):
    from database.book_queries import get_book
    from html import escape as _h
    rec = await get_book(uid)
    if not rec:
        return False
    caption = f"📚 <b>{_h(rec.get('title',''))}</b>"
    if rec.get('authors'):
        caption += f"\n{_h(rec['authors'])}"
    await _send_file(bot, user_id, rec["file_id"], "document", caption)
    return True


async def _get_solve_for_delivery(bot, user_id, uid):
    from database.solve_queries import get_solve
    from html import escape as _h
    rec = await get_solve(uid)
    if not rec:
        return False
    caption = f"✅ <b>{_h(rec.get('title',''))}</b>"
    if rec.get('course_code'):
        caption += f"\n{_h(rec['course_code'])}"
    await _send_file(bot, user_id, rec["file_id"], rec.get("file_type", "document"), caption)
    return True


async def _get_psq_for_delivery(bot, user_id, uid):
    from database.psq_queries import get_psq
    from html import escape as _h
    rec = await get_psq(uid)
    if not rec:
        return False
    title = _h(rec.get('title') or 'Past Questions')
    await _send_file(bot, user_id, rec["file_id"], "document", f"📋 <b>{title}</b>")
    return True


async def _get_vidoc_for_delivery(bot, user_id, uid):
    import json as _j
    from database.vidoc_queries import get_vidoc
    rec = await get_vidoc(uid)
    if not rec:
        return False
    msgs = rec.get("messages") or []
    if isinstance(msgs, str):
        try:
            msgs = _j.loads(msgs)
        except Exception:
            msgs = []
    for m in msgs:
        try:
            if m.get("type") == "text":
                await bot.send_message(user_id, m.get("content", ""), parse_mode="HTML")
            else:
                await bot.send_document(user_id, m["file_id"])
        except Exception:
            pass
    return True


async def _get_utility_for_delivery(bot, user_id, uid):
    from database.utility_queries import get_utility
    from html import escape as _h
    rec = await get_utility(uid)
    if not rec:
        return False
    fid = rec.get("file_id")
    if fid:
        label = _h(rec.get('title') or rec.get('category',''))
        await _send_file(bot, user_id, fid, rec.get("file_type", "document"), f"📋 <b>{label}</b>")
        return True
    url = rec.get("url")
    if url:
        title = _h(rec.get('url_title') or 'Link')
        await bot.send_message(
            user_id,
            f'🔗 <a href="{url}">{title}</a>',
            parse_mode="HTML"
        )
        return True
    return False


async def _get_waiver_for_delivery(bot, user_id, uid):
    from database.waiver_queries import get_waiver
    from html import escape as _h
    rec = await get_waiver(uid)
    if not rec:
        return False
    fid = rec.get("file_id")
    if fid:
        sem = _h(rec.get('semester_name',''))
        await _send_file(bot, user_id, fid, rec.get("file_type", "document"), f"💸 <b>Waiver — {sem}</b>")
        return True
    return False


async def _get_regpay_for_delivery(bot, user_id, uid):
    import json as _j
    from database.regpay_queries import get_regpay
    rec = await get_regpay(uid)
    if not rec:
        return False
    fids = rec.get("file_ids") or []
    if isinstance(fids, str):
        try:
            fids = _j.loads(fids)
        except Exception:
            fids = []
    for fid in fids:
        try:
            await bot.send_document(user_id, fid)
        except Exception:
            pass
    return bool(fids)


async def inline_search(update, context):
    inline_query = update.inline_query
    user = inline_query.from_user

    allowed = await check_membership(context.bot, user.id)
    if not allowed:
        denial = InlineQueryResultArticle(
            id="denied",
            title=settings.INLINE_DENIAL_TITLE,
            description=settings.INLINE_DENIAL_DESCRIPTION,
            input_message_content=InputTextMessageContent(message_text=settings.DENIAL_MESSAGE)
        )
        await inline_query.answer([denial], cache_time=60, is_personal=True)
        return

    raw = inline_query.query
    query_text = raw.strip()

    # ── Edit / Delete mode: "edit:note ds algo" or "delete:book " ────────────
    _pick_action = None
    _pick_rtype  = None
    if ":" in query_text:
        _prefix, _rest = query_text.split(":", 1)
        if _prefix in ("edit", "delete"):
            _pick_action = _prefix
            _parts       = _rest.strip().split(None, 1)
            _pick_rtype  = _parts[0].lower() if _parts else ""
            query_text   = _parts[1].strip() if len(_parts) > 1 else ""
    # ─────────────────────────────────────────────────────────────────────────

    first_word = query_text.split()[0].lower() if query_text else ""
    category_hint = first_word if first_word in CAT_LABELS else None

    if not query_text:
        # ── Pick mode with empty query: show recent records of that type ────
        if _pick_action and _pick_rtype:
            results_list = await _fetch_recent_by_type(_pick_rtype, limit=10)
            source = "recent"
        else:
            # Normal mode: show top resources this week
            top = await queries.get_top_resources_this_week(6)
            if not top:
                await inline_query.answer(
                    [_make_tip_result()],
                    cache_time=10, is_personal=True
                )
                return
            results = [_make_inline_result(r, source="top") for r in top]
            await inline_query.answer(
                results,
                cache_time=30,
                is_personal=True,
                button=InlineQueryResultsButton(text="📚 Top Resources This Week", start_parameter="search")
            )
            return
    else:
        if _pick_action and _pick_rtype:
            # Pick mode with query: search only within that type
            results_list = await _search_by_type(_pick_rtype, query_text, limit=10)
            source = "exact"
        else:
            results_list, source = await search_fast(query_text)

    if not results_list:
        if _pick_action and _pick_rtype:
            no_result = _make_no_result(
                query_text or f"(no {_pick_rtype} found)",
                _pick_rtype.title()
            )
        else:
            cat_label = CAT_LABELS.get(category_hint, "") if category_hint else ""
            no_result = _make_no_result(query_text, cat_label)
        await inline_query.answer(
            [no_result],
            cache_time=10,
            is_personal=True,
            button=InlineQueryResultsButton(text="🔍 Dr. Crow Search", start_parameter="search")
        )
        return

    inline_results = []
    for r in results_list[:settings.MAX_INLINE_RESULTS]:
        rtype = r.get("_type", "resource")

        # In pick mode, rewrite uid in the dict so _make_* builds the correct ID
        if _pick_action and _pick_rtype and r.get("uid"):
            r = dict(r)  # shallow copy — don't mutate original
            r["_pick_prefix"] = f"{_pick_action}_{_pick_rtype}_"

        if rtype == "book":
            cover_url = r.get("cover_url") or ""
            res = _make_book_inline_result(r, cover_url=cover_url)
        elif rtype == "note":
            cover_url = r.get("cover_url") or ""
            res = _make_note_inline_result(r, cover_url=cover_url)
        elif rtype == "solve":
            cover_url = r.get("cover_url") or ""
            res = _make_solve_inline_result(r, cover_url=cover_url)
        elif rtype == "vidoc":
            thumb = r.get("thumbnail_url") or ""
            res = _make_vidoc_inline_result(r, thumbnail_url=thumb)
        elif rtype == "regpay":
            thumb = r.get("thumbnail_url") or ""
            res = _make_regpay_inline_result(r, thumbnail_url=thumb)
        elif rtype == "waiver":
            thumb = r.get("thumbnail_url") or ""
            res = _make_waiver_inline_result(r, thumbnail_url=thumb)
        elif rtype == "utility":
            res = _make_utility_inline_result(r)
        elif rtype == "psq":
            cover_url = r.get("cover_url") or ""
            res = _make_psq_inline_result(r, cover_url=cover_url)
        else:
            res = _make_inline_result(r, source=source)
        inline_results.append(res)

    cache = 30 if source == "exact" else 10
    book_count = sum(1 for r in results_list if r.get("_type") == "book")
    note_count = sum(1 for r in results_list if r.get("_type") == "note")
    psq_count   = sum(1 for r in results_list if r.get("_type") == "psq")
    solve_count = sum(1 for r in results_list if r.get("_type") == "solve")
    vidoc_count = sum(1 for r in results_list if r.get("_type") == "vidoc")
    util_count  = sum(1 for r in results_list if r.get("_type") == "utility")
    res_count  = len(results_list) - book_count - note_count - psq_count - solve_count - vidoc_count - util_count

    parts = []
    if book_count: parts.append(f"📚 {book_count} Book{'s' if book_count > 1 else ''}")
    if note_count: parts.append(f"📝 {note_count} Note{'s' if note_count > 1 else ''}")
    if psq_count:   parts.append(f"📋 {psq_count} PSQ{'s' if psq_count > 1 else ''}")
    if solve_count: parts.append(f"✅ {solve_count} Solve{'s' if solve_count > 1 else ''}")
    if vidoc_count: parts.append(f"🎬 {vidoc_count} Vid{'s' if vidoc_count > 1 else ''}")
    if util_count:  parts.append(f"📄 {util_count} Info")
    if res_count:  parts.append(f"📎 {res_count} Resource{'s' if res_count > 1 else ''}")
    header_text = " · ".join(parts) if parts else "Dr. Crow Search"

    await inline_query.answer(
        inline_results,
        cache_time=cache,
        is_personal=True,
        button=InlineQueryResultsButton(text=header_text, start_parameter="search")
    )


async def _fetch_book_for_sol(book_uid: str):
    """Fetch book record for solution manual flow."""
    try:
        from database.book_queries import get_book as _gb
        return await _gb(book_uid)
    except Exception:
        return None


async def chosen_inline_result_handler(update, context):
    result    = update.chosen_inline_result
    user      = result.from_user
    result_id = result.result_id

    if result_id in ("denied", "no_result", "tip"):
        return

    # If user is in report flow, store the result text and skip delivery
    # If user is in report flow — don't deliver, store resource ref, ask for reason
    if hasattr(context.bot, "_report_pending") and user.id in context.bot._report_pending:
        from html import escape as _h

        # Fetch resource title and uploader_id
        resource_title = result_id
        uploader_id    = None
        try:
            from database.db import get_pool as _gp
            _pool = await _gp()
            async with _pool.acquire() as _conn:
                if result_id.startswith("book_"):
                    _row = await _conn.fetchrow("SELECT title, uploaded_by FROM books WHERE uid=$1", result_id[5:])
                elif result_id.startswith("note_"):
                    _row = await _conn.fetchrow("SELECT title, uploaded_by FROM notes WHERE uid=$1", result_id[5:])
                elif result_id.startswith("solve_"):
                    _row = await _conn.fetchrow("SELECT title, uploaded_by FROM solves WHERE uid=$1", result_id[6:])
                elif result_id.startswith("psq_"):
                    _row = await _conn.fetchrow("SELECT title, uploaded_by FROM psqs WHERE uid=$1", result_id[4:])
                elif result_id.startswith("vidoc_"):
                    _row = await _conn.fetchrow("SELECT subject as title, uploaded_by FROM vidocs WHERE uid=$1", result_id[6:])
                elif result_id.startswith("util_"):
                    _row = await _conn.fetchrow("SELECT category as title, uploaded_by FROM utilities WHERE uid=$1", result_id[5:])
                elif result_id.startswith("waiver_"):
                    _row = await _conn.fetchrow("SELECT semester_name as title, uploaded_by FROM waivers WHERE uid=$1", result_id[7:])
                elif result_id.startswith("regpay_"):
                    _row = await _conn.fetchrow("SELECT semester as title, uploaded_by FROM regpay WHERE uid=$1", result_id[7:])
                else:
                    _row = None
                if _row:
                    resource_title = _row["title"] or result_id
                    uploader_id    = _row["uploaded_by"]
        except Exception:
            pass

        # Store in user_data and advance to reason step
        context.user_data["resource_ref"]      = result_id
        context.user_data["resource_title"]    = resource_title
        context.user_data["resource_uploader"] = uploader_id
        context.user_data["_report_step"]      = "reason"
        context.user_data["report_files"]      = []
        context.bot._report_pending.discard(user.id)

        try:
            await context.bot.send_message(
                user.id,
                f"<b>Resource selected:</b>\n{_h(resource_title)}\n\n"
                f"Describe the issue briefly.\n\n"
                f"Example: <code>Wrong answer in Q3. The formula used is incorrect.</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Cancel", callback_data="flow_cancel")
                ]])
            )
        except Exception as e:
            logger.error(f"Report prompt failed: {e}")
        return
    # Solution manual flow — admin selected a book
    if hasattr(context.bot, "_sol_pending") and user.id in context.bot._sol_pending:
        if result_id.startswith("book_"):
            book_uid = result_id[5:]
            book = await _fetch_book_for_sol(book_uid)
            if book:
                from handlers.book import _generate_sol_uid
                from html import escape as _h
                sol_uid = await _generate_sol_uid(book_uid)
                context.user_data["sol_book_uid"] = book_uid
                context.user_data["sol_book"]     = book
                context.user_data["sol_uid"]      = sol_uid
                context.user_data["_sol_step"]    = "file"
                context.bot._sol_pending.discard(user.id)

                ed_str = f" ({_h(book['edition'])})" if book.get("edition") else ""
                try:
                    await context.bot.send_message(
                        user.id,
                        f"<b>Book selected:</b>\n"
                        f"{_h(book['title'])}{ed_str}\n"
                        f"{_h(book['authors'])}\n\n"
                        f"<code>UID : {_h(sol_uid)}</code>\n\n"
                        f"Now send the solution manual PDF.",
                        parse_mode="HTML"
                    )
                except Exception as _e:
                    logger.error(f"Sol prompt failed: {_e}")
        return

    # ── Edit / Delete flow — admin picked a resource via "edit:X" or "delete:X" query ──
    if result_id.startswith(("edit_", "delete_")):
        parts  = result_id.split("_", 2)   # ["edit", "note", "uid"]
        if len(parts) == 3:
            action, rtype, uid = parts
            from handlers.resource_picker import start_edit, start_delete
            if action == "edit":
                await start_edit(update, context, uid, rtype)
            else:
                await start_delete(update, context, uid, rtype)
        return

    # Correction flow — admin selected a solve
    if hasattr(context.bot, "_correct_pending") and user.id in context.bot._correct_pending:
        if result_id.startswith("solve_"):
            solve_uid = result_id[6:]
            from handlers.solve import _addcorrect_with_solve
            context.bot._correct_pending.discard(user.id)
            # Use bot.send_message as effective_message
            class _FakeMsg:
                async def reply_text(self, *a, **kw):
                    await context.bot.send_message(user.id, *a, **kw)
            await _addcorrect_with_solve(_FakeMsg(), context, user, solve_uid)
        return

    if result_id in ("denied", "no_result", "tip"):
        return

    allowed = await check_membership(context.bot, user.id)
    if not allowed:
        try:
            await context.bot.send_message(user.id, settings.DENIAL_MESSAGE)
        except Exception:
            pass
        return

    if result_id.startswith("book_"):
        book_uid = result_id[len("book_"):]
        await deliver_book(user.id, book_uid, context.bot)
    elif result_id.startswith("note_"):
        note_uid = result_id[len("note_"):]
        await deliver_note(user.id, note_uid, context.bot)
    elif result_id.startswith("solve_"):
        solve_uid = result_id[len("solve_"):]
        await deliver_solve(user.id, solve_uid, context.bot)
    elif result_id.startswith("regpay_"):
        regpay_uid = result_id[len("regpay_"):]
        await deliver_regpay(user.id, regpay_uid, context.bot)
    elif result_id.startswith("waiver_"):
        waiver_uid = result_id[len("waiver_"):]
        await deliver_waiver(user.id, waiver_uid, context.bot)
    elif result_id.startswith("util_"):
        util_uid = result_id[len("util_"):]
        await deliver_utility(user.id, util_uid, context.bot)
    elif result_id.startswith("vidoc_"):
        vidoc_uid = result_id[len("vidoc_"):]
        await deliver_vidoc(user.id, vidoc_uid, context.bot)
    elif result_id.startswith("psq_"):
        psq_uid = result_id[len("psq_"):]
        await deliver_psq(user.id, psq_uid, context.bot)
    elif result_id.isdigit():
        await _send_resource_to_dm(context.bot, user.id, int(result_id))


async def via_bot_delivery_handler(update, context):
    pass


def _result_id(rec: dict, base_id: str) -> str:
    """Return result ID, prefixed for pick mode if _pick_prefix is set."""
    prefix = rec.get("_pick_prefix", "")
    return f"{prefix}{base_id.split('_', 1)[-1]}" if prefix else base_id


def _make_book_inline_result(book: dict, cover_url: str = "") -> InlineQueryResultArticle:
    """
    Book inline result — always list view (InlineQueryResultArticle).
    Shows cover as thumbnail when available.
    Format:
      Title (Edition)
      Authors
      Subject · Course Codes
    """
    title        = book.get("title", "")
    authors      = book.get("authors", "")
    edition      = book.get("edition") or ""
    uid          = book.get("uid", "")
    subject      = book.get("subject") or ""
    course_codes = book.get("course_codes") or ""

    ed_str = f" ({edition})" if edition else ""

    # Third line: subject · course codes
    if subject and course_codes:
        third_line = f"{subject} · {course_codes}"
    elif subject:
        third_line = subject
    elif course_codes:
        third_line = course_codes
    else:
        third_line = ""

    desc = f"{authors}\n{third_line}" if third_line else authors
    msg_text = f"📚 {title}{ed_str} — {authors}"

    kwargs = dict(
        id=_result_id(book, f"book_{uid}"),
        title=f"📚 {title}{ed_str}",
        description=desc,
        input_message_content=InputTextMessageContent(message_text=msg_text)
    )

    if cover_url:
        kwargs["thumbnail_url"]   = cover_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100

    return InlineQueryResultArticle(**kwargs)


def _make_note_inline_result(note: dict, cover_url: str = "") -> InlineQueryResultArticle:
    """Note inline result — list view with optional cover thumbnail."""
    import json as _json
    title       = note.get("title", "")
    uid         = note.get("uid", "")
    course_code = note.get("course_code") or ""
    subject     = note.get("subject") or ""
    file_type   = note.get("file_type", "").upper()
    tags_raw    = note.get("tags", [])
    try:
        tags = _json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []

    # subtitle: course · subject
    parts = [p for p in [course_code, subject] if p]
    subtitle = " · ".join(parts) if parts else ""

    kwargs = dict(
        id=_result_id(note, f"note_{uid}"),
        title=f"📝 {title}",
        description=subtitle or file_type,
        input_message_content=InputTextMessageContent(
            message_text=f"📝 {title} — {course_code or subject or 'Note'}"
        )
    )
    if cover_url:
        kwargs["thumbnail_url"]   = cover_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100

    return InlineQueryResultArticle(**kwargs)


def _make_regpay_inline_result(r: dict, thumbnail_url: str = "") -> InlineQueryResultArticle:
    """Registration & Payment Info inline result."""
    uid      = r.get("uid", "")
    semester = r.get("semester", "Registration & Payment")
    kwargs   = dict(
        id=_result_id(r, f"regpay_{uid}"),
        title="📋 Registration & Payment Info",
        description=semester,
        input_message_content=InputTextMessageContent(
            message_text=f"📋 Registration & Payment Info — {semester}"
        )
    )
    if thumbnail_url:
        kwargs["thumbnail_url"]   = thumbnail_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100
    return InlineQueryResultArticle(**kwargs)


def _make_waiver_inline_result(w: dict, thumbnail_url: str = "") -> InlineQueryResultArticle:
    """Waiverulator inline result."""
    uid      = w.get("uid", "")
    semester = w.get("semester_name", "Waiverulator")

    kwargs = dict(
        id=_result_id(w, f"waiver_{uid}"),
        title=f"🧮 Waiverulator",
        description=f"{semester}",
        input_message_content=InputTextMessageContent(
            message_text=f"🧮 Waiverulator — {semester}"
        )
    )
    if thumbnail_url:
        kwargs["thumbnail_url"]   = thumbnail_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100
    return InlineQueryResultArticle(**kwargs)


def _make_utility_inline_result(u: dict) -> InlineQueryResultArticle:
    """Utility inline result — calendar, advisor, fee — with thumbnail."""
    uid  = u.get("uid", "")
    cat  = u.get("category", "")
    cats = {
        "cal":       ("📅", "Academic Calendar"),
        "advisor":   ("👨‍🏫", "Advisor Info"),
        "fee":       ("💰", "Fee Overview"),
        "syllabus":  ("📋", "Syllabus"),
        "outline":   ("📐", "Course Outline"),
        "routine":   ("🗓", "Exam Routine"),
        "util_misc": ("🔧", "Utility"),
        "slides":    ("📑", "Slides"),
    }
    emoji, label = cats.get(cat, ("📄", "Info"))

    import json as _json
    tags_raw = u.get("tags", [])
    try:
        tags = _json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    subtitle = " ".join([f"#{t}" for t in tags[:4]]) if tags else label

    kwargs = dict(
        id=_result_id(u, f"util_{uid}"),
        title=f"{emoji} {label}",
        description=subtitle,
        input_message_content=InputTextMessageContent(
            message_text=f"{emoji} {label}"
        )
    )

    thumb = u.get("thumbnail_url") or ""
    if thumb:
        kwargs["thumbnail_url"]   = thumb
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100

    return InlineQueryResultArticle(**kwargs)


def _make_vidoc_inline_result(vidoc: dict, thumbnail_url: str = "") -> InlineQueryResultArticle:
    """Vidoc inline result — list view with YouTube thumbnail."""
    import json as _json
    uid         = vidoc.get("uid", "")
    course_code = vidoc.get("course_code") or ""
    subject     = vidoc.get("subject") or ""
    tags_raw    = vidoc.get("tags", [])
    try:
        tags = _json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []

    try:
        msgs = _json.loads(vidoc["messages"]) if isinstance(vidoc["messages"], str) else vidoc.get("messages", [])
    except Exception:
        msgs = []

    parts    = [p for p in [course_code, subject] if p]
    subtitle = " · ".join(parts) if parts else f"{len(msgs)} messages"

    kwargs = dict(
        id=_result_id(vidoc, f"vidoc_{uid}"),
        title=f"🎬 {course_code or subject or uid}",
        description=subtitle,
        input_message_content=InputTextMessageContent(
            message_text=f"🎬 Videos & Docs — {course_code or subject or 'Video'}"
        )
    )
    if thumbnail_url:
        kwargs["thumbnail_url"]   = thumbnail_url
        kwargs["thumbnail_width"]  = 120
        kwargs["thumbnail_height"] = 90

    return InlineQueryResultArticle(**kwargs)


def _make_solve_inline_result(solve: dict, cover_url: str = "") -> InlineQueryResultArticle:
    """Solve inline result — list view."""
    import json as _json
    title       = solve.get("title", "")
    uid         = solve.get("uid", "")
    course_code = solve.get("course_code") or ""
    subject     = solve.get("subject") or ""
    parts       = [p for p in [course_code, subject] if p]
    subtitle    = " · ".join(parts) if parts else ""

    kwargs = dict(
        id=_result_id(solve, f"solve_{uid}"),
        title=f"✅ {title}",
        description=subtitle or "Solution",
        input_message_content=InputTextMessageContent(
            message_text=f"✅ {title} — {course_code or subject or 'Solve'}"
        )
    )
    if cover_url:
        kwargs["thumbnail_url"]   = cover_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100
    return InlineQueryResultArticle(**kwargs)


def _make_psq_inline_result(psq: dict, cover_url: str = "") -> InlineQueryResultArticle:
    """PSQ inline result — list view with title."""
    import json as _json
    uid      = psq.get("uid", "")
    title    = psq.get("title") or "Previous Semester Questions"
    tags_raw = psq.get("tags", [])
    try:
        tags = _json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []

    sys_tags = {"psq", "previous", "questions"}
    admin_tags = [t for t in tags if t not in sys_tags]
    subtitle = " ".join([f"#{t}" for t in admin_tags]) if admin_tags else "#psq"

    kwargs = dict(
        id=_result_id(psq, f"psq_{uid}"),
        title=f"📋 {title}",
        description=subtitle,
        input_message_content=InputTextMessageContent(
            message_text=f"📋 {title}"
        )
    )
    if cover_url:
        kwargs["thumbnail_url"]   = cover_url
        kwargs["thumbnail_width"]  = 100
        kwargs["thumbnail_height"] = 100

    return InlineQueryResultArticle(**kwargs)


def _make_inline_result(r, source: str) -> InlineQueryResultArticle:
    tags_raw = r.get("tags", [])
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except Exception:
            tags = []
    elif isinstance(tags_raw, list):
        tags = tags_raw
    else:
        tags = []

    course   = r.get("course_code") or "General"
    category = (r.get("category") or "resource").replace("_", " ").title()
    clean_desc = f"{course} · {category}" if course and course != "General" else category

    return InlineQueryResultArticle(
        id=str(r["id"]),
        title=r["title"],
        description=clean_desc,
        input_message_content=InputTextMessageContent(
            message_text=f"📎 {r['title']} — {course}"
        )
    )


def _make_no_result(query_text: str, cat_label: str = "") -> InlineQueryResultArticle:
    if cat_label:
        title = f"📭 No {cat_label} found"
        desc  = f"No results for \"{query_text}\". Try a different keyword or course code."
    else:
        title = "📭 No resources found"
        desc  = f"Nothing matched \"{query_text}\". Try shorter keywords or course codes."

    return InlineQueryResultArticle(
        id="no_result",
        title=title,
        description=desc,
        input_message_content=InputTextMessageContent(
            message_text=(
                f"🦅 No resource found for: {query_text}\n\n"
                f"Tips:\n"
                f"• Try course code: CSE311 note\n"
                f"• Try shorter keywords: dbms mid\n"
                f"• Or just DM Dr. Crow your question directly!"
            )
        )
    )


def _make_tip_result() -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="tip",
        title="🔍 Start typing to search...",
        description="Try: CSE311 note, dbms mid, algorithm book",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🦅 Dr. Crow Search Tips\n\n"
                "• CSE311 note → notes for that course\n"
                "• dbms mid → DBMS midterm resources\n"
                "• algorithm book → algorithm textbooks\n\n"
                "Just type your query after @drcrow_bot!"
            )
        )
    )


async def handle_search_callback(update, context):
    query = update.callback_query
    data  = query.data
    user  = update.effective_user

    from middleware.membership import check_membership
    if not settings.is_admin(user.id):
        allowed = await check_membership(context.bot, user.id)
        if not allowed:
            await query.answer("🔒 Access restricted to Twilight Crows members.", show_alert=True)
            return

    if data.startswith("book_get_"):
        book_uid = data.replace("book_get_", "")
        await query.answer("📥 Sending to your DM...")
        await deliver_book(user.id, book_uid, context.bot)

    elif data.startswith("search_get_"):
        resource_id = int(data.split("_")[-1])
        await query.answer("📥 Sending to your DM...")
        await _send_resource_to_dm(context.bot, user.id, resource_id)

    elif data == "search_report_start":
        await query.answer()
        await query.message.reply_text(
            "To report a resource, send me its *resource ID*:",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data.startswith("search_course_"):
        course = data.replace("search_course_", "")
        await query.answer()
        await _show_course_resources(update, context, course)

    elif data.startswith("course_sub_"):
        course_code = data.replace("course_sub_", "")
        await _toggle_subscription(update, context, course_code)


async def _send_resource_to_dm(bot, user_id: int, resource_id: int):
    resource = await queries.get_resource(resource_id)
    if not resource:
        try:
            await bot.send_message(user_id, "⚠️ Resource not found or has been removed.")
        except Exception:
            pass
        return

    caption   = format_resource_caption(resource)
    file_type = resource["file_type"]

    try:
        if file_type in ("photo", "image"):
            await bot.send_photo(user_id, resource["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN)
        elif file_type == "video":
            await bot.send_video(user_id, resource["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN)
        elif file_type == "audio":
            await bot.send_audio(user_id, resource["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await bot.send_document(user_id, resource["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN)

        await queries.increment_access(resource_id)
        await queries.increment_download(user_id)
        await queries.log_event("download", user_id=user_id, resource_id=resource_id)
        await queries.update_last_active(user_id)

    except Exception as e:
        logger.error(f"Failed to send resource {resource_id} to {user_id}: {e}")
        try:
            await bot.send_message(user_id, "⚠️ Failed to send file. Please try again.")
        except Exception:
            pass


async def _show_course_resources(update, context, course: str):
    query     = update.callback_query
    resources = await queries.get_resources_by_course(course, limit=20)
    user      = update.effective_user
    subs      = await queries.get_user_subscriptions(user.id)
    is_subbed = course in subs

    if not resources:
        await query.edit_message_text(
            f"*{course}* — No resources yet.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    buttons = []
    for r in resources:
        label = f"{r['title'][:40]}{'...' if len(r['title']) > 40 else ''}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"search_get_{r['id']}")])

    sub_label = "🔔 Unsubscribe" if is_subbed else "🔕 Subscribe"
    buttons.append([InlineKeyboardButton(sub_label, callback_data=f"course_sub_{course}")])

    await query.edit_message_text(
        f"*{course}* — {len(resources)} resource(s)\n_Tap to get file in DM_ 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _toggle_subscription(update, context, course_code: str):
    query = update.callback_query
    user  = update.effective_user
    subs  = await queries.get_user_subscriptions(user.id)
    if course_code in subs:
        await queries.unsubscribe(user.id, course_code)
        await query.answer(f"Unsubscribed from {course_code}")
    else:
        await queries.subscribe(user.id, course_code)
        await query.answer(f"✅ Subscribed to {course_code}!")


async def handle_course_callback(update, context):
    if not await should_respond(update, context.bot):
        return
    query  = update.callback_query
    course = query.data.replace("course_", "")
    await query.answer()
    await _show_course_resources(update, context, course)


async def dm_fallback(update, context):
    if not await should_respond(update, context.bot):
        return

    msg  = update.message
    user = update.effective_user

    if msg.via_bot:
        return

    text = msg.text.strip() if msg.text else ""
    if not text:
        return

    # Check if admin has pending approval/rejection
    if await handle_admin_pending_message(update, context):
        return

    # Check if user is in report flow (resource selected via inline)
    if await handle_report_pending_message(update, context):
        return

    await queries.update_last_active(user.id)
    await queries.log_event("dm_fallback", user_id=user.id, meta={"query": text[:200]})

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    await msg.reply_text(
        "Use the search button or tap below to find resources:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Search: " + text[:30],
                switch_inline_query_current_chat=text[:30]
            )
        ]])
    )


async def _report_start(update, context):
    if not await should_respond(update, context.bot):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🚩 *Report a Resource*\n\nSend the *resource ID* to report:",
        parse_mode=ParseMode.MARKDOWN
    )
    return REPORT_RESOURCE_ID


async def _report_get_id(update, context):
    try:
        resource_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please send a valid numeric resource ID.")
        return REPORT_RESOURCE_ID

    resource = await queries.get_resource(resource_id)
    if not resource:
        await update.message.reply_text("❌ Resource not found.")
        return REPORT_RESOURCE_ID

    context.user_data["report_resource_id"] = resource_id
    await update.message.reply_text(
        f"Reporting: *{resource['title']}*\n\nDescribe the issue:",
        parse_mode=ParseMode.MARKDOWN
    )
    return REPORT_REASON


async def _report_get_reason(update, context):
    reason      = update.message.text.strip()
    resource_id = context.user_data.get("report_resource_id")
    user        = update.effective_user

    if not resource_id:
        await update.message.reply_text("❌ Something went wrong. Start again.")
        return ConversationHandler.END

    report_id = await queries.insert_report(user.id, resource_id, reason)

    for admin_id in settings.ADMIN_IDS:
        try:
            resource = await queries.get_resource(resource_id)
            await context.bot.send_message(
                admin_id,
                f"🚩 *New Report* #{report_id}\n\n"
                f"Resource: *{resource['title']}* (ID: {resource_id})\n"
                f"Reporter: {user.full_name}\n"
                f"Reason: {reason}\n\nUse /admin to review.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

    await update.message.reply_text("✅ Report submitted! Admins will review it. 🦅")
    context.user_data.clear()
    return ConversationHandler.END


async def _report_cancel(update, context):
    await update.message.reply_text("Report cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def report_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("report", _report_start),
            CallbackQueryHandler(_report_start, pattern="^search_report_start$")
        ],
        states={
            REPORT_RESOURCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, _report_get_id)],
            REPORT_REASON:      [MessageHandler(filters.TEXT & ~filters.COMMAND, _report_get_reason)],
        },
        fallbacks=[CommandHandler("cancel", _report_cancel)],
        conversation_timeout=120
    )