"""Anonymous Q&A handler."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from config.settings import settings
from middleware.membership import should_respond
from database import queries

logger = logging.getLogger(__name__)

ANON_QUESTION = 0


async def cmd_anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await should_respond(update, context.bot):
        return ConversationHandler.END
    await update.message.reply_text(
        "🕵️ *Anonymous Q&A*\n\n"
        "Your identity will be completely hidden.\n"
        "Type your question and it will be reviewed by admins before publishing:\n\n"
        "_Type /cancel to exit._",
        parse_mode=ParseMode.MARKDOWN
    )
    return ANON_QUESTION


async def _receive_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text.strip()
    user = update.effective_user

    if len(question) < 10:
        await update.message.reply_text("❌ Question too short. Please be more specific.")
        return ANON_QUESTION

    q_id = await queries.insert_anon_question(user.id, question)
    await queries.add_points(user.id, 1, "anon_question")

    # Notify admins
    for admin_id in settings.ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"❓ *New Anonymous Question #{q_id}*\n\n{question}\n\n"
                f"Use /admin → Anon Questions to answer.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

    await update.message.reply_text(
        "✅ Your question has been submitted anonymously!\n"
        "Once answered by the admin, it will be published. 🦅",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


async def _anon_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def handle_anon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin answering an anon question."""
    query = update.callback_query
    if not settings.is_admin(update.effective_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    if query.data.startswith("anon_answer_"):
        q_id = int(query.data.split("_")[-1])
        context.user_data["answering_anon_id"] = q_id
        await query.answer()
        await query.message.reply_text("Send your answer:")


async def handle_admin_anon_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin typing an answer to an anon question."""
    if not settings.is_admin(update.effective_user.id):
        return
    q_id = context.user_data.get("answering_anon_id")
    if not q_id:
        return

    answer = update.message.text.strip()
    await queries.answer_anon_question(q_id, answer)

    # Publish to group
    from database.db import get_pool
    pool_obj = await get_pool()
    async with pool_obj.acquire() as conn:
        q = await conn.fetchrow("SELECT * FROM anon_questions WHERE id = $1", q_id)

    if q:
        try:
            await context.bot.send_message(
                chat_id=settings.GROUP_ID,
                text=f"❓ *Anonymous Question*\n\n{q['question']}\n\n"
                     f"💬 *Answer:*\n{answer}",
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=settings.ALLOWED_TOPIC_IDS[0] if settings.ALLOWED_TOPIC_IDS else None
            )
        except Exception:
            pass

    context.user_data.pop("answering_anon_id", None)
    await update.message.reply_text("✅ Answer published to the group!")


def anon_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("anon", cmd_anon)],
        states={
            ANON_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_question)]
        },
        fallbacks=[CommandHandler("cancel", _anon_cancel)],
        conversation_timeout=120
    )
