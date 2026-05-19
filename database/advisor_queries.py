"""
Advisor assignment DB queries.
Table: advisor_assignments
Each row = one advisor with an ID range (id_from to id_to).
Non-continuous IDs → two rows with same advisor, different ranges.

CSV format (admin uploads):
    advisor_name, designation, id_from, id_to, room, schedule, email, phone
"""

import csv
import io
from typing import Optional
from database.db import get_pool


def _id_num(student_id: str) -> Optional[int]:
    """
    Extract numeric suffix from student ID.
    "241-15-045" → 45
    "241-15-001" → 1
    Returns None if parsing fails.
    """
    try:
        return int(student_id.strip().split("-")[-1])
    except (ValueError, IndexError):
        return None


async def find_advisor_by_student_id(student_id: str) -> Optional[dict]:
    """
    Range-match student_id against advisor_assignments.
    Compares numeric suffix of student_id against numeric suffixes of id_from/id_to.
    Returns the matching advisor row, or None.
    """
    student_num = _id_num(student_id)
    if student_num is None:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM advisor_assignments ORDER BY created_at DESC"
        )

    for row in rows:
        from_num = _id_num(row["id_from"])
        to_num   = _id_num(row["id_to"])
        if from_num is None or to_num is None:
            continue
        if from_num <= student_num <= to_num:
            return dict(row)

    return None


async def insert_advisor_assignments(rows: list[dict], semester_id: Optional[int], uploaded_by: int):
    """
    Bulk insert advisor rows parsed from CSV.
    Each dict must have: advisor_name, designation, id_from, id_to,
                         room, schedule, email, phone
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany("""
            INSERT INTO advisor_assignments
                (advisor_name, designation, id_from, id_to,
                 room, schedule, email, phone, semester_id, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """, [
            (
                r.get("advisor_name", "").strip(),
                r.get("designation", "").strip() or None,
                r.get("id_from", "").strip(),
                r.get("id_to", "").strip(),
                r.get("room", "").strip() or None,
                r.get("schedule", "").strip() or None,
                r.get("email", "").strip() or None,
                r.get("phone", "").strip() or None,
                semester_id,
                uploaded_by,
            )
            for r in rows
        ])


def parse_advisor_csv(content: bytes) -> tuple[list[dict], list[str]]:
    """
    Parse CSV bytes into list of advisor dicts.
    Returns (rows, errors).
    Required columns: advisor_name, id_from, id_to
    Optional: designation, room, schedule, email, phone

    CSV header (case-insensitive, stripped):
        advisor_name, designation, id_from, id_to, room, schedule, email, phone
    """
    REQUIRED = {"advisor_name", "id_from", "id_to"}
    rows   = []
    errors = []

    try:
        text = content.decode("utf-8-sig").strip()  # strip BOM if present
    except UnicodeDecodeError:
        return [], ["Could not decode CSV. Make sure it is UTF-8 encoded."]

    reader = csv.DictReader(io.StringIO(text))

    # Normalise headers
    if reader.fieldnames is None:
        return [], ["CSV appears to be empty."]

    normalised = {h.strip().lower(): h for h in reader.fieldnames}
    missing = REQUIRED - set(normalised.keys())
    if missing:
        return [], [f"CSV missing required columns: {', '.join(sorted(missing))}"]

    for i, raw_row in enumerate(reader, start=2):  # line numbers start at 2 (1 = header)
        row = {k.strip().lower(): v for k, v in raw_row.items()}

        advisor_name = row.get("advisor_name", "").strip()
        id_from      = row.get("id_from", "").strip()
        id_to        = row.get("id_to", "").strip()

        if not advisor_name:
            errors.append(f"Row {i}: advisor_name is empty — skipped.")
            continue
        if not id_from or not id_to:
            errors.append(f"Row {i}: id_from or id_to is empty — skipped.")
            continue
        if _id_num(id_from) is None or _id_num(id_to) is None:
            errors.append(f"Row {i}: Cannot parse ID range '{id_from}' to '{id_to}' — skipped.")
            continue

        rows.append({
            "advisor_name": advisor_name,
            "designation":  row.get("designation", "").strip(),
            "id_from":      id_from,
            "id_to":        id_to,
            "room":         row.get("room", "").strip(),
            "schedule":     row.get("schedule", "").strip(),
            "email":        row.get("email", "").strip(),
            "phone":        row.get("phone", "").strip(),
        })

    return rows, errors


async def get_advisor_assignments_by_semester(semester_id: Optional[int]) -> list[dict]:
    """List all advisor rows for a semester (for admin listing)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if semester_id:
            rows = await conn.fetch(
                "SELECT * FROM advisor_assignments WHERE semester_id = $1 ORDER BY id_from",
                semester_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM advisor_assignments ORDER BY created_at DESC, id_from"
            )
    return [dict(r) for r in rows]


async def delete_advisor_assignments_by_semester(semester_id: int):
    """Delete all advisor rows for a semester (before re-upload)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM advisor_assignments WHERE semester_id = $1", semester_id
        )


async def save_student_id(user_id: int, student_id: str):
    """Save or update student_id in users table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET student_id = $1 WHERE user_id = $2",
            student_id.strip(), user_id
        )


async def get_student_id(user_id: int) -> Optional[str]:
    """Get stored student_id for a user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT student_id FROM users WHERE user_id = $1", user_id
        )