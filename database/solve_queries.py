"""
Solve & Correction DB queries.
"""

import json
from typing import Optional, List
from database.db import get_pool


# ─── SOLVES ────────────────────────────────────────────────────────────────────

async def solve_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM solves WHERE uid = $1)", uid
        )


async def insert_solve(
    uid: str, title: str, subject: Optional[str],
    course_code: Optional[str], file_id: str, file_type: str,
    tags: list, uploaded_by: int,
    semester_id: Optional[int] = None,
    cover_file_id: Optional[str] = None,
    cover_url: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO solves
                (uid, title, subject, course_code, file_id, file_type,
                 cover_file_id, cover_url, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, uid, title, subject, course_code, file_id, file_type,
            cover_file_id, cover_url, json.dumps(tags), semester_id, uploaded_by)


async def get_solve(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM solves WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_solve_field(uid: str, field: str, value):
    allowed = {"title", "subject", "course_code", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not editable.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE solves SET {field} = $1 WHERE uid = $2", value, uid
        )


async def update_solve_file(uid: str, file_id: str, file_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solves SET file_id = $1, file_type = $2 WHERE uid = $3",
            file_id, file_type, uid
        )


async def update_solve_cover(uid: str, cover_file_id: Optional[str], cover_url: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solves SET cover_file_id = $1, cover_url = $2 WHERE uid = $3",
            cover_file_id, cover_url, uid
        )


async def delete_solve(uid: str):
    """Deletes solve + all corrections (CASCADE)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM solves WHERE uid = $1", uid)


async def increment_solve_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solves SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def get_solves_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.*, COUNT(c.uid) as correction_count
            FROM solves s
            LEFT JOIN corrections c ON c.solve_uid = s.uid
            GROUP BY s.uid
            ORDER BY s.created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_solves_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM solves")


async def search_solves_current_semester(query: str, limit: int = 20) -> list:
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["title", "subject", "course_code", "uid", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM solves
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]


# ─── CORRECTIONS ───────────────────────────────────────────────────────────────

async def correct_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM corrections WHERE uid = $1)", uid
        )


async def insert_correction(
    uid: str, solve_uid: str, file_id: str,
    file_type: str, title: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO corrections (uid, solve_uid, title, file_id, file_type)
            VALUES ($1,$2,$3,$4,$5)
        """, uid, solve_uid, title, file_id, file_type)


async def get_correction(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM corrections WHERE uid = $1", uid
        )
        return dict(row) if row else None


async def get_corrections(solve_uid: str) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM corrections
            WHERE solve_uid = $1
            ORDER BY created_at ASC
        """, solve_uid)
        return [dict(r) for r in rows]


async def update_correction_title(uid: str, title: Optional[str]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE corrections SET title = $1 WHERE uid = $2", title, uid
        )


async def update_correction_file(uid: str, file_id: str, file_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE corrections SET file_id = $1, file_type = $2 WHERE uid = $3",
            file_id, file_type, uid
        )


async def delete_correction(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM corrections WHERE uid = $1", uid)


# ─── DELIVERY TRACKING ─────────────────────────────────────────────────────────

async def record_solve_delivery(user_id: int, solve_uid: str):
    """Track who received which solve for correction notifications."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO solve_deliveries (user_id, solve_uid)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, user_id, solve_uid)


async def get_solve_recipients(solve_uid: str) -> List[int]:
    """Get all user_ids who received this solve."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM solve_deliveries WHERE solve_uid = $1", solve_uid
        )
        return [r["user_id"] for r in rows]

async def search_solves_all(query: str, limit: int = 3) -> list:
    """Search solves across ALL semesters — for AI chat resource discovery."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["title", "subject", "course_code", "uid", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM solves
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]