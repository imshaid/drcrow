"""
All database query functions. Handlers never write raw SQL — they call these.
"""

import asyncpg
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from database.db import get_pool  # noqa: F401 — re-exported for convenience


# ─────────────────────────── USERS ───────────────────────────

async def get_user(user_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)


async def upsert_user(user_id: int, username: str, full_name: str) -> asyncpg.Record:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO users (user_id, username, full_name, last_active, is_member)
            VALUES ($1, $2, $3, NOW(), TRUE)
            ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_active = NOW(),
                    is_member = TRUE,
                    left_at = NULL
            RETURNING *
        """, user_id, username, full_name)
        # Auto-subscribe broadcast — deduplicate since NULL breaks UNIQUE constraint
        exists = await conn.fetchval("""
            SELECT 1 FROM subscriptions
            WHERE user_id = $1 AND course_code IS NULL AND category = 'broadcast'
            LIMIT 1
        """, user_id)
        if not exists:
            await conn.execute("""
                INSERT INTO subscriptions (user_id, course_code, category)
                VALUES ($1, NULL, 'broadcast')
            """, user_id)
        return row


async def mark_user_left(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET is_member = FALSE, left_at = NOW()
            WHERE user_id = $1
        """, user_id)


async def update_last_active(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET last_active = NOW() WHERE user_id = $1
        """, user_id)


async def add_points(user_id: int, points: int, reason: str = "") -> int:
    """Add (or deduct if negative) points and update rank. Returns new total."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        new_points = await conn.fetchval("""
            UPDATE users
            SET points = GREATEST(0, points + $2),
                last_active = NOW()
            WHERE user_id = $1
            RETURNING points
        """, user_id, points)

        rank = _calculate_rank(new_points)
        await conn.execute(
            "UPDATE users SET rank = $2 WHERE user_id = $1", user_id, rank
        )

        import json as _json
        await conn.execute("""
            INSERT INTO analytics (event_type, user_id, meta)
            VALUES ('points', $1, $2)
        """, user_id, _json.dumps({"points": points, "reason": reason, "total": new_points}))

        return new_points


def _calculate_rank(points: int) -> str:
    if points <= 20:
        return "Egg"
    elif points <= 100:
        return "Crow"
    elif points <= 300:
        return "Senior Crow"
    else:
        return "Dr. Crow"


async def add_stars(user_id: int, delta: float, reason: str = "") -> float:
    """Add (or deduct if negative) stars. Returns new total. Floors at 0."""
    import json as _json
    pool = await get_pool()
    async with pool.acquire() as conn:
        new_stars = await conn.fetchval("""
            UPDATE users
            SET stars = GREATEST(0, stars + $2),
                last_active = NOW()
            WHERE user_id = $1
            RETURNING stars
        """, user_id, float(delta))
        await conn.execute("""
            INSERT INTO analytics (event_type, user_id, meta)
            VALUES ('stars', $1, $2)
        """, user_id, _json.dumps({"delta": delta, "reason": reason, "total": new_stars}))
        return new_stars or 0.0


async def reset_warned_today():
    """Called by scheduler at midnight to reset daily warning flags."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET warned_today = FALSE")


async def increment_download(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET download_count = download_count + 1 WHERE user_id = $1
        """, user_id)


async def get_all_member_ids() -> List[int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users WHERE is_member = TRUE")
        return [r["user_id"] for r in rows]


async def add_flag(user_id: int, flag_type: str, reason: str, actioned_by: int = None):
    pool = await get_pool()
    col_map = {
        "bot": "bot_flags",
        "spam": "spam_flags",
        "content": "content_flags",
        "false_report": "false_report_flags"
    }
    col = col_map.get(flag_type, "spam_flags")
    async with pool.acquire() as conn:
        await conn.execute(f"""
            UPDATE users SET {col} = {col} + 1 WHERE user_id = $1
        """, user_id)
        await conn.execute("""
            INSERT INTO flags (user_id, flag_type, reason, actioned_by)
            VALUES ($1, $2, $3, $4)
        """, user_id, flag_type, reason, actioned_by)


async def set_muted_until(user_id: int, until: datetime):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET muted_until = $2 WHERE user_id = $1", user_id, until
        )


async def get_flag_counts(user_id: int) -> Dict[str, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT bot_flags, spam_flags, content_flags, false_report_flags
            FROM users WHERE user_id = $1
        """, user_id)
        if not row:
            return {}
        return dict(row)


# ─────────────────────────── MEMBERSHIP CACHE ───────────────────────────

async def set_membership_cache(user_id: int, is_member: bool, left_at: datetime = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO membership_cache (user_id, is_member, left_at, cached_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE
                SET is_member = EXCLUDED.is_member,
                    left_at = EXCLUDED.left_at,
                    cached_at = NOW()
        """, user_id, is_member, left_at)


async def get_membership_cache(user_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM membership_cache WHERE user_id = $1", user_id
        )


async def clear_membership_cache(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM membership_cache WHERE user_id = $1", user_id
        )


# ─────────────────────────── RESOURCES ───────────────────────────

async def insert_resource(
    title: str, file_id: str, file_type: str,
    course_code: str, category: str, tags: list,
    semester_id: int, uploaded_by: int, approved_by: int
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
        resource_id = await conn.fetchval("""
            INSERT INTO resources
                (title, file_id, file_type, course_code, category, tags,
                 semester_id, uploaded_by, approved_by, is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
            RETURNING id
        """, title, file_id, file_type, course_code, category,
            json.dumps(tags), semester_id, uploaded_by, approved_by)
        return resource_id


async def insert_search_index(resource_id: int, text_content: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO search_index (resource_id, text_content)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, resource_id, text_content or "")


async def get_resource(resource_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM resources WHERE id = $1 AND is_active = TRUE", resource_id
        )


async def increment_access(resource_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE resources SET access_count = access_count + 1 WHERE id = $1
        """, resource_id)


async def search_layer1(tokens: List[str], category: str = None, course: str = None, limit: int = 50):
    """SQL ILIKE + JSONB tag search — current semester only."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get current semester id for filtering
        current_sem = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )

        conditions = ["r.is_active = TRUE"]
        if current_sem:
            conditions.append(f"r.semester_id = {current_sem}")
        params = []
        idx = 1

        for token in tokens:
            conditions.append(f"(r.title ILIKE ${idx} OR r.tags @> ${idx+1}::jsonb)")
            params.append(f"%{token}%")
            params.append(f'["{token}"]')
            idx += 2

        if category:
            conditions.append(f"r.category = ${idx}")
            params.append(category)
            idx += 1

        if course:
            conditions.append(f"r.course_code ILIKE ${idx}")
            params.append(f"%{course}%")
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        return await conn.fetch(f"""
            SELECT r.*, s.name as semester_name
            FROM resources r
            LEFT JOIN semesters s ON s.id = r.semester_id
            WHERE {where}
            ORDER BY r.access_count DESC
            LIMIT ${idx}
        """, *params)


async def get_all_resources_for_fuzzy() -> List[asyncpg.Record]:
    """Pull current semester active resources for fuzzy matching."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_sem = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        if current_sem:
            return await conn.fetch("""
                SELECT id, title, course_code, category, tags, access_count
                FROM resources
                WHERE is_active = TRUE AND semester_id = $1
            """, current_sem)
        return await conn.fetch("""
            SELECT id, title, course_code, category, tags, access_count
            FROM resources WHERE is_active = TRUE
        """)


async def get_resources_by_ids(ids: List[int]) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT r.*, s.name as semester_name
            FROM resources r
            LEFT JOIN semesters s ON s.id = r.semester_id
            WHERE r.id = ANY($1) AND r.is_active = TRUE
            ORDER BY r.access_count DESC
        """, ids)


async def get_top_resources_this_week(limit: int = 6) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT r.* FROM resources r
            JOIN analytics a ON a.resource_id = r.id
            WHERE a.event_type = 'download'
              AND a.created_at >= NOW() - INTERVAL '7 days'
              AND r.is_active = TRUE
            GROUP BY r.id
            ORDER BY COUNT(a.id) DESC
            LIMIT $1
        """, limit)


async def get_resources_by_category(category: str, limit: int = 20) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM resources
            WHERE category = $1 AND is_active = TRUE
            ORDER BY access_count DESC
            LIMIT $2
        """, category, limit)


async def get_resources_by_course(course_code: str, limit: int = 20) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM resources
            WHERE course_code ILIKE $1 AND is_active = TRUE
            ORDER BY access_count DESC
            LIMIT $2
        """, f"%{course_code}%", limit)


async def deactivate_resource(resource_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resources SET is_active = FALSE WHERE id = $1", resource_id
        )


async def feature_resource(resource_id: int, featured: bool = True):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resources SET is_featured = $2 WHERE id = $1", resource_id, featured
        )


# ─────────────────────────── PENDING RESOURCES ───────────────────────────

async def insert_pending_resource(
    submitted_by: int, title: str, course_code: str, category: str, tags: list,
    file_id: str = None, file_type: str = None
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
        return await conn.fetchval("""
            INSERT INTO pending_resources
                (submitted_by, file_id, file_type, title, course_code, category, tags)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING id
        """, submitted_by, file_id, file_type, title, course_code, category, json.dumps(tags))


async def get_pending_resources() -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT pr.*, u.full_name, u.username
            FROM pending_resources pr
            JOIN users u ON u.user_id = pr.submitted_by
            WHERE pr.status = 'pending'
            ORDER BY pr.created_at ASC
        """)


async def get_pending_resource(pending_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM pending_resources WHERE id = $1", pending_id
        )


async def update_pending_status(pending_id: int, status: str, reviewed_by: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE pending_resources
            SET status = $2, reviewed_by = $3
            WHERE id = $1
        """, pending_id, status, reviewed_by)


# ─────────────────────────── SEMESTERS ───────────────────────────

async def get_current_semester() -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM semesters WHERE is_current = TRUE LIMIT 1"
        )


async def get_semester_by_uid(uid: str) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM semesters WHERE uid = $1", uid.lower().strip()
        )


async def get_all_semesters() -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM semesters ORDER BY created_at DESC")


async def create_semester(uid: str, name: str, courses: list) -> asyncpg.Record:
    """Create new semester and set as current. Returns new semester record."""
    import json
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE semesters SET is_current = FALSE")
            row = await conn.fetchrow("""
                INSERT INTO semesters (uid, name, courses, is_current)
                VALUES ($1, $2, $3, TRUE)
                RETURNING *
            """, uid.lower().strip(), name, json.dumps(courses))
        return row


async def activate_semester(uid: str) -> Optional[asyncpg.Record]:
    """Set an existing semester as current by uid. Returns semester or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sem = await conn.fetchrow("SELECT * FROM semesters WHERE uid = $1", uid.lower().strip())
        if not sem:
            return None
        async with conn.transaction():
            await conn.execute("UPDATE semesters SET is_current = FALSE")
            await conn.execute("UPDATE semesters SET is_current = TRUE WHERE uid = $1", uid.lower().strip())
        return sem


async def rename_semester(uid: str, new_name: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE semesters SET name = $1 WHERE uid = $2", new_name, uid.lower().strip()
        )
        return result == "UPDATE 1"


async def kill_semester(uid: str) -> dict:
    """
    Permanently delete a semester and all its resources.
    Returns dict with counts of deleted rows per table.
    Refuses to kill the current semester.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        sem = await conn.fetchrow("SELECT * FROM semesters WHERE uid = $1", uid.lower().strip())
        if not sem:
            return {"error": "not_found"}
        if sem["is_current"]:
            return {"error": "is_current"}

        sid = sem["id"]
        counts = {}
        async with conn.transaction():
            for tbl in ("books", "notes", "psqs", "solves", "vidocs",
                        "utilities", "waivers", "regpay", "resources"):
                n = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {tbl} WHERE semester_id = $1", sid
                )
                await conn.execute(
                    f"DELETE FROM {tbl} WHERE semester_id = $1", sid
                )
                counts[tbl] = n
            await conn.execute("DELETE FROM semesters WHERE id = $1", sid)
        return {"deleted": counts, "semester": dict(sem)}


async def get_semester_resource_counts(sem_id: int) -> dict:
    """Row counts per table for a specific semester."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        counts = {}
        for tbl in ("books", "notes", "solves", "psqs",
                    "vidocs", "utilities", "waivers", "regpay"):
            counts[tbl] = await conn.fetchval(
                f"SELECT COUNT(*) FROM {tbl} WHERE semester_id = $1", sem_id
            )
        counts["resources"] = await conn.fetchval(
            "SELECT COUNT(*) FROM resources WHERE semester_id = $1 AND is_active = TRUE", sem_id
        )
        return counts


# ─────────────────────────── SUBSCRIPTIONS ───────────────────────────

# Course-related categories (require a course_code)
COURSE_CATEGORIES = [
    "books", "notes", "solutions", "psqs",
    "videos", "utilities", "syllabus", "outline", "routine",
]

# Global topics — course-independent
GLOBAL_CATEGORIES = [
    "broadcast", "calendar", "advisor", "regpay",
]


async def get_courses_current_semester() -> List[dict]:
    """
    Return courses for current semester from semesters.courses JSONB.
    Each entry: {code: str, title: str}
    """
    import json
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT courses FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        if not row:
            return []
        courses_raw = row["courses"]
        if isinstance(courses_raw, str):
            try:
                courses_raw = json.loads(courses_raw)
            except Exception:
                return []
        return courses_raw if isinstance(courses_raw, list) else []


# Keep old name as alias for backward compat
async def get_distinct_courses_current_semester() -> List[str]:
    courses = await get_courses_current_semester()
    return [c["code"] for c in courses if "code" in c]


async def get_user_subscriptions(user_id: int) -> List[dict]:
    """Return all subscriptions for a user as list of {course_code, category}."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT course_code, category FROM subscriptions WHERE user_id = $1",
            user_id
        )
        return [dict(r) for r in rows]


async def save_user_subscriptions(
    user_id: int,
    course_subs: List[dict],   # [{"course_code": "CSE322", "category": "notes"}, ...]
    global_subs: List[str],    # ["broadcast", "calendar", ...]
    preserve_global: bool = False
):
    """
    Replace course+category subscriptions for user.
    Global subs only replaced if preserve_global=False.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if preserve_global:
                # Only delete course subscriptions
                await conn.execute("""
                    DELETE FROM subscriptions
                    WHERE user_id = $1 AND course_code IS NOT NULL
                """, user_id)
            else:
                # Delete everything and reinsert
                await conn.execute(
                    "DELETE FROM subscriptions WHERE user_id = $1", user_id
                )
                # Reinsert globals
                for cat in global_subs:
                    await conn.execute("""
                        INSERT INTO subscriptions (user_id, course_code, category)
                        VALUES ($1, NULL, $2) ON CONFLICT DO NOTHING
                    """, user_id, cat)

            # Insert course subs
            for sub in course_subs:
                await conn.execute("""
                    INSERT INTO subscriptions (user_id, course_code, category)
                    VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                """, user_id, sub["course_code"], sub["category"])


async def ensure_broadcast_subscription(user_id: int):
    """Auto-subscribe user to broadcast on first registration."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Delete duplicates first, keep only one
        await conn.execute("""
            DELETE FROM subscriptions
            WHERE user_id = $1 AND course_code IS NULL AND category = 'broadcast'
        """, user_id)
        await conn.execute("""
            INSERT INTO subscriptions (user_id, course_code, category)
            VALUES ($1, NULL, 'broadcast')
        """, user_id)


async def reset_course_subscriptions_all():
    """Called on new semester — wipe all course+category subs for all users."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM subscriptions WHERE course_code IS NOT NULL
        """)


async def get_subscribers_for(
    category: str,
    course_code: str = None
) -> List[int]:
    """
    Get user_ids subscribed to a specific category.
    - course_code=None → global category (broadcast, calendar, etc.)
    - course_code set  → course+category pair
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if course_code is None:
            rows = await conn.fetch("""
                SELECT DISTINCT user_id FROM subscriptions
                WHERE course_code IS NULL AND category = $1
            """, category)
        else:
            rows = await conn.fetch("""
                SELECT DISTINCT user_id FROM subscriptions
                WHERE course_code = $1 AND category = $2
            """, course_code, category)
        return [r["user_id"] for r in rows]


async def toggle_global_subscription(user_id: int, category: str) -> bool:
    """Toggle a global category sub. Returns True if now subscribed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT 1 FROM subscriptions
            WHERE user_id = $1 AND course_code IS NULL AND category = $2
        """, user_id, category)
        if exists:
            await conn.execute("""
                DELETE FROM subscriptions
                WHERE user_id = $1 AND course_code IS NULL AND category = $2
            """, user_id, category)
            return False
        else:
            await conn.execute("""
                INSERT INTO subscriptions (user_id, course_code, category)
                VALUES ($1, NULL, $2) ON CONFLICT DO NOTHING
            """, user_id, category)
            return True


# ─────────────────────────── REPORTS ───────────────────────────

async def insert_report(reporter_id: int, resource_id: int, reason: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO reports (reporter_id, resource_id, reason)
            VALUES ($1, $2, $3) RETURNING id
        """, reporter_id, resource_id, reason)


async def get_pending_reports() -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT r.*, u.full_name, res.title as resource_title
            FROM reports r
            JOIN users u ON u.user_id = r.reporter_id
            JOIN resources res ON res.id = r.resource_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC
        """)


async def update_report_status(report_id: int, status: str, reviewed_by: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE reports SET status = $2, reviewed_by = $3
            WHERE id = $1
        """, report_id, status, reviewed_by)


async def count_false_reports(reporter_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            SELECT COUNT(*) FROM reports
            WHERE reporter_id = $1 AND status = 'rejected'
        """, reporter_id)


# ─────────────────────────── ANONYMOUS Q&A ───────────────────────────

async def insert_anon_question(user_id: int, question: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO anon_questions (user_id, question)
            VALUES ($1, $2) RETURNING id
        """, user_id, question)


async def get_pending_anon_questions() -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM anon_questions
            WHERE is_published = FALSE AND answer IS NULL
            ORDER BY created_at ASC
        """)


async def answer_anon_question(question_id: int, answer: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE anon_questions
            SET answer = $2, is_published = TRUE, published_at = NOW()
            WHERE id = $1
        """, question_id, answer)


# ─────────────────────────── EXAM EVENTS ───────────────────────────

async def get_active_exam_events() -> List[asyncpg.Record]:
    """Legacy stub — kept for any old references. Use get_active_schedules instead."""
    return await get_active_schedules()


# ─────────────────────────── ANALYTICS ───────────────────────────

async def log_event(event_type: str, user_id: int = None, resource_id: int = None, meta: dict = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
        await conn.execute("""
            INSERT INTO analytics (event_type, user_id, resource_id, meta)
            VALUES ($1, $2, $3, $4)
        """, event_type, user_id, resource_id, json.dumps(meta or {}))


async def get_weekly_stats() -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        downloads = await conn.fetchval("""
            SELECT COUNT(*) FROM analytics
            WHERE event_type = 'download'
              AND created_at >= NOW() - INTERVAL '7 days'
        """)
        uploads = await conn.fetchval("""
            SELECT COUNT(*) FROM resources
            WHERE created_at >= NOW() - INTERVAL '7 days' AND is_active = TRUE
        """)
        active_users = await conn.fetchval("""
            SELECT COUNT(DISTINCT user_id) FROM analytics
            WHERE created_at >= NOW() - INTERVAL '7 days'
        """)
        return {
            "downloads": downloads or 0,
            "uploads": uploads or 0,
            "active_users": active_users or 0
        }


async def get_leaderboard(limit: int = 5) -> List[asyncpg.Record]:
    """Top users by stars."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, username, full_name, stars FROM users "
            "WHERE is_member = TRUE ORDER BY stars DESC LIMIT $1",
            limit
        )

async def search_books_current_semester(query: str, limit: int = 20):
    """Search books in current semester only."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_sem = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        q = f"%{query}%"
        if current_sem:
            return await conn.fetch("""
                SELECT * FROM books
                WHERE semester_id = $1
                AND (
                    title        ILIKE $2 OR
                    authors      ILIKE $2 OR
                    subject      ILIKE $2 OR
                    course_codes ILIKE $2 OR
                    uid          ILIKE $2 OR
                    tags::text   ILIKE $2
                )
                ORDER BY access_count DESC
                LIMIT $3
            """, current_sem, q, limit)
        # Fallback: no semester set — return all
        return await conn.fetch("""
            SELECT * FROM books
            WHERE (
                title        ILIKE $1 OR
                authors      ILIKE $1 OR
                subject      ILIKE $1 OR
                course_codes ILIKE $1 OR
                uid          ILIKE $1 OR
                tags::text   ILIKE $1
            )
            ORDER BY access_count DESC
            LIMIT $2
        """, q, limit)


async def search_books_all(query: str, limit: int = 20):
    """Search ALL books regardless of semester (admin use)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        q = f"%{query}%"
        return await conn.fetch("""
            SELECT b.*, s.name as semester_name
            FROM books b
            LEFT JOIN semesters s ON s.id = b.semester_id
            WHERE (
                b.title        ILIKE $1 OR
                b.authors      ILIKE $1 OR
                b.subject      ILIKE $1 OR
                b.course_codes ILIKE $1 OR
                b.uid          ILIKE $1 OR
                b.tags::text   ILIKE $1
            )
            ORDER BY b.access_count DESC
            LIMIT $2
        """, q, limit)

# ─────────────────────────── EXAM SYSTEM v4 ──────────────────────────────────
# exam_events table dropped. All data lives in exam_schedule + exam_sections.
# exam_schedule now has: routine_file_id, routine_file_type, semester_id per row.

async def insert_exam_schedule_single(
    course_code: str,
    course_name: str | None,
    exam_title: str | None,
    exam_date,
    start_time: str,
    end_time: str,
    slot_label: str,
    routine_file_id: str | None,
    routine_file_type: str | None,
    semester_id: int | None,
    sections: list,
) -> int:
    """Insert one exam schedule row and its section rows. Returns schedule_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            schedule_id = await conn.fetchval("""
                INSERT INTO exam_schedule
                    (course_code, course_name, exam_title, exam_date,
                     start_time, end_time, slot_label,
                     routine_file_id, routine_file_type, semester_id,
                     is_active)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, FALSE)
                RETURNING id
            """,
                course_code,
                course_name,
                exam_title,
                exam_date,
                start_time,
                end_time,
                slot_label,
                routine_file_id,
                routine_file_type or "document",
                semester_id,
            )
            for sec in sections:
                await conn.execute("""
                    INSERT INTO exam_sections
                        (schedule_id, section, room, seats, teacher)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    schedule_id,
                    sec["section"].upper(),
                    sec["room"],
                    sec.get("seats"),
                    sec.get("teacher"),
                )
    return schedule_id


async def get_all_exam_schedules() -> list:
    """All schedules ordered by date — for admin list/delete."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                s.id,
                s.course_code,
                s.course_name,
                s.exam_date,
                s.start_time,
                s.end_time,
                s.slot_label,
                s.is_active,
                s.routine_file_id,
                COUNT(DISTINCT sec.section) AS section_count
            FROM exam_schedule s
            LEFT JOIN exam_sections sec ON sec.schedule_id = s.id
            GROUP BY s.id
            ORDER BY s.exam_date ASC, s.start_time ASC
        """)


async def delete_exam_schedule(schedule_id: int) -> None:
    """Delete a schedule and its sections/notifications (CASCADE)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM exam_schedule WHERE id = $1", schedule_id
        )


async def section_has_active_exam(section: str) -> bool:
    """Check if section has any upcoming exam in current semester."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 1
            FROM exam_sections sec
            JOIN exam_schedule s ON s.id = sec.schedule_id
            WHERE s.semester_id = (SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1)
              AND s.exam_date >= CURRENT_DATE
              AND UPPER(sec.section) = UPPER($1)
            LIMIT 1
        """, section)
        return row is not None


async def get_active_schedules() -> list:
    """Get upcoming exams in current semester (date-based, no is_active filter)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT *
            FROM exam_schedule
            WHERE semester_id = (SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1)
              AND exam_date >= CURRENT_DATE
            ORDER BY exam_date ASC, start_time ASC
        """)


async def get_exam_info_for_section(section: str) -> list:
    """Active schedules with section-specific room data."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                s.id            AS schedule_id,
                s.course_code,
                s.course_name,
                s.exam_title,
                s.exam_date,
                s.start_time,
                s.end_time,
                s.slot_label,
                s.routine_file_id,
                s.routine_file_type,
                sec.room,
                sec.seats,
                sec.teacher
            FROM exam_schedule s
            LEFT JOIN exam_sections sec
                   ON sec.schedule_id = s.id
                  AND UPPER(sec.section) = UPPER($1)
            WHERE s.semester_id = (SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1)
              AND s.exam_date >= CURRENT_DATE
            ORDER BY s.exam_date ASC, s.start_time ASC
        """, section)


async def get_upcoming_schedules_for_activation() -> list:
    """
    Activation logic (global, no event grouping):
    - Find the earliest exam_date that has ANY inactive schedule.
    - If it's the very first date ever: activate when (first_date - 3 days) <= TODAY
    - If there's a previous date: activate when that previous date's MAX(end_time)
      has passed in Asia/Dhaka timezone.
    - All schedules on that one date activate together.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            WITH now_dhaka AS (
                SELECT NOW() AT TIME ZONE 'Asia/Dhaka' AS now_dt
            ),
            -- The next date that still has inactive schedules
            next_inactive_date AS (
                SELECT MIN(exam_date) AS target_date
                FROM exam_schedule
                WHERE is_active = FALSE
            ),
            -- The most recent date that is fully past (all end_times passed)
            prev_done_date AS (
                SELECT MAX(exam_date) AS prev_date,
                       MAX(end_time)  AS prev_max_end
                FROM exam_schedule
                WHERE exam_date < (SELECT target_date FROM next_inactive_date)
            ),
            -- Is there any date before target_date?
            has_prev AS (
                SELECT (prev_date IS NOT NULL) AS exists
                FROM prev_done_date
            )
            SELECT s.id, s.course_code, s.exam_date, s.start_time, s.end_time, s.slot_label
            FROM exam_schedule s
            CROSS JOIN now_dhaka nd
            CROSS JOIN next_inactive_date nid
            CROSS JOIN prev_done_date pdd
            CROSS JOIN has_prev hp
            WHERE s.is_active = FALSE
              AND s.exam_date = nid.target_date
              AND (
                -- No previous date → this is the first exam, activate 3 days before
                (hp.exists = FALSE
                 AND nid.target_date - INTERVAL '3 days' <= nd.now_dt::date)
                OR
                -- Previous date exists → activate after its last end_time passes
                (hp.exists = TRUE
                 AND (pdd.prev_date + pdd.prev_max_end::time) < nd.now_dt)
              )
            ORDER BY s.start_time
        """)


async def get_schedules_to_deactivate() -> list:
    """Deactivate schedules whose end_time has passed in Asia/Dhaka timezone."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT *
            FROM exam_schedule
            WHERE is_active = TRUE
              AND (exam_date + end_time::time) < (NOW() AT TIME ZONE 'Asia/Dhaka')
        """)


async def activate_schedule(schedule_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE exam_schedule SET is_active = TRUE WHERE id = $1", schedule_id
        )


async def deactivate_schedule(schedule_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE exam_schedule SET is_active = FALSE WHERE id = $1", schedule_id
        )


async def get_users_with_section_for_notification(schedule_id: int) -> list:
    """
    Returns users who have a section, are active members,
    haven't been notified yet, and have a room in exam_sections for this schedule.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                u.user_id,
                u.section,
                sec.room,
                sec.seats,
                sec.teacher
            FROM users u
            JOIN exam_sections sec
                ON UPPER(sec.section) = UPPER(u.section)
               AND sec.schedule_id = $1
            WHERE u.section IS NOT NULL
              AND u.is_member = TRUE
              AND NOT EXISTS (
                SELECT 1 FROM exam_notifications n
                WHERE n.schedule_id = $1 AND n.user_id = u.user_id
              )
        """, schedule_id)


async def mark_exam_notified(schedule_id: int, user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO exam_notifications (schedule_id, user_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, schedule_id, user_id)


async def set_user_section(user_id: int, section: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET section = $1 WHERE user_id = $2",
            section.upper(), user_id
        )


async def get_current_semester_courses() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json as _json
        row = await conn.fetchrow(
            "SELECT courses FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        if not row:
            return {}
        courses = row["courses"]
        if isinstance(courses, str):
            courses = _json.loads(courses)
        return {
            c["code"]: {"name": c["name"], "abbr": c.get("abbr", "")}
            for c in courses
        }


async def get_current_semester_name() -> str:
    """Returns the name of the current semester, e.g. 'Summer 2026'. Empty string if none."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        return row["name"].strip() if row and row["name"] else ""