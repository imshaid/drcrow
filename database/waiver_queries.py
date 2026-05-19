"""
Waiver DB queries.
"""

import json
from typing import Optional
from database.db import get_pool


async def waiver_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM waivers WHERE uid = $1)", uid
        )


async def insert_waiver(
    uid: str, semester_name: str, tuition_fee: int, semester_fee: int,
    tags: list, uploaded_by: int,
    file_id: Optional[str] = None, file_type: Optional[str] = None,
    thumbnail_url: Optional[str] = None, cover_file_id: Optional[str] = None,
    url: Optional[str] = None, url_title: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO waivers
                (uid, semester_name, tuition_fee, semester_fee,
                 file_id, file_type, thumbnail_url, cover_file_id,
                 url, url_title, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """, uid, semester_name, tuition_fee, semester_fee,
            file_id, file_type, thumbnail_url, cover_file_id,
            url, url_title, json.dumps(tags), None, uploaded_by)


async def get_waiver(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM waivers WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_waiver_field(uid: str, field: str, value):
    allowed = {"semester_name", "tuition_fee", "semester_fee",
               "file_id", "file_type", "thumbnail_url", "cover_file_id",
               "url", "url_title", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not editable.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE waivers SET {field} = $1 WHERE uid = $2", value, uid
        )


async def delete_waiver(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM waivers WHERE uid = $1", uid)


async def increment_waiver_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE waivers SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_waivers(query: str, limit: int = 5) -> list:
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["uid", "semester_name", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM waivers
            WHERE {token_where}
            ORDER BY created_at DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]


async def get_waivers_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM waivers
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_waivers_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM waivers")