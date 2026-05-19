"""
Note DB queries — CRUD + search.
"""

import json
from typing import Optional, List
from database.db import get_pool


async def note_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM notes WHERE uid = $1)", uid
        )


async def insert_note(
    uid: str, title: str, subject: Optional[str],
    course_code: Optional[str], semester_id: Optional[int],
    file_id: str, file_type: str, tags: list,
    uploaded_by: int,
    cover_file_id: Optional[str] = None,
    cover_url: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO notes
                (uid, title, subject, course_code, semester_id,
                 file_id, file_type, cover_file_id, cover_url, tags, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, uid, title, subject, course_code, semester_id,
            file_id, file_type, cover_file_id, cover_url,
            json.dumps(tags), uploaded_by)


async def get_note(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM notes WHERE uid = $1", uid)
        return dict(row) if row else None


async def update_note_field(uid: str, field: str, value):
    allowed = {"title", "subject", "course_code", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not editable.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE notes SET {field} = $1 WHERE uid = $2", value, uid
        )


async def update_note_file(uid: str, file_id: str, file_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notes SET file_id = $1, file_type = $2 WHERE uid = $3",
            file_id, file_type, uid
        )


async def update_note_cover(uid: str, cover_file_id: Optional[str], cover_url: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notes SET cover_file_id = $1, cover_url = $2 WHERE uid = $3",
            cover_file_id, cover_url, uid
        )


async def delete_note(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM notes WHERE uid = $1", uid)


async def increment_note_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notes SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_notes_current_semester(query: str, limit: int = 20) -> list:
    """Search notes by tags, title, subject, course_code — current semester only."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_sem = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        cols = ["title", "subject", "course_code", "uid", "tags::text"]
        if current_sem:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=2)
            params = [current_sem] + params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM notes
                WHERE semester_id = $1 AND {token_where}
                ORDER BY access_count DESC
                LIMIT ${next_idx}
            """, *params)
        else:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
            params = params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM notes
                WHERE {token_where}
                ORDER BY access_count DESC
                LIMIT ${next_idx}
            """, *params)
        return [dict(r) for r in rows]


async def get_notes_paginated(offset: int = 0, limit: int = 5) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM notes
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_notes_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM notes")

async def search_notes_all(query: str, limit: int = 5) -> list:
    """Search notes across ALL semesters — for AI chat resource discovery."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["title", "subject", "course_code", "uid", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM notes
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]