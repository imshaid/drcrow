"""
Dr. Crow — The Brain of Twilight Crows
Main entry point.
"""

import asyncio
import logging
from telegram.ext import (
    Application, CommandHandler, MessageHandler, InlineQueryHandler,
    CallbackQueryHandler, ChatMemberHandler, ConversationHandler,
    ChosenInlineResultHandler, filters
)
from config.settings import settings
from database.db import init_db
from handlers import start, search, upload, admin, moderation, profile, exam
from handlers.search import _deliver_resource_by_uid
from handlers import admin_commands
from handlers.broadcast import broadcast_conversation
from handlers.book import addbook_conversation, addsolution_start, addsolution_handle_file
from handlers.regpay import (
    addregpay_conversation, editregpay_conversation,
    deleteregpay_conversation, listregpays_cmd, listregpays_page_callback,
    sethelp_conversation, show_profile, send_help
)
from handlers.upload import (
    upload_conversation,
    handle_upload_callback, handle_report_callback,
    admin_report_reject_message,
    report_start, report_handle_text, report_handle_file,
)
from handlers.waiver import (
    addwaiver2_conversation, addwaiver_conversation, editwaiver_conversation,
    deletewaiver_conversation, listwaivers_cmd, listwaivers_page_callback
)
from handlers.utility import (
    addcal_conversation, addadvisor_conversation, addfee_conversation,
    editcal_conversation, editadvisor_conversation, editfee_conversation,
    deletecal_conversation, deleteadvisor_conversation, deletefee_conversation,
    listcals_cmd, listadvisors_cmd, listfees_cmd,
    listcals_page_callback, listadvisors_page_callback, listfees_page_callback,
    _make_add_conversation, _make_edit_conversation, _make_delete_conversation,
    _make_list_cmd, _make_list_page_callback,
    addsyllabus2_conversation, addoutline_conversation, addroutine_conversation,
    addcal2_conversation, addadvisor2_conversation, addregpay2_conversation,
    addutil2_conversation,
    _make_edit_extended_conversation,
    addsyllabus_conversation, editsyllabus_conversation,
    addutil_conversation, editutil_conversation,
    deleteutil_conversation, listutils_cmd, listutils_page_callback
)
from handlers.vidoc import (
    addvidoc2_conversation, addvidoc_conversation, editvidoc_conversation,
    deletevidoc_conversation, listvidocs_cmd, listvidocs_page_callback
)
from handlers.solve import (
    addsolve_conversation, editsolve_conversation, deletesolve_conversation,
    listsolves_cmd, listsolves_page_callback,
    addcorrect_start, addcorrect_handle_file, addcorrect_done,
    editcorrect_conversation, deletecorrect_conversation,
    handle_correction_callback
)
from handlers.psq import (
    addpsq_conversation, editpsq_conversation,
    deletepsq_conversation, listpsqs_cmd, listpsqs_page_callback
)
from handlers.note import (
    addnote_conversation, editnote_conversation,
    deletenote_conversation, listnotes_cmd, listnotes_page_callback
)
from handlers.book_edit import (
    editbook_conversation, editsolution_conversation,
    deletebook_conversation, deletesolution_conversation,
    listbooks_cmd, listsolutions_cmd, listbooks_page_callback
)
from handlers.resource_picker import resource_picker_conversation
from handlers.semester import semester_conversation
from handlers.ai_chat import ai_chat_conversation
from scheduler.jobs import setup_scheduler
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)


async def health_check(request):
    return web.Response(text="Dr. Crow is alive. 🦅")


def build_app() -> Application:
    app = (
        Application.builder()
        .token(settings.BOT_TOKEN)
        .build()
    )

    # Chat member events
    app.add_handler(ChatMemberHandler(
        moderation.handle_chat_member_update,
        ChatMemberHandler.ANY_CHAT_MEMBER
    ))

    # Group: respond only in allowed topics, ignore everything else
    _group_filter = filters.Chat(chat_id=settings.GROUP_ID) & ~filters.UpdateType.EDITED_MESSAGE
    app.add_handler(MessageHandler(_group_filter, start.group_redirect), group=0)

    # Conversation flows
    app.add_handler(addbook_conversation())
    app.add_handler(editbook_conversation())
    app.add_handler(editsolution_conversation())
    app.add_handler(deletebook_conversation())
    app.add_handler(deletesolution_conversation())
    app.add_handler(addnote_conversation())
    app.add_handler(editnote_conversation())
    app.add_handler(deletenote_conversation())
    app.add_handler(addpsq_conversation())
    app.add_handler(editpsq_conversation())
    app.add_handler(deletepsq_conversation())
    app.add_handler(addsolve_conversation())
    app.add_handler(editsolve_conversation())
    app.add_handler(deletesolve_conversation())
    app.add_handler(editcorrect_conversation())
    app.add_handler(deletecorrect_conversation())
    app.add_handler(addvidoc2_conversation())
    app.add_handler(editvidoc_conversation())
    app.add_handler(deletevidoc_conversation())
    app.add_handler(addcal2_conversation())
    app.add_handler(addadvisor2_conversation())
    app.add_handler(addfee_conversation())
    app.add_handler(editcal_conversation())
    app.add_handler(editadvisor_conversation())
    app.add_handler(editfee_conversation())
    app.add_handler(deletecal_conversation())
    app.add_handler(deleteadvisor_conversation())
    app.add_handler(deletefee_conversation())
    app.add_handler(addsyllabus2_conversation())
    app.add_handler(addoutline_conversation())
    app.add_handler(addroutine_conversation())
    app.add_handler(editsyllabus_conversation())
    app.add_handler(addutil2_conversation())
    app.add_handler(editutil_conversation())
    app.add_handler(deleteutil_conversation())
    app.add_handler(_make_edit_extended_conversation("outline",  "editoutline"))
    app.add_handler(_make_edit_extended_conversation("routine",  "editroutine"))
    app.add_handler(_make_delete_conversation("syllabus", "deletesyllabus"))
    app.add_handler(_make_delete_conversation("outline",  "deleteoutline"))
    app.add_handler(_make_delete_conversation("routine",  "deleteroutine"))
    app.add_handler(addwaiver2_conversation())
    app.add_handler(editwaiver_conversation())
    app.add_handler(deletewaiver_conversation())
    app.add_handler(addregpay2_conversation())
    app.add_handler(editregpay_conversation())
    app.add_handler(deleteregpay_conversation())
    app.add_handler(sethelp_conversation())
    app.add_handler(broadcast_conversation())
    app.add_handler(ai_chat_conversation())
    app.add_handler(semester_conversation())
    app.add_handler(resource_picker_conversation())
    # Correction handlers use group=1 — runs after group=0 (ConversationHandlers)
    # but _correct_file_handler checks _correct_step flag before acting
    app.add_handler(upload_conversation())
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Document.ALL | filters.PHOTO),
        _exam_file_handler
    ), group=2)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Document.ALL | filters.PHOTO),
        _correct_file_handler
    ), group=3)
    app.add_handler(CommandHandler("done", _correct_done_handler,
        filters=filters.ChatType.PRIVATE), group=3)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & filters.Regex(r"^Report$"),
        report_start
    ))
    app.add_handler(CommandHandler("report", report_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("addsolution", addsolution_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("addcorrect", addcorrect_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("done",   _report_done_handler, filters=filters.ChatType.PRIVATE), group=1)

    # List commands (admin only, no conversation needed)
    app.add_handler(CommandHandler("listbooks",     listbooks_cmd,     filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listsolutions", listsolutions_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listnotes",     listnotes_cmd,     filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listpsqs",      listpsqs_cmd,      filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listsolves",    listsolves_cmd,    filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listvidocs",    listvidocs_cmd,    filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listcals",      listcals_cmd,      filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listadvisors",  listadvisors_cmd,  filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listfees",       listfees_cmd,       filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listsyllabuses", _make_list_cmd("syllabus"), filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listoutlines",   _make_list_cmd("outline"),  filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listroutines",   _make_list_cmd("routine"),  filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listwaivers",    listwaivers_cmd,            filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listutils",      listutils_cmd,              filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listregpays",    listregpays_cmd,            filters=filters.ChatType.PRIVATE))

    # Inline search
    app.add_handler(InlineQueryHandler(search.inline_search))
    app.add_handler(ChosenInlineResultHandler(search.chosen_inline_result_handler))

    # Callback queries
    app.add_handler(CallbackQueryHandler(handle_get_resource, pattern="^get_res_"))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # DM commands
    app.add_handler(CommandHandler("start",      start.cmd_start,             filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("profile",    profile.cmd_profile,         filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("admin",      admin.cmd_admin,             filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("cancel",     cancel_handler,              filters=filters.ChatType.PRIVATE))

    # Admin commands
    app.add_handler(CommandHandler("addexam",    admin_commands.cmd_addexam,   filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("feature",    admin_commands.cmd_feature,   filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("deactivate", admin_commands.cmd_deactivate,filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("semester",   admin_commands.cmd_semester,  filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stats",      admin_commands.cmd_stats,     filters=filters.ChatType.PRIVATE))

    # DM text fallback — group=1 so ConversationHandlers (group=0) take priority
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        dm_text_handler
    ), group=1)
    # Report file handler — catches files when user is in report files step
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.Document.ALL | filters.PHOTO |
            filters.VIDEO | filters.VIDEO_NOTE |
            filters.AUDIO | filters.VOICE
        ),
        _report_file_handler
    ), group=1)
    # Solution file handler — group=2
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.ALL,
        _sol_file_handler
    ), group=2)


    async def error_handler(update, context):
        import logging as _lg
        _lg.getLogger(__name__).error(
            f"Exception: {context.error}", exc_info=context.error
        )
    app.add_error_handler(error_handler)

    return app


# Map of callback suffix → command string to simulate
_ADM_ADD_MAP = {
    "book":     "addbook",
    "note":     "addnote",
    "psq":      "addpsq",
    "solve":    "addsolve",
    "vidoc":    "addvidoc",
    "solution": "addsolution",
    "syllabus": "addsyllabus",
    "outline":  "addoutline",
    "routine":  "addroutine",
    "util":     "addutil",
    "waiver":   "addwaiver",
    "regpay":   "addregpay",
    "cal":      "addcal",
    "advisor":  "addadvisor",
    "fee":      "addfee",
}

async def _adm_add_callback(update, context):
    """Handles adm_add_* inline buttons — simulates the corresponding command."""
    query = update.callback_query
    if not settings.is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    await query.answer()

    suffix = query.data.replace("adm_add_", "")

    # Handle types that don't use _ADM_ADD_MAP
    if suffix == "correct":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        for k in ["_correct_step", "_correct_solve_uid", "_correct_solve", "_correct_uid"]:
            context.user_data.pop(k, None)
        context.user_data["_correct_step"]    = "search"
        context.user_data["_in_conversation"] = True
        if not hasattr(context.bot, "_correct_pending"):
            context.bot._correct_pending = set()
        context.bot._correct_pending.add(query.from_user.id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Search Solve", switch_inline_query_current_chat="solve ")
        ]])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="<b>Add Correction</b>\n\nSearch for the solve to attach the correction to.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    cmd = _ADM_ADD_MAP.get(suffix)
    if not cmd:
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Simulate the command by sending a fake update through the dispatcher
    # Easiest: just tell the user which command to send, pre-filled
    # Actually we trigger the handler directly via a message with the command
    # PTB way: create a fake Message isn't clean. Better: send instruction.
    # Best clean way: use deep linking or just directly call the start function.

    # Direct approach — call the start function directly
    cmd_handlers = {
        "addbook":    ("handlers.book",    "addbook_start"),
        "addnote":    ("handlers.note",    "addnote_start"),
        "addpsq":     ("handlers.psq",     "addpsq_start"),
        "addsolve":   ("handlers.solve",   "addsolve_start"),
        "addvidoc":   ("handlers.vidoc",   "addvidoc_start"),
        "addsolution":("handlers.book",    "addsolution_start"),
        "addsyllabus":("handlers.utility", "addsyllabus_start"),
        "addoutline": ("handlers.utility", "addoutline_start"),
        "addroutine": ("handlers.utility", "addroutine_start"),
        "addutil":    ("handlers.utility", "addutil_start"),
        "addwaiver":  ("handlers.waiver",  "addwaiver_start"),
        "addregpay":  ("handlers.regpay",  "addregpay_start"),
        "addcal":     ("handlers.utility", "addcal_start"),
        "addadvisor": ("handlers.utility", "addadvisor_start"),
        "addfee":     ("handlers.utility", "addfee_start"),
    }

    handler_info = cmd_handlers.get(cmd)
    if not handler_info:
        return

    module_name, fn_name = handler_info
    import importlib
    module = importlib.import_module(module_name)
    fn     = getattr(module, fn_name, None)
    if not fn:
        return

    # Patch the message onto context and call start function directly.
    # ConversationHandler needs update.message — we use query.message.
    # We also need context.args for commands that take arguments.
    context.args = []

    # For book: ConversationHandler entry_point (CallbackQueryHandler) handles it directly.
    # For other types: send the command so admin can trigger it.
    # ConversationHandler registered first takes priority over this callback.
    label_map = {
        "book":     None,  # handled by addbook_conversation entry_point
        "solution": None,  # handled by addsolution_start call below
        "correct":  None,  # handled by addcorrect_start call below
        "note":     None,  # handled by addnote_conversation entry_point
        "psq":      None,  # handled by addpsq_conversation entry_point
        "solve":    None,  # handled by addsolve_conversation entry_point
        "vidoc":    "/addvidoc",
        "syllabus": None,  # handled by addsyllabus2_conversation
        "outline":  None,  # handled by addoutline_conversation
        "routine":  None,  # handled by addroutine_conversation
        "util":     "/addutil",
        "waiver":   "/addwaiver",
        "regpay":   None,  # handled by addregpay2_conversation
        "cal":      None,  # handled by addcal2_conversation
        "advisor":  None,  # handled by addadvisor2_conversation
        "fee":      "/addfee",
    }
    if suffix == "solution":
        context.args = []
        await addsolution_start(
            type("_U", (), {
                "effective_message": query.message,
                "effective_user":    query.from_user,
                "message":           query.message,
            })(),
            context
        )
        return

    hint = label_map.get(suffix)
    if hint:
        await query.message.reply_text(
            f"Type <code>{hint}</code> to start.",
            parse_mode="HTML"
        )


async def _flow_cancel_callback(update, context):
    """Handles Cancel inline button for both upload and report flows."""
    query = update.callback_query
    ud    = context.user_data

    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Report flow — either waiting for resource selection or in a step
    if ud.get("_report_step") or hasattr(context.bot, "_report_pending") and update.effective_user.id in context.bot._report_pending:
        from handlers.upload import _report_cancel
        await _report_cancel(update, context)
        return

    # Upload flow handled by ConversationHandler fallback — nothing extra needed here


async def _report_done_handler(update, context):
    """Handles /done command when user is in report files step."""
    if not context.user_data.get("_report_step"):
        return
    from handlers.upload import _report_submit
    await _report_submit(update, context)


async def _correct_done_handler(update, context):
    """Handles /done for correction upload."""
    await addcorrect_done(update, context)


async def _correct_file_handler(update, context):
    """Handles correction file upload via state machine."""
    import logging as _lg
    _lg.getLogger(__name__).info(
        f"_correct_file_handler: step={context.user_data.get('_correct_step')!r} "
        f"user={update.effective_user.id}"
    )
    from handlers.solve import addcorrect_handle_file
    await addcorrect_handle_file(update, context)


async def _sol_file_handler(update, context):
    """Handles solution PDF when admin is in sol flow."""
    await addsolution_handle_file(update, context)


async def _report_file_handler(update, context):
    """Handles file messages when user is in report files step."""
    from handlers.upload import report_handle_file
    await report_handle_file(update, context)


async def _exam_file_handler(update, context):
    """Intercepts photo/document when admin is in exam add flow."""
    if not context.user_data.get("exam_add_step"):
        return
    from handlers.exam import handle_exam_admin_message
    await handle_exam_admin_message(update, context)


async def dm_text_handler(update, context):
    from handlers.start import (
        ALL_REPLY_BUTTONS, handle_reply_button,
        handle_waiver_calc_input, handle_admin_add_input
    )

    text = (update.message.text or "").strip()

    # Skip texts handled exclusively by ConversationHandlers (group=0)
    _conv_only = {"Upload", "Report"}
    if text in _conv_only:
        return

    # Report flow — check first before anything else
    if context.user_data.get("_report_step"):
        from handlers.upload import report_handle_text
        if await report_handle_text(update, context):
            return

    # Skip if user is inside upload ConversationHandler or resource picker
    if (context.user_data.get("_in_conversation")
            or context.user_data.get("rpe_uid")
            or context.user_data.get("sm_new_name")
            or context.user_data.get("sm_edit_uid")):
        return

    # Skip if this update was already handled by a ConversationHandler (group=0)
    # semester and resource_picker handlers mark the update_id in bot_data
    handled_updates = context.bot_data.get("_handled_update_ids", set())
    if update.update_id in handled_updates:
        handled_updates.discard(update.update_id)
        return

    # 1. Reply KB button press
    if text in ALL_REPLY_BUTTONS:
        await handle_reply_button(update, context)
        return

    # 1b. Exam admin multi-step (file/title/CSV)
    from handlers.exam import handle_exam_admin_message, handle_member_section_input
    if await handle_exam_admin_message(update, context):
        return

    # 1c. Member section input
    if await handle_member_section_input(update, context):
        return

    # 2. Admin pending approve/reject / report reject
    if settings.is_admin(update.effective_user.id):
        if await admin_report_reject_message(update, context):
            return
        from handlers.upload import handle_admin_pending_message
        if await handle_admin_pending_message(update, context):
            return

    # 3. Admin add uid input
    if settings.is_admin(update.effective_user.id):
        if await handle_admin_add_input(update, context):
            return

    # 4. Advisor student ID input
    from handlers.advisor import handle_advisor_id_input
    if await handle_advisor_id_input(update, context):
        return

    # 5. Waiver free-text input
    if await handle_waiver_calc_input(update, context):
        return

    # 6. Fallback
    await search.dm_fallback(update, context)


async def handle_get_resource(update, context):
    """
    [📥 Get Resource] button from subscription notifications.
    Delivers the resource directly to member DM — UID never visible.
    """
    query = update.callback_query
    uid   = query.data.replace("get_res_", "")
    user  = update.effective_user

    delivered = await _deliver_resource_by_uid(context.bot, user.id, uid)
    if delivered:
        await query.answer("Here you go!")
    else:
        await query.answer("Resource not found or unavailable.", show_alert=True)


async def handle_callback(update, context):
    query = update.callback_query
    data  = query.data or ""
    user  = update.effective_user

    if False:  # menu_ callbacks removed — handled by Reply KB now
        pass
    elif data.startswith("correct_one_") or data.startswith("correct_all_"):
        await handle_correction_callback(update, context)
    elif data.startswith("lu_cal_page_") or data == "lu_cal_noop":
        await listcals_page_callback(update, context)
    elif data.startswith("lu_advisor_page_") or data == "lu_advisor_noop":
        await listadvisors_page_callback(update, context)
    elif data.startswith("lu_syllabus_page_") or data == "lu_syllabus_noop":
        await _make_list_page_callback("syllabus")(update, context)
    elif data.startswith("lu_outline_page_") or data == "lu_outline_noop":
        await _make_list_page_callback("outline")(update, context)
    elif data.startswith("lu_util_misc_page_") or data == "lu_util_misc_noop":
        await listutils_page_callback(update, context)
    elif data.startswith("lrp_page_") or data == "lrp_noop":
        await listregpays_page_callback(update, context)
    elif data.startswith("lw_page_") or data == "lw_noop":
        await listwaivers_page_callback(update, context)
    elif data.startswith("lu_routine_page_") or data == "lu_routine_noop":
        await _make_list_page_callback("routine")(update, context)
    elif data.startswith("lu_fee_page_") or data == "lu_fee_noop":
        await listfees_page_callback(update, context)
    elif data.startswith("lv_page_") or data == "lv_noop":
        await listvidocs_page_callback(update, context)
    elif data.startswith("lsv_page_") or data == "lsv_noop":
        await listsolves_page_callback(update, context)
    elif data.startswith("lp_page_") or data == "lp_noop":
        await listpsqs_page_callback(update, context)
    elif data.startswith("ln_page_") or data == "ln_noop":
        await listnotes_page_callback(update, context)
    elif data.startswith("lb_page_") or data == "lb_noop":
        await listbooks_page_callback(update, context)
    elif data == "profile":
        await query.answer()
        await show_profile(user.id, user.id, context.bot)
    elif data == "help":
        await query.answer()
        await send_help(user.id, context.bot)
    elif data.startswith("book_get_"):
        await search.handle_search_callback(update, context)
    elif data.startswith("search_") or data.startswith("course_"):
        await search.handle_search_callback(update, context)
    elif data.startswith("upload_approve_") or data.startswith("upload_reject_"):
        await handle_upload_callback(update, context)
    elif data.startswith("adm_add_correct_"):
        from handlers.solve import addcorrect_start
        await addcorrect_start(update, context)
    elif data.startswith("adm_add_"):
        await _adm_add_callback(update, context)
    elif data.startswith("rpt_accept_") or data.startswith("rpt_reject_"):
        await handle_report_callback(update, context)
    elif data == "flow_cancel":
        await _flow_cancel_callback(update, context)
    elif data.startswith("upload_"):
        await upload.handle_upload_callback(update, context)
    elif data.startswith("admin_"):
        await admin.handle_admin_callback(update, context)
    elif data.startswith("exam_adm_"):
        from handlers.exam import handle_exam_admin_callback
        await handle_exam_admin_callback(update, context)
    elif data.startswith("exam_"):
        from handlers.exam import handle_exam_admin_callback
        await handle_exam_admin_callback(update, context)
    elif data.startswith("profile_"):
        await profile.handle_profile_callback(update, context)
    elif data.startswith("sub_"):
        from handlers.subscription import handle_subscription_callback
        await handle_subscription_callback(update, context)
    elif data.startswith("clean_"):
        await admin_commands.handle_cleanup_callback(update, context)
    else:
        await query.answer("Unknown action.", show_alert=True)


async def cancel_handler(update, context):
    await update.message.reply_text("Flow cancelled. Use /start to begin again.")
    context.user_data.clear()
    return ConversationHandler.END


async def run_webhook(app: Application):
    webhook_url = f"{settings.WEBHOOK_BASE_URL}/webhook"
    await app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query", "inline_query",
                         "chosen_inline_result", "chat_member", "my_chat_member"]
    )
    logger.info(f"Webhook set: {webhook_url}")

    web_app = web.Application()
    web_app.router.add_get("/health", health_check)

    async def telegram_webhook(request):
        from telegram import Update
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    web_app.router.add_post("/webhook", telegram_webhook)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.PORT)
    await site.start()
    logger.info(f"Server on port {settings.PORT}")
    await asyncio.Event().wait()


async def main():
    # ── Init DB FIRST before anything else ────────────────────────────────────
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready. ✅")

    app = build_app()

    # ── Start scheduler ────────────────────────────────────────────────────────
    setup_scheduler(app)

    if settings.DEV:
        logger.info("Starting in POLLING mode (DEV)")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=["message", "callback_query", "inline_query",
                             "chosen_inline_result", "chat_member", "my_chat_member"]
        )
        logger.info("Dr. Crow is flying. 🦅")
        # Keep running until Ctrl+C
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    else:
        await app.initialize()
        await app.start()
        await run_webhook(app)
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())