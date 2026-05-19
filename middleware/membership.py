"""
Membership Gate — The first wall every update hits.
Fail-closed: if Telegram API throws, deny access.
Cache: 5 minutes. Stores left_at for grace window tracking.
"""

import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, Bot
from telegram.error import TelegramError
from telegram.constants import ChatMemberStatus
from config.settings import settings
from database import queries

logger = logging.getLogger(__name__)


async def check_membership(bot: Bot, user_id: int) -> bool:
    """
    Returns True if user is allowed to use the bot.
    Checks cache first, then Telegram API.
    Handles 4-hour grace window for ex-members.
    """
    now = datetime.now(timezone.utc)

    # Check cache
    cached = await queries.get_membership_cache(user_id)
    if cached:
        cache_age = (now - cached["cached_at"].replace(tzinfo=timezone.utc)).total_seconds()
        if cache_age < settings.MEMBERSHIP_CACHE_TTL:
            # Cache is fresh — use it
            if cached["is_member"]:
                return True
            # Ex-member: check 4-hour grace
            if cached["left_at"]:
                left_at = cached["left_at"].replace(tzinfo=timezone.utc)
                elapsed = (now - left_at).total_seconds()
                if elapsed < settings.EX_MEMBER_GRACE_SECONDS:
                    return True
            return False

    # Cache miss or stale — call Telegram API
    try:
        member = await bot.get_chat_member(
            chat_id=settings.GROUP_ID,
            user_id=user_id
        )
        active_statuses = {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,  # restricted but still in group
        }
        is_member = member.status in active_statuses

        if is_member:
            await queries.set_membership_cache(user_id, True, left_at=None)
            # Also update DB user record
            await queries.upsert_user(
                user_id,
                getattr(member.user, "username", None) or "",
                member.user.full_name or ""
            )
            return True
        else:
            # Not a member — check if they were recently (grace window)
            user_record = await queries.get_user(user_id)
            left_at = None
            if user_record and user_record["left_at"]:
                left_at = user_record["left_at"].replace(tzinfo=timezone.utc)

            await queries.set_membership_cache(user_id, False, left_at=left_at)

            if left_at:
                elapsed = (now - left_at).total_seconds()
                if elapsed < settings.EX_MEMBER_GRACE_SECONDS:
                    return True

            return False

    except TelegramError as e:
        logger.warning(f"Membership check failed for {user_id}: {e}. Denying (fail-closed).")
        return False


async def is_in_grace_window(user_id: int) -> bool:
    """Check if user is in the 4-hour ex-member grace window."""
    now = datetime.now(timezone.utc)
    user = await queries.get_user(user_id)
    if not user or not user["left_at"]:
        return False
    left_at = user["left_at"].replace(tzinfo=timezone.utc)
    elapsed = (now - left_at).total_seconds()
    return elapsed < settings.EX_MEMBER_GRACE_SECONDS


async def handle_member_left(bot: Bot, user_id: int, username: str, full_name: str):
    """
    Called when a member leaves the group.
    1. Mark left_at in DB
    2. Update cache
    3. DM them the rejoin message with group link
    """
    now = datetime.now(timezone.utc)
    await queries.mark_user_left(user_id)
    await queries.set_membership_cache(user_id, False, left_at=now)

    try:
        grace_hours = settings.EX_MEMBER_GRACE_SECONDS // 3600
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"Hey {full_name}! 👋\n\n"
                f"Looks like you've left *Twilight Crows*. We'll miss you! 🦅\n\n"
                f"You can still use Dr. Crow for the next *{grace_hours} hours*. "
                f"After that, access will be restricted to active members only.\n\n"
                f"Want to come back? Here's the group link:\n"
                f"👉 t.me/+-m-ji4z7LRIzZWQ9\n\n"
                f"The crow always remembers its flock. 🖤"
            ),
            parse_mode="Markdown"
        )
    except TelegramError as e:
        logger.warning(f"Could not DM left member {user_id}: {e}")


async def send_denial_message(bot, chat_id: int):
    """Send the warm denial message to non-members."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=settings.DENIAL_MESSAGE,
            parse_mode="Markdown"
        )
    except TelegramError as e:
        logger.warning(f"Could not send denial to {chat_id}: {e}")


async def should_respond(update: Update, bot: Bot) -> bool:
    """
    Master gate. Returns True if bot should process this update.
    Handles:
    - DM from non-member → send denial once per day, return False
    - Group topic not in allowed list → silent, return False
    - Foreign group/channel → handled separately in moderation
    - Inline query from non-member → handled in inline handler
    """
    user = update.effective_user
    if not user or user.is_bot:
        return False

    # Admin always passes
    if settings.is_admin(user.id):
        return True

    # Callback queries from group — skip topic check, just verify membership
    # (inline result buttons can be clicked from any chat)
    if update.callback_query and update.effective_chat and update.effective_chat.id == settings.GROUP_ID:
        return await check_membership(bot, user.id)

    # Check group topic restriction for regular messages
    message = update.effective_message
    if message and update.effective_chat and update.effective_chat.id == settings.GROUP_ID:
        topic_id = message.message_thread_id
        if topic_id not in settings.ALLOWED_TOPIC_IDS:
            return False  # Silent ignore for wrong topics
        # In the right topic, still check membership
        allowed = await check_membership(bot, user.id)
        return allowed

    # DM interaction
    if update.effective_chat and update.effective_chat.type == "private":
        allowed = await check_membership(bot, user.id)
        if not allowed:
            # Check if we warned them today
            db_user = await queries.get_user(user.id)
            if db_user and db_user["warned_today"]:
                return False  # Already warned today, silent
            # Send denial once per day
            await send_denial_message(bot, user.id)
            # Mark warned
            from database.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO users (user_id, username, full_name, warned_today, last_warned_at)
                    VALUES ($1, $2, $3, TRUE, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                        SET warned_today = TRUE, last_warned_at = NOW()
                """, user.id,
                    getattr(user, "username", None) or "",
                    user.full_name or "")
            return False
        return True

    return False