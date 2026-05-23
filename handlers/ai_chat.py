"""
ai_chat.py — Dr. Crow AI Chat Handler (Bot Side)

Flow:
  Member taps [🤖 Ask AI] → welcome message
  Member types question → DB search + Gemini → response + resource buttons
  Member taps [✖ Done] or /endai or 30min idle → end

Features:
  - Multi-turn conversation with history
  - DB resource search (Tier 1)
  - File explanation via Gemini Vision (Tier 2)
  - Response caching in Supabase
  - Model rotation when quota hits
  - Exam period extended history TTL
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape as h
from typing import Optional

import aiohttp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CommandHandler, MessageHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode, ChatAction

from config.settings import settings
from database.db import get_pool

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

# ── States ────────────────────────────────────────────────────────────────────
AI_CHAT = 0

# ── Model rotation list ───────────────────────────────────────────────────────
# When one hits quota, next is tried automatically
# Model rotation — free tier, verified May 2026
GEMINI_MODELS = [
    "gemini-3.1-flash-lite",   # Primary — best quality/speed, 500 RPD
    "gemini-2.5-flash",        # Secondary — stable, 10K RPD
    "gemma-4-26b-a4b-it",      # MoE fallback — fast, 1.5K RPD
    "gemma-4-31b-it",          # Dense fallback — highest quality, 1.5K RPD
]

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Dr. Crow, the AI assistant for the Twilight Crows community — a group of BSc CSE students at DIU, Bangladesh.

Important identity rules:
- You serve Twilight Crows members specifically — not all DIU CSE students
- Never claim to be "the official DIU assistant" or represent all DIU CSE students
- Never say "our community includes all DIU CSE students" — Twilight Crows is a private group
- If asked who you are, say you assist Twilight Crows members with academic work
- Keep identity answers short — don't over-explain your purpose unprompted

Your role:
- Answer academic questions about CSE topics clearly and accurately
- Help students understand concepts, theories, and problems
- Respond in the same language the student uses (Bangla or English)
- Be concise but thorough — get to the point, avoid filler phrases
- Use examples and analogies when explaining complex concepts
- For code, always explain what the code does after showing it
- If uncertain, say so honestly — never fabricate or guess facts
- Never mention UIDs or internal system details to members

Math notation rules (strictly follow):
- Never use LaTeX syntax like \\frac{}{}, \\Delta, $$...$$, $...$
- Write fractions as (numerator)/(denominator) — e.g. (x - x0)/(h)
- Use Unicode symbols directly: Δ ∇ Σ ∫ √ × ∞ α β γ θ λ π
- Superscripts: use ² ³ or write as x^2, x^n
- Subscripts: write as x_0, x_n
- Keep math expressions readable as plain text

Response style:
- Short/simple questions → short focused answers, no unnecessary structure
- Complex topics → structured with bold headers and numbered steps
- Math/algorithms → show working step by step with readable notation
- Code → clean code block + brief explanation of what it does

Resource mentioning rules (CRITICAL — strictly follow):
- ONLY mention resources that are explicitly listed in the context above
- NEVER invent resource names, course codes, or descriptions not in the list
- NEVER say "we have resources on X" unless resources are actually listed
- Only say a resource is "tagged with" a topic — never claim depth or completeness
- Safe phrases: "tagged with", "may contain", "worth checking"
- Reference by number only (Resource 1, Resource 2) — never show uid
- If NO resources are listed: answer from your knowledge, say nothing about resources

Tone: Friendly, direct, knowledgeable — like a senior student or TA who respects the reader's time."""

# ── Keyboards ─────────────────────────────────────────────────────────────────

def _ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖ End Chat", callback_data="ai_end"),
        InlineKeyboardButton("🆕 New Chat", callback_data="ai_new"),
    ]])


def _history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Continue", callback_data="ai_continue"),
        InlineKeyboardButton("🆕 New Chat", callback_data="ai_new"),
    ]])


# ── History TTL ───────────────────────────────────────────────────────────────

async def _get_history_ttl_hours() -> int:
    """
    Normal: 24 hours
    Exam period (2 days before first exam → last exam day): extended
    """
    try:
        from database.queries import get_active_exam_events
        exams = await get_active_exam_events()
        if not exams:
            return 24

        now = datetime.now(timezone.utc).date()
        earliest = min(e["exam_date"] for e in exams) - timedelta(days=2)
        latest   = max(e["exam_date"] for e in exams)

        if earliest <= now <= latest:
            days_left = (latest - now).days + 1
            return max(days_left * 24 + 24, 48)
    except Exception as e:
        logger.warning("TTL check failed: %s", e)
    return 24


# ── DB: history ───────────────────────────────────────────────────────────────

async def _load_history(user_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history, updated_at FROM ai_chat_history WHERE user_id = $1",
            user_id
        )
    if not row:
        return []
    ttl = await _get_history_ttl_hours()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl)
    updated = row["updated_at"]
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if updated < cutoff:
        await _clear_history(user_id)
        return []
    try:
        return json.loads(row["history"]) if isinstance(row["history"], str) else (row["history"] or [])
    except Exception:
        return []


async def _save_history(user_id: int, history: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ai_chat_history (user_id, history, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET history = $2, updated_at = NOW()
        """, user_id, json.dumps(history))


async def _clear_history(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM ai_chat_history WHERE user_id = $1", user_id)


# ── DB: cache ─────────────────────────────────────────────────────────────────

def _cache_key(question: str) -> str:
    return hashlib.sha256(question.strip().lower().encode()).hexdigest()[:16]


async def _load_cache(question: str) -> Optional[dict]:
    key = _cache_key(question)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT response, resources_json FROM ai_response_cache WHERE cache_key = $1",
            key
        )
    if not row:
        return None
    response = row["response"]
    # Discard incomplete cached responses
    if not _is_complete_response(response):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ai_response_cache WHERE cache_key = $1", key
            )
        return None
    return {
        "response":  response,
        "resources": json.loads(row["resources_json"]) if row["resources_json"] else [],
    }


async def _save_cache(question: str, response: str, resources: list):
    key = _cache_key(question)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ai_response_cache (cache_key, question, response, resources_json, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (cache_key) DO UPDATE
            SET response = $3, resources_json = $4, hit_count = ai_response_cache.hit_count + 1
        """, key, question[:500], response, json.dumps(resources))


# ── Resource search ───────────────────────────────────────────────────────────

def _is_complete_response(text: str) -> bool:
    """
    Check if a response looks complete.
    Incomplete responses often end mid-sentence or mid-word.
    """
    if not text or len(text.strip()) < 50:
        return False
    t = text.strip()
    # Must end with sentence-ending punctuation or closing bracket
    complete_endings = ('.', '!', '?', '```', ')', '}', ']', '।', ':', '"', "'")
    return any(t.endswith(e) for e in complete_endings)


def _extract_search_query(question: str) -> str:
    """
    Extract 1-2 meaningful keywords from a question for DB search.
    Removes stop words so AND-per-token search doesn't fail on common words.
    """
    STOP_WORDS = {
        "what", "is", "are", "was", "were", "the", "a", "an", "of", "in",
        "on", "at", "to", "for", "and", "or", "but", "how", "why", "when",
        "where", "who", "which", "this", "that", "it", "its", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "about", "with", "from",
        "actually", "really", "please", "explain", "tell", "me", "give",
        "write", "show", "provide", "sample", "example", "define", "describe",
        "difference", "between", "vs", "compare", "some", "any", "all", "my",
        "your", "their", "our", "now", "also", "just", "by", "as", "so",
        "if", "then", "than", "more", "most", "very", "too", "i", "we",
        "you", "they", "he", "she", "means", "mean", "many", "types", "type",
        "make", "create", "solution", "math", "code", "number", "using",
        "use", "used", "way", "ways", "method", "methods", "approach",
        "simple", "basic", "full", "complete", "good", "best", "proper",
        "resources", "resource", "provide", "get", "find", "need", "want", "books", "notes", "book", "note", "solve", "solves", "there", "here", "yes", "no", "not", "any", "something", "anything", "everything", "nothing", "much", "many", "few", "lot", "lots", "number", "have", "has", "had", "got", "give", "get", "put", "set",
        "send", "list", "related", "available", "topic", "topics",
        "information", "info", "details", "help", "know", "about",
    }
    SHORT_ALLOW = {"ip", "os", "db", "ai", "ml", "dl", "oop", "dsa", "cpu", "gpu", "nat", "dns", "tcp", "udp", "api"}
    _punct = "?.,!:;'()"
    tokens = [
        w.lower().strip(_punct)
        for w in question.split()
        if w.lower().strip(_punct) not in STOP_WORDS
        and (len(w.strip(_punct)) > 2 or w.lower().strip(_punct) in SHORT_ALLOW)
    ]
    # Return max 2 keywords — enough for meaningful AND match
    return " ".join(tokens[:2]) if tokens else question.strip()


async def _search_resources(question: str) -> list:
    """
    Search DB for relevant resources based on question.
    Searches ALL semesters — not limited to current semester.
    Extracts keywords first to avoid AND-per-token false negatives.
    """
    results = []
    q = _extract_search_query(question)
    if not q:
        q = question.strip()

    def _extract_tags(r, key="tags") -> list:
        tags = r.get(key) or []
        if isinstance(tags, str):
            import json as _j
            try: tags = _j.loads(tags)
            except: tags = []
        return [str(t) for t in tags[:6]]

    try:
        from database.note_queries import search_notes_all
        for r in await search_notes_all(q, limit=3):
            results.append({
                "type": "Note", "title": r.get("title", ""),
                "uid": r.get("uid", ""), "course": r.get("course_code", ""),
                "tags": _extract_tags(r)
            })
    except Exception as e:
        logger.debug("Note search error: %s", e)

    try:
        from database.book_queries import search_books_all
        for r in await search_books_all(q, limit=2):
            results.append({
                "type": "Book", "title": r.get("title", ""),
                "uid": r.get("uid", ""), "course": r.get("course_codes", ""),
                "tags": _extract_tags(r)
            })
    except Exception as e:
        logger.debug("Book search error: %s", e)

    try:
        from database.solve_queries import search_solves_all
        for r in await search_solves_all(q, limit=2):
            results.append({
                "type": "Solve", "title": r.get("title", ""),
                "uid": r.get("uid", ""), "course": r.get("course_code", ""),
                "tags": _extract_tags(r)
            })
    except Exception as e:
        logger.debug("Solve search error: %s", e)

    try:
        from database.psq_queries import search_psqs
        for r in await search_psqs(q, limit=2):
            results.append({
                "type": "PSQ", "title": r.get("title") or "Past Questions",
                "uid": r.get("uid", ""), "course": "",
                "tags": _extract_tags(r)
            })
    except Exception as e:
        logger.debug("PSQ search error: %s", e)

    return results[:5]  # max 5 resources


# ── Gemini API call ───────────────────────────────────────────────────────────

async def _send_draft(bot, chat_id: int, text: str):
    """
    Send streaming draft via sendMessageDraft (Bot API 9.5+).
    Fire-and-forget — doesn't wait for response to keep streaming fast.
    """
    try:
        url = f"https://api.telegram.org/bot{bot.token}/sendMessageDraft"
        # Create task — don't await, keep processing chunks
        asyncio.create_task(_post_draft(url, chat_id, text))
    except Exception:
        pass


async def _post_draft(url: str, chat_id: int, text: str):
    """Actual HTTP POST for draft — runs as background task."""
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                url,
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception:
        pass


async def _call_gemini_stream(
    messages: list,
    resources: list,
    thinking_msg,
    model_idx: int = 0,
    image_data: str = None,
    keyboard=None
) -> tuple[str, int]:
    """
    Call Gemini streaming API.
    Uses sendMessageDraft for native streaming (Bot API 9.5+).
    Falls back to editMessageText every 0.8s.
    Returns (full_response_text, model_idx_used).
    """
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        return "AI feature is not configured.", model_idx

    # Build resource context — safe language, no overclaiming
    resource_ctx = ""
    if resources:
        resource_ctx = "\n\nAvailable resources (from our community database):\n"
        for i, r in enumerate(resources, 1):
            resource_ctx += f"{i}. {r['type']}: {r['title']}"
            if r.get("course"):
                resource_ctx += f" | Course: {r['course']}"
            if r.get("tags"):
                resource_ctx += f" | Tagged: {', '.join(r['tags'])}"
            resource_ctx += f" | uid:{r['uid']}\n"
        resource_ctx += (
            "\nIMPORTANT resource rules:"
            "\n- Tags only indicate topic presence, NOT content depth or quality"
            "\n- Use safe language: 'tagged with', 'may contain', 'worth checking'"
            "\n- Never claim a resource covers something 'completely' or 'in detail'"
            "\n- Reference resources by number (e.g. Resource 1) — never show uid"
            "\n- Only mention a resource if it is genuinely relevant to the query"
            "\n- NEVER invent or assume resource types/content not listed above"
        )
    else:
        resource_ctx = (
            "\n\nNo resources were found in the database for this query."
            "\n- Do NOT invent, assume, or describe any resources"
            "\n- Do NOT say 'there may be resources' or 'you can search for'"
            "\n- Simply answer the question from your own knowledge only"
        )

    system = SYSTEM_PROMPT + resource_ctx

    contents = []
    for i, msg in enumerate(messages):
        role = "user" if msg["role"] == "user" else "model"
        parts = [{"text": msg["content"]}]
        if image_data and i == len(messages) - 1 and role == "user":
            parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": image_data}
            })
        contents.append({"role": role, "parts": parts})

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }

    for i in range(model_idx, len(GEMINI_MODELS)):
        model = GEMINI_MODELS[i]
        url   = f"{GEMINI_BASE}/{model}:streamGenerateContent?alt=sse&key={api_key}"
        try:
            full_text      = ""
            last_edit_time = 0
            EDIT_INTERVAL  = 0.3  # seconds between draft updates (faster, no edit rate limit)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status in (429, 503):
                        logger.warning("Model %s quota hit, rotating...", model)
                        continue
                    if resp.status == 404:
                        logger.warning("Model %s not found, skipping...", model)
                        continue
                    if resp.status != 200:
                        logger.error("Gemini stream %s error %d", model, resp.status)
                        continue

                    async for line in resp.content:
                        line = line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data  = json.loads(data_str)
                            chunk = (
                                data.get("candidates", [{}])[0]
                                .get("content", {})
                                .get("parts", [{}])[0]
                                .get("text", "")
                            )
                            if chunk:
                                full_text += chunk
                                now = asyncio.get_event_loop().time()
                                if now - last_edit_time >= EDIT_INTERVAL and full_text.strip():
                                    # sendMessageDraft only — no editMessageText during stream
                                    # This gives native smooth animation without lag
                                    await _send_draft(
                                        thinking_msg._bot,
                                        thinking_msg.chat_id,
                                        f"🤖 {full_text}"
                                    )
                                    last_edit_time = now
                        except json.JSONDecodeError:
                            continue

            if full_text.strip():
                # Final: delete draft bubble, send proper formatted message
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass
                # Try HTML first, fallback to plain text if parse error
                html_text = _md_to_html(full_text)
                sent = False
                try:
                    await thinking_msg._bot.send_message(
                        chat_id=thinking_msg.chat_id,
                        text=f"🤖 {html_text}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )
                    sent = True
                except Exception as e:
                    logger.warning("HTML send failed (%s), trying plain text", e)
                if not sent:
                    try:
                        # Strip all HTML tags for plain text fallback
                        import re as _re
                        plain = _re.sub(r'<[^>]+>', '', html_text)
                        await thinking_msg._bot.send_message(
                            chat_id=thinking_msg.chat_id,
                            text=f"🤖 {plain}",
                            reply_markup=keyboard
                        )
                        sent = True
                    except Exception:
                        pass
                if not sent:
                    # Last resort: edit the thinking_msg
                    try:
                        await thinking_msg.edit_text(
                            f"🤖 {full_text[:4000]}",
                            reply_markup=keyboard
                        )
                    except Exception:
                        pass
                return full_text.strip(), i

        except asyncio.TimeoutError:
            logger.warning("Gemini stream %s timeout, rotating...", model)
            continue
        except Exception as e:
            logger.error("Gemini stream %s exception: %s", model, e)
            continue

    return "Sorry, AI is temporarily unavailable. Please try again later.", model_idx


async def _call_gemini(
    messages: list,
    resources: list,
    model_idx: int = 0,
    image_data: str = None
) -> tuple[str, int]:
    """
    Non-streaming fallback — kept for cache responses.
    Returns (response_text, model_idx_used).
    """
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        return "AI feature is not configured.", model_idx

    # Build resource context
    resource_ctx = ""
    if resources:
        resource_ctx = "\n\nAvailable resources in our database:\n"
        for r in resources:
            resource_ctx += f"- {r['type']}: {r['title']}"
            if r.get("course"):
                resource_ctx += f" ({r['course']})"
            resource_ctx += f" [uid:{r['uid']}]\n"
        resource_ctx += "\nMention these naturally in your response if relevant. Never show the uid to the user."

    system = SYSTEM_PROMPT + resource_ctx

    # Build Gemini contents format
    contents = []
    for i, msg in enumerate(messages):
        role = "user" if msg["role"] == "user" else "model"
        parts = [{"text": msg["content"]}]
        # Attach image to the last user message if provided
        if image_data and i == len(messages) - 1 and role == "user":
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": image_data
                }
            })
        contents.append({"role": role, "parts": parts})

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        }
    }

    # Try models in rotation
    for i in range(model_idx, len(GEMINI_MODELS)):
        model = GEMINI_MODELS[i]
        url   = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status in (429, 503):
                        logger.warning("Model %s quota hit, rotating...", model)
                        continue
                    if resp.status == 404:
                        logger.warning("Model %s not found, skipping...", model)
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error("Gemini %s error %d: %s", model, resp.status, text[:200])
                        continue
                    data = await resp.json()
                    content = (
                        data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                    if content:
                        return content.strip(), i
        except asyncio.TimeoutError:
            logger.warning("Gemini %s timeout, rotating...", model)
            continue
        except Exception as e:
            logger.error("Gemini %s exception: %s", model, e)
            continue

    return "Sorry, AI is temporarily unavailable. Please try again later.", model_idx


# ── Resource buttons ──────────────────────────────────────────────────────────

def _md_to_html(text: str) -> str:
    """
    Convert Gemini Markdown to Telegram HTML.
    Properly handles: code blocks, bold, italic, strikethrough,
    headers, blockquotes. Strips LaTeX math notation.
    """
    import re

    # ── Step 1: Extract fenced code blocks BEFORE any processing ─────────────
    code_blocks = {}
    counter = [0]

    def _store_code(m):
        key = f"\x00CODE{counter[0]}\x00"
        counter[0] += 1
        lang = (m.group(1) or "").strip()
        # Escape HTML inside code but keep content intact
        from html import escape as _he2
        code = _he2(m.group(2).strip())
        if lang:
            code_blocks[key] = f'<pre><code>{code}</code></pre>'
        else:
            code_blocks[key] = f'<pre><code>{code}</code></pre>'
        return key

    text = re.sub(r'```(\w*)\n?(.*?)```', _store_code, text, flags=re.DOTALL)

    # ── Step 2: Convert LaTeX math to readable Unicode ─────────────────────────
    # Replace common LaTeX patterns with readable equivalents
    def _latex_to_readable(m):
        expr = m.group(1).strip()
        # \frac{a}{b} → (a)/(b)
        expr = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', expr)
        # \Delta → Δ
        expr = expr.replace(r'\Delta', 'Δ')
        expr = expr.replace(r'\delta', 'δ')
        # \nabla → ∇
        expr = expr.replace(r'\nabla', '∇')
        # \sqrt → √
        expr = re.sub(r'\\sqrt\{([^{}]+)\}', r'√(\1)', expr)
        # \sum → Σ
        expr = expr.replace(r'\sum', 'Σ')
        # \int → ∫
        expr = expr.replace(r'\int', '∫')
        # \infty → ∞
        expr = expr.replace(r'\infty', '∞')
        # \alpha β γ etc
        greek = {
            r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\theta': 'θ',
            r'\lambda': 'λ', r'\mu': 'μ', r'\pi': 'π', r'\sigma': 'σ',
            r'\tau': 'τ', r'\phi': 'φ', r'\omega': 'ω', r'\epsilon': 'ε',
        }
        for k, v in greek.items():
            expr = expr.replace(k, v)
        # _n → subscript (keep readable: x_n → x_n)
        # ^2 → superscript: x^2 → x²
        expr = re.sub(r'\^2\b', '²', expr)
        expr = re.sub(r'\^3\b', '³', expr)
        expr = re.sub(r'\^n\b', 'ⁿ', expr)
        # \times → ×
        expr = expr.replace(r'\times', '×')
        # \dots → ...
        expr = expr.replace(r'\dots', '...')
        # \ldots → ...
        expr = expr.replace(r'\ldots', '...')
        # Remove remaining backslash commands
        expr = re.sub(r'\\[a-zA-Z]+', '', expr)
        return expr.strip()

    # Block math: $$...$$
    text = re.sub(r'\$\$(.+?)\$\$', _latex_to_readable, text, flags=re.DOTALL)
    # Inline math: $...$
    text = re.sub(r'\$([^\$\n]+?)\$', _latex_to_readable, text)

    # ── Step 3: Escape HTML (& < >) but NOT quotes ────────────────────────────
    # Use manual replace to avoid &#x27; and &quot; issues
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')

    # ── Step 4: Restore code blocks ───────────────────────────────────────────
    for key, val in code_blocks.items():
        # Key was NOT HTML-escaped, so replace directly
        text = text.replace(key, val)

    # ── Step 5: Inline code: `code` → <code>code</code> ──────────────────────
    def _inline_code(m):
        code = m.group(1).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return f'<code>{code}</code>'
    text = re.sub(r'`([^`\n]+)`', _inline_code, text)

    # ── Step 6: Bold+Italic: ***text*** ───────────────────────────────────────
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text, flags=re.DOTALL)

    # ── Step 7: Bold: **text** ────────────────────────────────────────────────
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)

    # ── Step 8: Italic: *text* (not **) ──────────────────────────────────────
    text = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)

    # ── Step 9: Strikethrough: ~~text~~ ──────────────────────────────────────
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text, flags=re.DOTALL)

    # ── Step 10: Headers: # Header → <b>Header</b> ───────────────────────────
    text = re.sub(r'^#{1,6} (.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # ── Step 11: Horizontal rules ─────────────────────────────────────────────
    text = re.sub(r'^(?:---|\*\*\*|___)$', '─' * 20, text, flags=re.MULTILINE)

    # ── Step 12: Cleanup extra blank lines ────────────────────────────────────
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """
    Split long text into chunks ≤ limit characters.
    Tries to split on double newlines, then single newlines, then by limit.
    Preserves <pre><code> blocks intact — never splits inside a code block.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while len(remaining) > limit:
        # Find split point — prefer double newline before limit
        split_at = remaining.rfind('\n\n', 0, limit)
        if split_at == -1:
            # Try single newline
            split_at = remaining.rfind('\n', 0, limit)
        if split_at == -1:
            # Hard split at limit
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return [c for c in chunks if c]


def _resource_buttons(resources: list) -> Optional[InlineKeyboardMarkup]:
    if not resources:
        return None
    TYPE_EMOJI = {"Note": "📝", "Book": "📚", "Solve": "✅", "PSQ": "📋"}
    rows = []
    for i, r in enumerate(resources, 1):
        emoji = TYPE_EMOJI.get(r["type"], "📄")
        label = f"[{i}] {emoji} {r['title'][:28]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"get_res_{r['uid']}")])
    rows.append([
        InlineKeyboardButton("✖ End Chat", callback_data="ai_end"),
        InlineKeyboardButton("🆕 New Chat", callback_data="ai_new"),
    ])
    return InlineKeyboardMarkup(rows)


# ── Entry ──────────────────────────────────────────────────────────────────────

async def ai_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Member taps [🤖 Ask AI]"""
    user = update.effective_user
    context.user_data["_in_conversation"] = True

    # Check for existing history
    history = await _load_history(user.id)
    if history:
        last_q = next((m["content"][:50] for m in reversed(history) if m["role"] == "user"), "")
        ttl    = await _get_history_ttl_hours()
        await update.message.reply_text(
            f"🤖 <b>Dr. Crow AI</b>\n\n"
            f"Previous conversation found.\n"
            f"Last topic: <i>{h(last_q)}...</i>\n\n"
            f"History expires in <b>{ttl}h</b>.",
            parse_mode=HTML,
            reply_markup=_history_keyboard()
        )
        return AI_CHAT

    # Fresh start
    context.user_data["ai_history"] = []
    context.user_data["ai_model_idx"] = 0
    await update.message.reply_text(
        "🤖 <b>Dr. Crow AI</b>\n\n"
        "Ask me anything academic.\n"
        "Courses, topics, concepts — I'm here to help.\n\n"
        "<i>Tap Done or type /endai to end the chat.</i>",
        parse_mode=HTML,
        reply_markup=_ai_keyboard()
    )
    return AI_CHAT


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def ai_chat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    user  = update.effective_user

    if data == "ai_end":
        await _end_chat(query, context, user.id, save=True)
        return ConversationHandler.END

    if data == "ai_new":
        await _clear_history(user.id)
        context.user_data["ai_history"]   = []
        context.user_data["ai_model_idx"] = 0
        # Remove buttons from the response message — don't overwrite content
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        # Send new message below
        await query.message.reply_text(
            "🤖 <b>Dr. Crow AI</b>\n\n"
            "New chat started. Go ahead, ask me anything.",
            parse_mode=HTML,
            reply_markup=_ai_keyboard()
        )
        return AI_CHAT

    if data == "ai_continue":
        history = await _load_history(user.id)
        context.user_data["ai_history"]   = history
        context.user_data["ai_model_idx"] = 0
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "🤖 <b>Dr. Crow AI</b>\n\n"
            "Continuing from before. What's your question?",
            parse_mode=HTML,
            reply_markup=_ai_keyboard()
        )
        return AI_CHAT

    return AI_CHAT


# ── Main message handler ───────────────────────────────────────────────────────

async def ai_chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process member's question — text, photo, or document."""
    msg     = update.message
    user    = update.effective_user

    # Determine input type
    question = ""
    image_data = None   # base64 inline image for Gemini Vision
    file_note  = ""

    if msg.text:
        question = msg.text.strip()
    elif msg.photo or (msg.document and msg.document.mime_type and
                       msg.document.mime_type.startswith("image")):
        # Image — use Gemini Vision
        caption  = msg.caption or ""
        question = caption.strip() if caption.strip() else "Describe and explain this image."
        file_note = "[Image shared]"
        try:
            file_obj = await (msg.photo[-1] if msg.photo else msg.document).get_file()
            file_bytes = await file_obj.download_as_bytearray()
            import base64
            image_data = base64.b64encode(file_bytes).decode()
        except Exception as e:
            logger.warning("Image download failed: %s", e)
            await msg.reply_text("❌ Could not process the image. Please try again.", reply_markup=_ai_keyboard())
            return AI_CHAT
    elif msg.document:
        caption   = msg.caption or ""
        question  = caption.strip() if caption.strip() else "Summarize this document."
        file_note = f"[Document: {msg.document.file_name or 'file'}]"
        # For non-image docs — just use the caption as question
        # Full doc processing (Tier 2) can be added later
    else:
        await msg.reply_text("Please send a text message, image, or document.", reply_markup=_ai_keyboard())
        return AI_CHAT

    if not question:
        question = "What is this?"

    # Load/init history
    history = context.user_data.get("ai_history")
    if history is None:
        history = await _load_history(user.id)
        context.user_data["ai_history"] = history

    model_idx = context.user_data.get("ai_model_idx", 0)

    # Cache disabled — always call LLM for context-aware responses

    # ── Step 2: DB resource search ────────────────────────────────────────────
    resources = await _search_resources(question)

    # ── Step 3: Build messages for Gemini ─────────────────────────────────────
    history.append({"role": "user", "content": question})
    gemini_messages = history[-20:]  # sliding window — last 20 messages

    # ── Step 4: Gemini call with visible spinner message ─────────────────────
    kb   = _resource_buttons(resources) if resources else _ai_keyboard()
    _img = image_data if 'image_data' in dir() and image_data else None

    # Static processing indicator — no repeated API calls
    _t_start     = __import__('time').perf_counter()
    spinner_msg  = await msg.reply_text("Dr. Crow is thinking...")
    _typing_done = asyncio.Event()

    async def _keep_typing():
        """Refresh Telegram typing indicator every 4s — single cheap action."""
        while not _typing_done.is_set():
            try:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(_typing_done.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    typing_task = asyncio.create_task(_keep_typing())
    try:
        response, model_idx = await _call_gemini(
            gemini_messages, resources,
            model_idx=model_idx,
            image_data=_img
        )
    finally:
        _typing_done.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    context.user_data["ai_model_idx"] = model_idx
    _t_elapsed = round(__import__('time').perf_counter() - _t_start, 1)

    # ── Step 5: Replace spinner with formatted response ──────────────────────
    html_text = _md_to_html(response)
    full_text  = f"🤖 {html_text}"
    chunks     = _split_message(full_text, limit=4000)

    sent = False
    for idx, chunk in enumerate(chunks):
        chunk_kb = kb if idx == len(chunks) - 1 else None
        # First chunk: edit spinner_msg
        # Subsequent chunks: send new messages
        if idx == 0:
            try:
                await spinner_msg.edit_text(chunk, parse_mode=HTML, reply_markup=chunk_kb)
                sent = True
            except Exception as e:
                logger.warning("HTML edit failed: %s", e)
                import re as _re2
                plain = _re2.sub(r'<[^>]+>', '', chunk)
                try:
                    await spinner_msg.edit_text(plain, reply_markup=chunk_kb)
                    sent = True
                except Exception:
                    # edit failed — try delete + send
                    try:
                        await spinner_msg.delete()
                    except Exception:
                        pass
                    try:
                        await msg.reply_text(plain, reply_markup=chunk_kb)
                        sent = True
                    except Exception as e2:
                        logger.error("Send failed: %s", e2)
        else:
            try:
                await msg.reply_text(chunk, parse_mode=HTML, reply_markup=chunk_kb)
                sent = True
            except Exception as e:
                import re as _re2
                plain = _re2.sub(r'<[^>]+>', '', chunk)
                try:
                    await msg.reply_text(plain, reply_markup=chunk_kb)
                    sent = True
                except Exception as e2:
                    logger.error("Chunk send failed: %s", e2)

    # ── Step 6: Save history + cache ─────────────────────────────────────────
    _model_name = GEMINI_MODELS[model_idx] if model_idx < len(GEMINI_MODELS) else "unknown"
    history.append({
        "role": "assistant",
        "content": response,
        "meta": {
            "model":   _model_name,
            "elapsed": str(_t_elapsed),
            "sentAt":  __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        }
    })
    history = history[-20:]
    context.user_data["ai_history"] = history
    await _save_history(user.id, history)
    # Cache disabled — no saving

    return AI_CHAT


# ── End chat ───────────────────────────────────────────────────────────────────

async def _end_chat(query_or_msg, context, user_id: int, save: bool = True):
    ttl = await _get_history_ttl_hours()
    history = context.user_data.get("ai_history", [])
    if save and history:
        await _save_history(user_id, history)

    context.user_data.pop("ai_history",   None)
    context.user_data.pop("ai_model_idx", None)
    context.user_data.pop("_in_conversation", None)

    text = (
        f"✅ AI chat ended.\n"
        f"History saved for {ttl}h — tap Ask AI to continue anytime."
    )
    try:
        # Remove buttons from last response — preserve content
        await query_or_msg.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        # Send end notification as new message below
        await query_or_msg.message.reply_text(text)
    except Exception:
        pass


async def ai_chat_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/endai command"""
    _mark_handled(update, context)
    await _end_chat(update.message, context, update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


def _mark_handled(update, context):
    handled = context.bot_data.setdefault("_handled_update_ids", set())
    handled.add(update.update_id)


# ── ConversationHandler ────────────────────────────────────────────────────────

def ai_chat_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(r"^✦ Ask AI$"),
                ai_chat_start
            ),
        ],
        states={
            AI_CHAT: [
                CallbackQueryHandler(ai_chat_cb,  pattern="^ai_"),
                CommandHandler("endai",           ai_chat_end),
                MessageHandler(
                    filters.ChatType.PRIVATE &
                    (filters.TEXT | filters.PHOTO | filters.Document.ALL) &
                    ~filters.COMMAND,
                    ai_chat_msg
                ),
            ],
        },
        fallbacks=[CommandHandler("endai", ai_chat_end)],
        conversation_timeout=1800,  # 30 min idle timeout
        per_message=False,
        per_user=True,     # each user has independent conversation state
        block=False,       # non-blocking — multiple users run in parallel
        allow_reentry=True,
    )