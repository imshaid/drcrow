"""
Book & Solution Manual DB queries.
"""

import json
from typing import Optional, List
from database.db import get_pool


async def book_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM books WHERE uid = $1)", uid
        )


async def insert_book(
    uid: str, title: str, authors: str, edition: Optional[str],
    subject: str, course_codes: Optional[str], file_id: str,
    tags: list, uploaded_by: int, semester_id: Optional[int],
    cover_file_id: Optional[str] = None,
    cover_url: Optional[str] = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO books
                (uid, title, authors, edition, subject, course_codes,
                 file_id, cover_file_id, cover_url, tags, semester_id, uploaded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        """, uid, title, authors, edition, subject, course_codes,
            file_id, cover_file_id, cover_url, json.dumps(tags), semester_id, uploaded_by)


async def get_book(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM books WHERE uid = $1", uid
        )
        return dict(row) if row else None


async def get_book_solutions(book_uid: str) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM solution_manuals
            WHERE book_uid = $1
            ORDER BY created_at ASC
        """, book_uid)
        return [dict(r) for r in rows]


async def increment_book_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE books SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def solution_uid_exists(uid: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM solution_manuals WHERE uid = $1)", uid
        )


async def insert_solution(uid: str, book_uid: str, file_id: str, uploaded_by: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO solution_manuals (uid, book_uid, file_id, uploaded_by)
            VALUES ($1,$2,$3,$4)
        """, uid, book_uid, file_id, uploaded_by)


async def increment_solution_access(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solution_manuals SET access_count = access_count + 1 WHERE uid = $1", uid
        )


async def search_books_current_semester(query: str, limit: int = 20) -> list:
    """Search books in current semester. Falls back to all books if no semester set."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_sem = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        cols = ["title", "authors", "subject", "course_codes", "uid", "tags::text"]
        if current_sem:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=2)
            params = [current_sem] + params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM books
                WHERE semester_id = $1 AND {token_where}
                ORDER BY access_count DESC
                LIMIT ${next_idx}
            """, *params)
        else:
            token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
            params = params + [limit]
            rows = await conn.fetch(f"""
                SELECT * FROM books
                WHERE {token_where}
                ORDER BY access_count DESC
                LIMIT ${next_idx}
            """, *params)
        return [dict(r) for r in rows]

# ─── EDIT / DELETE ─────────────────────────────────────────────────────────────

async def update_book_field(uid: str, field: str, value):
    """Update a single metadata field of a book."""
    allowed = {"title", "authors", "edition", "subject", "course_codes", "file_id", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' not allowed")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if field == "tags":
            import json as _json
            value = _json.dumps(value)
        await conn.execute(
            f"UPDATE books SET {field} = $2 WHERE uid = $1", uid, value
        )


async def delete_book(uid: str):
    """Delete book and all its solution manuals (CASCADE)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM books WHERE uid = $1", uid)


async def get_solution(uid: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM solution_manuals WHERE uid = $1", uid
        )
        return dict(row) if row else None


async def update_solution_file(uid: str, file_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solution_manuals SET file_id = $2 WHERE uid = $1", uid, file_id
        )


async def delete_solution(uid: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM solution_manuals WHERE uid = $1", uid
        )


async def count_solutions(book_uid: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM solution_manuals WHERE book_uid = $1", book_uid
        )


async def update_cover_file(uid: str, cover_file_id: Optional[str], cover_url: Optional[str] = None):
    """Update book cover image file_id and imgBB URL."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE books SET cover_file_id = $1, cover_url = $2 WHERE uid = $3",
            cover_file_id, cover_url, uid
        )


# ─── EDIT & DELETE ─────────────────────────────────────────────────────────────

async def update_book_field(uid: str, field: str, value):
    """Update a single metadata field of a book."""
    allowed = {"title", "authors", "edition", "subject", "course_codes", "tags"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' is not editable.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE books SET {field} = $1 WHERE uid = $2", value, uid
        )


async def update_book_file(uid: str, file_id: str):
    """Replace the book PDF file_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE books SET file_id = $1 WHERE uid = $2", file_id, uid
        )


async def get_solution(uid: str) -> Optional[dict]:
    """Get a single solution manual by UID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM solution_manuals WHERE uid = $1", uid
        )
        return dict(row) if row else None


async def replace_solution_file(uid: str, file_id: str):
    """Replace the solution manual PDF file_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE solution_manuals SET file_id = $1 WHERE uid = $2", file_id, uid
        )


async def delete_book(uid: str):
    """
    Delete a book and all its solution manuals.
    CASCADE constraint handles solution deletion automatically.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM books WHERE uid = $1", uid)


async def delete_solution(uid: str):
    """Delete a single solution manual."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM solution_manuals WHERE uid = $1", uid
        )


async def get_books_paginated(offset: int = 0, limit: int = 5) -> list:
    """Get books with pagination, ordered by creation date."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT b.*, COUNT(s.uid) as solution_count
            FROM books b
            LEFT JOIN solution_manuals s ON s.book_uid = b.uid
            GROUP BY b.uid
            ORDER BY b.created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(r) for r in rows]


async def get_books_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM books")

async def search_books_all(query: str, limit: int = 3) -> list:
    """Search books across ALL semesters — for AI chat resource discovery."""
    from database.db import build_token_where
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = ["title", "authors", "subject", "course_codes", "uid", "tags::text"]
        token_where, params, next_idx = build_token_where(query, cols, start_idx=1)
        params = params + [limit]
        rows = await conn.fetch(f"""
            SELECT * FROM books
            WHERE {token_where}
            ORDER BY access_count DESC
            LIMIT ${next_idx}
        """, *params)
        return [dict(r) for r in rows]