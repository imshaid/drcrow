"""
Registration & Payment Info DB queries.
Supports multiple files per entry.
"""

import json
from typing import Optional, List
from database.db import get_pool


async def regpay_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM regpay WHERE uid = $1)", uid
        )


async def insert_regpay(
    uid: str, semester: str, file_ids: list, tags: list, uploaded_by: int,
    thumbnail_url: Optional[str] = None,
    cover_file_id: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO regpay
                (uid, semester, file_ids, thumbnail_url, cover_file_id, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, uid, semester, json.dumps(file_ids),
            thumbnail_url, cover_file_id,
            json.dumps(tags), None, uploaded_by)


async def get_regpay(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM regpay WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_regpay_files(uid: str, file_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE regpay SET file_ids = $1 WHERE uid = $2",
            json.dumps(file_ids), uid
        )


async def update_regpay_field(uid: str, field: str, value):
    allowed = {"semester", "thumbnail_url", "cover_file_id", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not editable.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE regpay SET {field} = $1 WHERE uid = $2", value, uid
        )


async def delete_regpay(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM regpay WHERE uid = $1", uid)


async def increment_regpay_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE regpay SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_regpay(query: str, limit: int = 10) -> list:
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["uid", "semester", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM regpay
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]


async def get_regpay_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM regpay
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_regpay_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM regpay")


# ── Bot Settings (for Help text) ────────────────────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM bot_settings WHERE key = $1", key
        )
        return row["value"] if row else None


async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, value)