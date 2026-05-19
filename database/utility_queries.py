"""
Utility DB queries — Academic Calendar, Advisor Info, Fee Overview.
All share the same table with category field.
Supports all file types. Optional thumbnail, URL button, tags.
"""

import json
from typing import Optional
from database.db import get_pool

CATEGORIES = {
    "cal":       ("📅", "Academic Calendar"),
    "advisor":   ("👨‍🏫", "Advisor Info"),
    "fee":       ("💰", "Fee Overview"),
    "syllabus":  ("📋", "Syllabus"),
    "outline":   ("📐", "Course Outline"),
    "routine":   ("🗓", "Exam Routine"),
    "util_misc": ("🔧", "Utility"),
}


async def utility_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM utilities WHERE uid = $1)", uid
        )


async def insert_utility(
    uid: str, category: str, tags: list, uploaded_by: int,
    title: Optional[str] = None,
    subject: Optional[str] = None,
    course_code: Optional[str] = None,
    file_id: Optional[str] = None, file_type: Optional[str] = None,
    file_ids: Optional[list] = None,
    thumbnail_url: Optional[str] = None, cover_file_id: Optional[str] = None,
    url: Optional[str] = None, url_title: Optional[str] = None,
    message_text: Optional[str] = None,
    message_entities: Optional[list] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO utilities
                (uid, category, title, subject, course_code,
                 file_id, file_type, file_ids, thumbnail_url, cover_file_id,
                 message_text, message_entities,
                 url, url_title, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        """, uid, category, title, subject, course_code,
            file_id, file_type, json.dumps(file_ids or []),
            thumbnail_url, cover_file_id,
            message_text, json.dumps(message_entities or []),
            url, url_title, json.dumps(tags), None, uploaded_by)


async def get_utility(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM utilities WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_utility_message(uid: str, message_text: Optional[str], message_entities: Optional[list] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET message_text = $1, message_entities = $2 WHERE uid = $3",
            message_text, json.dumps(message_entities or []), uid
        )


async def update_utility_metadata(uid: str, title: Optional[str], subject: Optional[str], course_code: Optional[str]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET title = $1, subject = $2, course_code = $3 WHERE uid = $4",
            title, subject, course_code, uid
        )


async def update_utility_file(uid: str, file_id: Optional[str], file_type: Optional[str]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET file_id = $1, file_type = $2 WHERE uid = $3",
            file_id, file_type, uid
        )


async def update_utility_thumbnail(uid: str, thumbnail_url: Optional[str], cover_file_id: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET thumbnail_url = $1, cover_file_id = $2 WHERE uid = $3",
            thumbnail_url, cover_file_id, uid
        )


async def update_utility_url(uid: str, url: Optional[str], url_title: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET url = $1, url_title = $2 WHERE uid = $3",
            url, url_title, uid
        )


async def update_utility_tags(uid: str, tags: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET tags = $1 WHERE uid = $2",
            json.dumps(tags), uid
        )


async def delete_utility(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM utilities WHERE uid = $1", uid)


async def increment_utility_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE utilities SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def get_utilities_by_category(category: str, offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM utilities WHERE category = $1
            ORDER BY created_at DESC LIMIT $2 OFFSET $3
        """, category, limit, offset)
        return [dict(r) for r in rows]


async def count_utilities_by_category(category: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM utilities WHERE category = $1", category
        )


async def search_utilities(query: str, category: Optional[str] = None, limit: int = 10) -> list:
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["uid", "tags::text"]
        if category:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=2)
            params = [category] + params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM utilities WHERE category = $1
                AND {token_where}
                ORDER BY access_count DESC LIMIT ${next_idx}
            """, *params)
        else:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
            params = params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM utilities
                WHERE {token_where}
                ORDER BY access_count DESC LIMIT ${next_idx}
            """, *params)
        return [dict(r) for r in rows]