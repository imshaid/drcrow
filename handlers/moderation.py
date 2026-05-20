"""
Moderation Engine — anti-bot, anti-spam, foreign group handling.
Runs on ChatMemberUpdated events and message monitoring.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, ChatMemberUpdated
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError
from config.settings import settings
from middleware.membership import handle_member_left
from database import queries

logger = logging.getLogger(__name__)

_message_timestamps: dict = {}
_recent_messages: dict = {}


async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle member join/leave and bot additions."""
    result: ChatMemberUpdated = update.chat_member or update.my_chat_member
    if not result:
        return

    chat = result.chat
    new_member = result.new_chat_member
    old_member = result.old_chat_member

    # ── BOT ADDED TO FOREIGN CHAT ─────────────────────────────────────────────
    if update.my_chat_member and not settings.is_allowed_chat(chat.id):
        logger.info(f"Bot added to foreign chat {chat.id} ({chat.title}). Leaving.")
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "Hey there! 👋\n\n"
                    "Dr. Crow is a private companion built exclusively for "
                    "*Twilight Crows* — a special BSc CSE community.\n\n"
                    "I can't operate outside my flock. Goodbye! 🦅🖤"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError:
            pass
        try:
            await context.bot.leave_chat(chat.id)
        except TelegramError as e:
            logger.warning(f"Could not leave chat {chat.id}: {e}")
        return

    # ── Only process member events from the home group ────────────────────────
    if chat.id != settings.GROUP_ID:
        return

    # ── EXTERNAL BOT ADDED ─────────────────────────────────────────────────────
    if (new_member and new_member.user.is_bot and
            new_member.status == ChatMemberStatus.MEMBER and
            new_member.user.id != context.bot.id):

        added_by      = result.from_user
        bot_name      = new_member.user.username or new_member.user.first_name or "Unknown bot"
        adder_mention = (
            f"@{added_by.username}" if added_by and added_by.username
            else (added_by.first_name if added_by else "Someone")
        )
        topic_id = settings.ALLOWED_TOPIC_IDS[0] if settings.ALLOWED_TOPIC_IDS else None

        logger.warning(f"External bot @{bot_name} added by {adder_mention}. Removing.")

        try:
            await context.bot.ban_chat_member(chat.id, new_member.user.id)
            await context.bot.unban_chat_member(chat.id, new_member.user.id)
        except TelegramError as e:
            logger.error(f"Could not remove bot @{bot_name}: {e}")

        try:
            warn = (
                f"<b>Action:</b> Bot removed\n"
                f"<b>Added by:</b> <code>{adder_mention}</code>\n"
                f"<b>Bot:</b> <code>@{bot_name}</code>\n\n"
                f"External bots are not allowed in this group."
            )
            await context.bot.send_message(
                chat_id=chat.id,
                text=warn,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
        except TelegramError as e:
            logger.warning(f"Could not send bot-removal warn: {e}")

        return

    # ── MEMBER LEFT / REMOVED ─────────────────────────────────────────────────
    left_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.KICKED}
    was_member = old_member and old_member.status in {
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED
    }

    if was_member and new_member and new_member.status in left_statuses:
        user = old_member.user
        logger.info(f"Member left: {user.id} ({user.full_name})")
        await handle_member_left(
            context.bot, user.id,
            getattr(user, "username", "") or "",
            user.full_name or "member"
        )
        return

    # ── NEW MEMBER JOINED ─────────────────────────────────────────────────────
    if new_member and new_member.status == ChatMemberStatus.MEMBER:
        user = new_member.user
        if not user.is_bot:
            await queries.upsert_user(
                user.id,
                getattr(user, "username", "") or "",
                user.full_name or ""
            )
            logger.info(f"New member registered: {user.id} ({user.full_name})")


async def check_message_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check incoming group messages for spam violations."""
    msg = update.effective_message
    user = update.effective_user

    if not msg or not user or user.is_bot:
        return
    if settings.is_admin(user.id):
        return
    if msg.chat.id != settings.GROUP_ID:
        return

    now = datetime.now(timezone.utc)
    user_id = user.id

    timestamps = _message_timestamps.get(user_id, [])
    timestamps = [t for t in timestamps if (now - t).total_seconds() < 10]
    timestamps.append(now)
    _message_timestamps[user_id] = timestamps

    if len(timestamps) >= 5:
        await _handle_spam_violation(
            update, context, user_id, "flood",
            mute_minutes=10,
            reason="Message flood (5+ msgs in 10s)"
        )
        try:
            await msg.delete()
        except TelegramError:
            pass
        return

    last = _recent_messages.get(user_id)
    if last:
        last_text, last_time = last
        if (msg.text == last_text and
                (now - last_time).total_seconds() < 60):
            await _handle_spam_violation(
                update, context, user_id, "repeat",
                reason="Repeated message within 60s"
            )
            try:
                await msg.delete()
            except TelegramError:
                pass
            return

    if msg.text:
        _recent_messages[user_id] = (msg.text, now)

    if msg.entities:
        url_count = sum(1 for e in msg.entities if e.type in ("url", "text_link"))
        if url_count >= 3:
            await _handle_spam_violation(
                update, context, user_id, "spam",
                reason="Link spam (3+ URLs in one message)"
            )
            try:
                await msg.delete()
            except TelegramError:
                pass
            return

    if msg.document or msg.video or msg.audio:
        topic_id = msg.message_thread_id
        if topic_id and topic_id not in settings.ALLOWED_TOPIC_IDS:
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat_id=settings.GROUP_ID,
                    text=f"📁 Files should be shared via @{context.bot.username} → Upload Resource. "
                         f"Keeping the group organized! 🦅",
                    message_thread_id=topic_id
                )
            except TelegramError:
                pass


async def _handle_spam_violation(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    user_id: int, violation_type: str,
    mute_minutes: int = 0, reason: str = ""
):
    await queries.add_flag(user_id, "spam", reason)
    await queries.add_stars(user_id, -2, "spam_flag")

    if mute_minutes > 0:
        until = datetime.now(timezone.utc) + timedelta(minutes=mute_minutes)
        await queries.set_muted_until(user_id, until)
        try:
            from telegram import ChatPermissions
            await context.bot.restrict_chat_member(
                chat_id=settings.GROUP_ID,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
        except TelegramError as e:
            logger.warning(f"Could not mute {user_id}: {e}")

    user = update.effective_user
    db_user = await queries.get_user(user_id)
    if not db_user or not db_user["warned_today"]:
        try:
            msg = update.effective_message
            warn_text = f"⚠️ @{user.username or user.first_name}, {reason}."
            if mute_minutes > 0:
                warn_text += f" You've been muted for {mute_minutes} minutes."
            await context.bot.send_message(
                chat_id=settings.GROUP_ID,
                text=warn_text,
                message_thread_id=msg.message_thread_id if msg else None
            )
        except TelegramError:
            pass

    flags = await queries.get_flag_counts(user_id)
    if flags.get("spam_flags", 0) >= 4:
        await _permanent_remove(context, settings.GROUP_ID, user_id, "Spam threshold exceeded")


async def _permanent_remove(context, chat_id: int, user_id: int, reason: str):
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        logger.info(f"Permanently removed user {user_id}: {reason}")
    except TelegramError as e:
        logger.error(f"Could not permanently remove {user_id}: {e}")

    try:
        await context.bot.send_message(
            user_id,
            f"😔 You have been permanently removed from *Twilight Crows*.\n"
            f"Reason: {reason}\n\n"
            f"If you believe this is an error, contact the group admin.",
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError:
        pass