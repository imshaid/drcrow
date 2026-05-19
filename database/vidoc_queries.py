"""
Vidoc (Videos & Docs collection) DB queries.
"""

import json
from typing import Optional, List
from database.db import get_pool


async def vidoc_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM vidocs WHERE uid = $1)", uid
        )


async def insert_vidoc(
    uid: str, subject: Optional[str], course_code: Optional[str],
    messages: list, tags: list, uploaded_by: int,
    semester_id: Optional[int] = None,
    thumbnail_url: Optional[str] = None,
    cover_file_id: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO vidocs
                (uid, subject, course_code, messages, tags,
                 thumbnail_url, cover_file_id, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, uid, subject, course_code,
            json.dumps(messages), json.dumps(tags),
            thumbnail_url, cover_file_id, semester_id, uploaded_by)


async def get_vidoc(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM vidocs WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_vidoc_messages(uid: str, messages: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vidocs SET messages = $1 WHERE uid = $2",
            json.dumps(messages), uid
        )


async def update_vidoc_tags(uid: str, tags: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vidocs SET tags = $1 WHERE uid = $2",
            json.dumps(tags), uid
        )


async def update_vidoc_thumbnail(uid: str, thumbnail_url: Optional[str], cover_file_id: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vidocs SET thumbnail_url = $1, cover_file_id = $2 WHERE uid = $3",
            thumbnail_url, cover_file_id, uid
        )


async def update_vidoc_metadata(uid: str, subject: Optional[str], course_code: Optional[str]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vidocs SET subject = $1, course_code = $2 WHERE uid = $3",
            subject, course_code, uid
        )


async def delete_vidoc(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vidocs WHERE uid = $1", uid)


async def increment_vidoc_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vidocs SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_vidocs(query: str, limit: int = 20) -> list:
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["uid", "subject", "course_code", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM vidocs
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]


async def get_vidocs_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM vidocs
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_vidocs_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM vidocs")