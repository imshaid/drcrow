"""
Search Engine — 3-Layer Intelligence
Layer 1: SQL ILIKE + JSONB (~30ms)
Layer 2: RapidFuzz fuzzy (~150-300ms)
Layer 3: OpenRouter LLM semantic (~1-2s)
"""

import logging
import json
import asyncio
import re
import aiohttp
from typing import List, Optional, Tuple
from rapidfuzz import fuzz
from config.settings import settings
from database import queries
from database.book_queries import search_books_current_semester
from database.note_queries import search_notes_current_semester
from database.psq_queries import search_psqs
from database.solve_queries import search_solves_current_semester
from database.vidoc_queries import search_vidocs
from database.utility_queries import search_utilities
from database.waiver_queries import search_waivers
from database.regpay_queries import search_regpay
from database.note_queries import search_notes_current_semester
from utils.local_kb import get_local_answer

logger = logging.getLogger(__name__)

# Per-model: 1 attempt only, move on immediately if rate limited
# Hard cap: 20 seconds total across all models
LLM_TOTAL_TIMEOUT = 20  # seconds


async def search(query: str, category: str = None, course: str = None) -> Tuple[List, str]:
    if not query or not query.strip():
        top = await queries.get_top_resources_this_week(6)
        return list(top), "top"

    tokens = [t.lower() for t in query.strip().split() if t]

    # ── LAYER 1 ──────────────────────────────────────────────
    results = await queries.search_layer1(tokens, category, course, limit=50)
    if len(results) >= 3:
        return list(results), "exact"

    # ── LAYER 2 ──────────────────────────────────────────────
    all_resources = await queries.get_all_resources_for_fuzzy()
    fuzzy_results = _fuzzy_search(query, all_resources, threshold=70)
    if fuzzy_results:
        ids = [r["id"] for r in fuzzy_results]
        full = await queries.get_resources_by_ids(ids)
        return list(full), "fuzzy"

    # ── LAYER 3 ──────────────────────────────────────────────
    llm_ids = await _llm_semantic_search(query, all_resources)
    if llm_ids:
        full = await queries.get_resources_by_ids(llm_ids)
        if full:
            return list(full), "llm"

    return [], "none"


async def search_fast(query: str) -> Tuple[List, str]:
    """
    Fast search for inline queries — Layer 1 + 2 only, NO LLM.
    Searches both resources table AND books table.
    Completes well within Telegram's 10s inline query timeout.
    Returns (results, source) where each result has a '_type' key:
      'resource' for normal resources, 'book' for books, 'note' for notes.
    """
    if not query or not query.strip():
        top = await queries.get_top_resources_this_week(6)
        # Mark as resource type
        results = [dict(r) | {"_type": "resource"} for r in top]
        return results, "top"

    tokens = [t.lower() for t in query.strip().split() if t]

    # ── Search resources (Layer 1) ─────────────────────────
    resource_results = await queries.search_layer1(tokens, limit=30)
    resource_hits = [dict(r) | {"_type": "resource"} for r in resource_results]

    # ── Search books ──────────────────────────────────────
    book_hits_raw = await search_books_current_semester(query, limit=20)
    book_hits = [dict(b) | {"_type": "book"} for b in book_hits_raw]

    # ── Search notes ──────────────────────────────────────
    note_hits_raw = await search_notes_current_semester(query, limit=20)
    note_hits = [dict(n) | {"_type": "note"} for n in note_hits_raw]

    # ── Search PSQs ───────────────────────────────────────
    psq_hits_raw = await search_psqs(query, limit=10)
    psq_hits = [dict(p) | {"_type": "psq"} for p in psq_hits_raw]

    # ── Search solves ────────────────────────────────────
    solve_hits_raw = await search_solves_current_semester(query, limit=20)
    solve_hits = [dict(s) | {"_type": "solve"} for s in solve_hits_raw]

    # ── Search vidocs ────────────────────────────────────
    vidoc_hits_raw = await search_vidocs(query, limit=20)
    vidoc_hits = [dict(v) | {"_type": "vidoc"} for v in vidoc_hits_raw]

    # ── Search utilities ─────────────────────────────────
    util_hits_raw = await search_utilities(query, limit=10)
    util_hits = [dict(u) | {"_type": "utility"} for u in util_hits_raw]

    # ── Search waivers ───────────────────────────────────
    waiver_hits_raw = await search_waivers(query, limit=5)
    waiver_hits = [dict(w) | {"_type": "waiver"} for w in waiver_hits_raw]

    # ── Search regpay ────────────────────────────────────
    regpay_hits_raw = await search_regpay(query, limit=5)
    regpay_hits = [dict(r) | {"_type": "regpay"} for r in regpay_hits_raw]

    combined = book_hits + note_hits + psq_hits + solve_hits + vidoc_hits + util_hits + waiver_hits + regpay_hits + resource_hits

    if combined:
        return combined, "exact"

    # ── Fuzzy fallback on resources only ───────────────────
    all_resources = await queries.get_all_resources_for_fuzzy()
    fuzzy_results = _fuzzy_search(query, all_resources, threshold=65)
    if fuzzy_results:
        ids = [r["id"] for r in fuzzy_results]
        full = await queries.get_resources_by_ids(ids)
        return [dict(r) | {"_type": "resource"} for r in full], "fuzzy"

    return [], "none"



def _fuzzy_search(query: str, resources: list, threshold: int = 70) -> list:
    scored = []
    for r in resources:
        title = r["title"] or ""
        course = r["course_code"] or ""
        tags_raw = r["tags"]
        if isinstance(tags_raw, str):
            try:
                tags = " ".join(json.loads(tags_raw))
            except Exception:
                tags = ""
        elif isinstance(tags_raw, list):
            tags = " ".join(tags_raw)
        else:
            tags = ""
        combined = f"{title} {course} {tags}"
        score = fuzz.token_sort_ratio(query.lower(), combined.lower())
        if score >= threshold:
            scored.append((r, score))
    scored.sort(key=lambda x: (-x[1], -x[0]["access_count"]))
    return [r for r, _ in scored[:50]]


async def _llm_semantic_search(query: str, resources: list) -> List[int]:
    if not resources:
        return []

    index_lines = []
    for r in resources[:100]:
        tags_raw = r["tags"]
        if isinstance(tags_raw, str):
            try:
                tags = ",".join(json.loads(tags_raw))
            except Exception:
                tags = ""
        elif isinstance(tags_raw, list):
            tags = ",".join(tags_raw)
        else:
            tags = ""
        index_lines.append(f"ID:{r['id']} | {r['title']} | {r['course_code'] or ''} | {tags}")

    prompt = (
        f"Search query: \"{query}\"\n"
        f"Resources:\n" + "\n".join(index_lines) + "\n\n"
        f"Return ONLY a JSON array of matching IDs, max 10. Example: [12,5]. If none: []"
    )

    try:
        result = await asyncio.wait_for(
            _try_models(prompt, max_tokens=80),
            timeout=LLM_TOTAL_TIMEOUT
        )
        if result:
            return _parse_id_list(result) or []
    except asyncio.TimeoutError:
        logger.warning("[Search LLM] Total timeout reached")
    return []


async def llm_direct_answer(query: str) -> Optional[str]:
    """DM fallback answer. Hard 20s timeout total across all models."""
    system = (
        "You are Dr. Crow, academic assistant of Twilight Crows BSc CSE group in Bangladesh.\n"
        "Rules:\n"
        "1. Answer in English. If student writes in Bengali, reply in Bengali.\n"
        "2. Focus on BSc CSE topics only.\n"
        "3. Maximum 200 words. Be concise and clear.\n"
        "4. Use PLAIN TEXT ONLY. No asterisks, no backticks, no hashtags, no markdown.\n"
        "5. End your answer with: Search @drcrow_bot for related files.\n"
        "6. If unsure, say so honestly."
    )
    prompt = f"{system}\n\nQuestion: {query}"

    try:
        result = await asyncio.wait_for(
            _try_models(prompt, max_tokens=350),
            timeout=LLM_TOTAL_TIMEOUT
        )
        if result:
            return _safe_text(result)
    except asyncio.TimeoutError:
        logger.warning("[LLM Answer] Total timeout reached after 20s")

    return None


async def _try_models(prompt: str, max_tokens: int) -> Optional[str]:
    """Try each model once, skip immediately on 429/503/null."""
    for model in settings.LLM_MODELS:
        try:
            result = await _call_openrouter(model, prompt, max_tokens)
            if result and result.strip():
                logger.info(f"[LLM] Success with {model}")
                return result.strip()
            else:
                logger.warning(f"[LLM] {model} returned empty response, trying next")
        except RateLimitError:
            logger.warning(f"[LLM] {model} rate limited (429), trying next")
        except ServiceUnavailableError:
            logger.warning(f"[LLM] {model} unavailable (503), trying next")
        except asyncio.TimeoutError:
            logger.warning(f"[LLM] {model} timed out, trying next")
        except Exception as e:
            logger.warning(f"[LLM] {model} error: {e}, trying next")
    return None


class RateLimitError(Exception):
    pass

class ServiceUnavailableError(Exception):
    pass


async def _call_openrouter(model: str, prompt: str, max_tokens: int = 350) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://drcrow.twilightcrows.app",
        "X-Title": "Dr. Crow"
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            settings.OPENROUTER_BASE_URL,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8)  # 8s per model max
        ) as resp:
            if resp.status == 429:
                raise RateLimitError()
            if resp.status == 503:
                raise ServiceUnavailableError()
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"HTTP {resp.status}: {text[:100]}")
            data = await resp.json()
            # Safely extract content — some models return null
            try:
                content = data["choices"][0]["message"]["content"]
                return content if content else None
            except (KeyError, IndexError, TypeError):
                return None


def _safe_text(text: str) -> str:
    """Strip all markdown — LLM answers are sent as plain text."""
    text = re.sub(r'\*{1,3}', '', text)
    text = re.sub(r'_{1,3}', '', text)
    text = re.sub(r'`{1,3}', '', text)
    text = re.sub(r'#{1,6}\s?', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    if len(text) > 3000:
        text = text[:2950] + "\n\n[Truncated — ask a more specific question]"
    return text


def _parse_id_list(text: str) -> Optional[List[int]]:
    try:
        text = text.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return None
        arr = json.loads(text[start:end+1])
        return [int(x) for x in arr if isinstance(x, (int, float))]
    except Exception:
        return None


def format_resource_caption(resource) -> str:
    cat_emoji = {
        "note": "📝", "book": "📚", "past_question": "📋",
        "solution": "✅", "video": "🎥", "utility": "🔧",
        "syllabus": "📄", "outline": "📌", "routine": "🗓️"
    }
    emoji = cat_emoji.get(resource["category"], "📎")
    course = resource["course_code"] or "General"
    category = (resource["category"] or "resource").replace("_", " ").title()

    tags_raw = resource["tags"]
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except Exception:
            tags = []
    elif isinstance(tags_raw, list):
        tags = tags_raw
    else:
        tags = []

    tag_text = " ".join([f"#{t}" for t in tags]) if tags else ""
    caption = (
        f"{emoji} *{resource['title']}*\n"
        f"📚 Course: `{course}`  |  🗂 {category}\n"
    )
    if tag_text:
        caption += f"🏷 {tag_text}\n"
    caption += f"⬇️ Downloaded {resource['access_count']} times"
    return caption