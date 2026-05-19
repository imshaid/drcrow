"""
Central configuration. All values loaded from environment variables.
Never hardcode tokens or IDs in source.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


def _parse_int_list(val: str) -> List[int]:
    if not val:
        return []
    return [int(x.strip()) for x in val.split(",") if x.strip()]


@dataclass
class Settings:
    # Core
    BOT_TOKEN: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    GROUP_ID: int = field(default_factory=lambda: int(os.environ["GROUP_ID"]))
    BOT_USERNAME: str = field(default_factory=lambda: os.environ.get("BOT_USERNAME", "drcrow_bot"))
    ADMIN_IDS: List[int] = field(
        default_factory=lambda: _parse_int_list(os.environ.get("ADMIN_IDS", ""))
    )

    # Storage channel — bot must stay here even though it's not the main group
    STORAGE_CHANNEL_ID: Optional[int] = field(
        default_factory=lambda: int(os.environ["STORAGE_CHANNEL_ID"]) if os.environ.get("STORAGE_CHANNEL_ID") else None
    )

    # Allowed topic IDs in the group where bot responds
    ALLOWED_TOPIC_IDS: List[int] = field(
        default_factory=lambda: _parse_int_list(os.environ.get("ALLOWED_TOPIC_IDS", "2"))
    )

    # Database
    DATABASE_URL: str = field(default_factory=lambda: os.environ["DATABASE_URL"])

    # imgBB image hosting
    IMGBB_API_KEY: str = field(default_factory=lambda: os.environ.get("IMGBB_API_KEY", ""))

    # OpenRouter LLM
    OPENROUTER_API_KEY: str = field(default_factory=lambda: os.environ["OPENROUTER_API_KEY"])
    GEMINI_API_KEY: str    = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1/chat/completions"

    LLM_MODELS: List[str] = field(default_factory=lambda: [
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemma-4-26b-a4b-it:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "openai/gpt-oss-20b:free",
        "minimax/minimax-m2.5:free",
        "qwen/qwen3-coder:free",
        "mistralai/mistral-7b-instruct:free",
        "nvidia/nemotron-nano-9b-v2:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
    ])

    # Deployment
    WEBHOOK_BASE_URL: str = field(default_factory=lambda: os.environ.get("WEBHOOK_BASE_URL", ""))
    PORT: int = field(default_factory=lambda: int(os.environ.get("PORT", 8080)))
    DEV: bool = field(default_factory=lambda: os.environ.get("DEV", "false").lower() == "true")

    # Grace period for ex-members (in seconds)
    EX_MEMBER_GRACE_SECONDS: int = 4 * 60 * 60  # 4 hours

    # Membership cache TTL (seconds)
    MEMBERSHIP_CACHE_TTL: int = 300  # 5 minutes

    # Telegram limits
    MAX_INLINE_RESULTS: int = 50
    FILE_FLOOD_DELAY: float = 0.3  # seconds between file sends

    # Denial message
    DENIAL_MESSAGE: str = (
        "Hey there! 👋\n\n"
        "Dr. Crow is a private academic companion, built exclusively for the "
        "*Twilight Crows* — a close-knit BSc CSE community.\n\n"
        "This bot isn't open to the public. It's a signature of something special. 🦅\n\n"
        "If you're a Twilight Crow, make sure you're a member of the group and try again."
    )

    INLINE_DENIAL_TITLE: str = "🔒 Access Restricted"
    INLINE_DENIAL_DESCRIPTION: str = (
        "Dr. Crow serves Twilight Crows members only. "
        "You must be a member of the group to use this bot."
    )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.ADMIN_IDS

    def is_allowed_chat(self, chat_id: int) -> bool:
        """Returns True if bot is allowed to stay in this chat."""
        if chat_id == self.GROUP_ID:
            return True
        if self.STORAGE_CHANNEL_ID and chat_id == self.STORAGE_CHANNEL_ID:
            return True
        return False


settings = Settings()