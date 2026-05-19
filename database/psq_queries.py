"""
PSQ (Previous Semester Questions) DB queries.
"""

import json
from typing import Optional, List
from database.db import get_pool

# System reserved tags always present
SYSTEM_TAGS = ["psq", "previous", "questions"]


async def psq_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM psqs WHERE uid = $1)", uid
        )


async def insert_psq(
    uid: str, title: Optional[str], file_id: str, tags: list,
    uploaded_by: int,
    semester_id: Optional[int] = None,
    cover_file_id: Optional[str] = None,
    cover_url: Optional[str] = None
):
    # Merge admin tags with system tags (deduplicated, system tags first)
    merged = list(dict.fromkeys(SYSTEM_TAGS + [t for t in tags if t not in SYSTEM_TAGS]))
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO psqs (uid, title, file_id, cover_file_id, cover_url, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, uid, title, file_id, cover_file_id, cover_url,
            json.dumps(merged), semester_id, uploaded_by)


async def get_psq(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM psqs WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_psq_title(uid: str, title: Optional[str]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE psqs SET title = $1 WHERE uid = $2", title, uid
        )


async def update_psq_tags(uid: str, admin_tags: list):
    """Update tags — always keeps system tags."""
    merged = list(dict.fromkeys(SYSTEM_TAGS + [t for t in admin_tags if t not in SYSTEM_TAGS]))
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE psqs SET tags = $1 WHERE uid = $2",
            json.dumps(merged), uid
        )


async def update_psq_file(uid: str, file_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE psqs SET file_id = $1 WHERE uid = $2", file_id, uid
        )


async def update_psq_cover(uid: str, cover_file_id: Optional[str], cover_url: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE psqs SET cover_file_id = $1, cover_url = $2 WHERE uid = $3",
            cover_file_id, cover_url, uid
        )


async def delete_psq(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM psqs WHERE uid = $1", uid)


async def increment_psq_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE psqs SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_psqs(query: str, limit: int = 20) -> list:
    """Search PSQs by tags or uid."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["uid", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM psqs
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]


async def get_psqs_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM psqs
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_psqs_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM psqs")