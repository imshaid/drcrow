"""
Database layer — asyncpg connection pool + full schema init.
All tables, triggers, and indexes created here on startup.
"""

import asyncpg
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # strip query params from URL — asyncpg doesn't understand them
        import re as _re
        dsn = _re.sub(r'\?.*$', '', settings.DATABASE_URL)
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
            ssl='require',               # Supabase enforces SSL
            statement_cache_size=0,      # required for Supabase transaction pooler
        )
    return _pool


async def init_db():
    """Create all tables and triggers if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id             BIGINT PRIMARY KEY,
                username            TEXT,
                full_name           TEXT,
                points              INTEGER DEFAULT 0,
                rank                TEXT DEFAULT 'Egg',
                streak_days         INTEGER DEFAULT 0,
                last_active         TIMESTAMPTZ,
                download_count      INTEGER DEFAULT 0,
                upload_count        INTEGER DEFAULT 0,
                joined_at           TIMESTAMPTZ DEFAULT NOW(),
                left_at             TIMESTAMPTZ,
                is_member           BOOLEAN DEFAULT TRUE,
                warned_today        BOOLEAN DEFAULT FALSE,
                last_warned_at      TIMESTAMPTZ,
                spam_flags          INTEGER DEFAULT 0,
                bot_flags           INTEGER DEFAULT 0,
                content_flags       INTEGER DEFAULT 0,
                false_report_flags  INTEGER DEFAULT 0,
                muted_until         TIMESTAMPTZ
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS semesters (
                id          SERIAL PRIMARY KEY,
                uid         TEXT UNIQUE NOT NULL DEFAULT 'sem0',
                name        TEXT NOT NULL,
                courses     JSONB DEFAULT '[]',
                is_current  BOOLEAN DEFAULT FALSE,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Migrations for existing installs
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='semesters' AND column_name='uid'
                ) THEN
                    ALTER TABLE semesters ADD COLUMN uid TEXT;
                    UPDATE semesters SET uid = 'sem' || id::TEXT WHERE uid IS NULL;
                    ALTER TABLE semesters ALTER COLUMN uid SET NOT NULL;
                    ALTER TABLE semesters ADD CONSTRAINT semesters_uid_unique UNIQUE (uid);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='semesters' AND column_name='courses'
                ) THEN
                    ALTER TABLE semesters ADD COLUMN courses JSONB DEFAULT '[]';
                END IF;
            END $$;
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS resources (
                id              SERIAL PRIMARY KEY,
                title           TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                file_type       TEXT NOT NULL,
                course_code     TEXT,
                category        TEXT,
                semester_id     INTEGER REFERENCES semesters(id),
                tags            JSONB DEFAULT '[]',
                is_active       BOOLEAN DEFAULT TRUE,
                is_featured     BOOLEAN DEFAULT FALSE,
                access_count    INTEGER DEFAULT 0,
                uploaded_by     BIGINT,
                approved_by     BIGINT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS search_index (
                id              SERIAL PRIMARY KEY,
                resource_id     INTEGER REFERENCES resources(id) ON DELETE CASCADE,
                text_content    TEXT,
                tsv             TSVECTOR,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id      SERIAL PRIMARY KEY,
                name    TEXT UNIQUE NOT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                course_code TEXT,                         -- NULL for global topics
                category    TEXT NOT NULL,                -- resource category or global topic
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, course_code, category)
            )
        """)

        # Migration: if old schema (no category column) exists, recreate cleanly
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='subscriptions' AND column_name='category'
                ) THEN
                    DROP TABLE IF EXISTS subscriptions CASCADE;
                    CREATE TABLE subscriptions (
                        id          SERIAL PRIMARY KEY,
                        user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                        course_code TEXT,
                        category    TEXT NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(user_id, course_code, category)
                    );
                END IF;
            END $$;
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS flags (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id),
                flag_type   TEXT NOT NULL,
                reason      TEXT,
                actioned_by BIGINT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          SERIAL PRIMARY KEY,
                reporter_id BIGINT REFERENCES users(user_id),
                resource_id INTEGER REFERENCES resources(id),
                reason      TEXT,
                status      TEXT DEFAULT 'pending',
                reviewed_by BIGINT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anon_questions (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT REFERENCES users(user_id),
                question        TEXT NOT NULL,
                answer          TEXT,
                is_published    BOOLEAN DEFAULT FALSE,
                published_at    TIMESTAMPTZ,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS exam_events (
                id              SERIAL PRIMARY KEY,
                name            TEXT NOT NULL,
                exam_date       DATE NOT NULL,
                course_codes    JSONB DEFAULT '[]',
                is_active       BOOLEAN DEFAULT TRUE,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                id          SERIAL PRIMARY KEY,
                event_type  TEXT NOT NULL,
                user_id     BIGINT,
                resource_id INTEGER,
                meta        JSONB DEFAULT '{}',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id      SERIAL PRIMARY KEY,
                message TEXT NOT NULL,
                sent_by BIGINT,
                sent_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_resources (
                id           SERIAL PRIMARY KEY,
                submitted_by BIGINT REFERENCES users(user_id),
                file_id      TEXT NOT NULL,
                file_type    TEXT NOT NULL,
                title        TEXT,
                course_code  TEXT,
                category     TEXT,
                tags         JSONB DEFAULT '[]',
                status       TEXT DEFAULT 'pending',
                reviewed_by  BIGINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS membership_cache (
                user_id   BIGINT PRIMARY KEY,
                is_member BOOLEAN NOT NULL,
                left_at   TIMESTAMPTZ,
                cached_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                uid           TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                authors       TEXT NOT NULL,
                edition       TEXT,
                subject       TEXT,
                course_codes  TEXT,
                file_id       TEXT NOT NULL,
                cover_file_id TEXT,
                cover_url     TEXT,
                tags          JSONB DEFAULT '[]',
                semester_id   INTEGER REFERENCES semesters(id),
                access_count  INTEGER DEFAULT 0,
                uploaded_by   BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS solution_manuals (
                uid          TEXT PRIMARY KEY,
                book_uid     TEXT NOT NULL REFERENCES books(uid) ON DELETE CASCADE,
                file_id      TEXT NOT NULL,
                access_count INTEGER DEFAULT 0,
                uploaded_by  BIGINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                uid          TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                subject      TEXT,
                course_code  TEXT,
                semester_id  INTEGER REFERENCES semesters(id),
                file_id      TEXT NOT NULL,
                file_type    TEXT NOT NULL,
                cover_file_id TEXT,
                cover_url    TEXT,
                tags         JSONB DEFAULT '[]',
                access_count INTEGER DEFAULT 0,
                uploaded_by  BIGINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS psqs (
                uid           TEXT PRIMARY KEY,
                title         TEXT,
                file_id       TEXT NOT NULL,
                cover_file_id TEXT,
                cover_url     TEXT,
                tags          JSONB DEFAULT '["psq", "previous", "questions"]',
                semester_id   INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count  INTEGER DEFAULT 0,
                uploaded_by   BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_psqs_tags ON psqs USING GIN(tags)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS solves (
                uid           TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                subject       TEXT,
                course_code   TEXT,
                file_id       TEXT NOT NULL,
                file_type     TEXT NOT NULL DEFAULT 'document',
                cover_file_id TEXT,
                cover_url     TEXT,
                tags          JSONB DEFAULT '[]',
                semester_id   INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count  INTEGER DEFAULT 0,
                uploaded_by   BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                uid        TEXT PRIMARY KEY,
                solve_uid  TEXT NOT NULL REFERENCES solves(uid) ON DELETE CASCADE,
                title      TEXT,
                file_id    TEXT NOT NULL,
                file_type  TEXT NOT NULL DEFAULT 'document',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS solve_deliveries (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                solve_uid  TEXT NOT NULL REFERENCES solves(uid) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, solve_uid)
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_solves_course      ON solves(course_code);
            CREATE INDEX IF NOT EXISTS idx_solves_tags        ON solves USING GIN(tags);
            CREATE INDEX IF NOT EXISTS idx_corrections_solve  ON corrections(solve_uid);
            CREATE INDEX IF NOT EXISTS idx_deliveries_solve   ON solve_deliveries(solve_uid);
            CREATE INDEX IF NOT EXISTS idx_deliveries_user    ON solve_deliveries(user_id)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vidocs (
                uid          TEXT PRIMARY KEY,
                subject      TEXT,
                course_code  TEXT,
                messages     JSONB DEFAULT '[]',
                tags         JSONB DEFAULT '[]',
                thumbnail_url TEXT,
                cover_file_id TEXT,
                semester_id  INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count INTEGER DEFAULT 0,
                uploaded_by  BIGINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vidocs_course ON vidocs(course_code);
            CREATE INDEX IF NOT EXISTS idx_vidocs_tags   ON vidocs USING GIN(tags)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS utilities (
                uid           TEXT PRIMARY KEY,
                category      TEXT NOT NULL,
                file_id       TEXT,
                file_type     TEXT,
                thumbnail_url TEXT,
                message_text     TEXT,
                message_entities JSONB DEFAULT '[]',
                url           TEXT,
                url_title     TEXT,
                tags          JSONB DEFAULT '[]',
                semester_id   INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count  INTEGER DEFAULT 0,
                uploaded_by   BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_utilities_category ON utilities(category);
            CREATE INDEX IF NOT EXISTS idx_utilities_tags     ON utilities USING GIN(tags)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS waivers (
                uid              TEXT PRIMARY KEY,
                semester_name    TEXT NOT NULL,
                tuition_fee      INTEGER NOT NULL,
                semester_fee     INTEGER NOT NULL,
                file_id          TEXT,
                file_type        TEXT,
                thumbnail_url    TEXT,
                cover_file_id    TEXT,
                url              TEXT,
                url_title        TEXT,
                tags             JSONB DEFAULT '[]',
                semester_id      INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count     INTEGER DEFAULT 0,
                uploaded_by      BIGINT,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_waivers_tags ON waivers USING GIN(tags)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS regpay (
                uid           TEXT PRIMARY KEY,
                semester      TEXT NOT NULL,
                file_ids      JSONB DEFAULT '[]',
                thumbnail_url TEXT,
                cover_file_id TEXT,
                tags          JSONB DEFAULT '[]',
                semester_id   INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                access_count  INTEGER DEFAULT 0,
                uploaded_by   BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regpay_tags ON regpay USING GIN(tags)
        """)

        await conn.execute("""
            CREATE OR REPLACE FUNCTION update_tsv()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.tsv := to_tsvector('english', COALESCE(NEW.text_content, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)

        await conn.execute("""
            DROP TRIGGER IF EXISTS tsv_update ON search_index;
            CREATE TRIGGER tsv_update
                BEFORE INSERT OR UPDATE ON search_index
                FOR EACH ROW EXECUTE FUNCTION update_tsv()
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_resources_course   ON resources(course_code);
            CREATE INDEX IF NOT EXISTS idx_resources_category ON resources(category);
            CREATE INDEX IF NOT EXISTS idx_resources_active   ON resources(is_active);
            CREATE INDEX IF NOT EXISTS idx_resources_semester ON resources(semester_id);
            CREATE INDEX IF NOT EXISTS idx_resources_tags     ON resources USING GIN(tags);
            CREATE INDEX IF NOT EXISTS idx_search_tsv         ON search_index USING GIN(tsv);
            CREATE INDEX IF NOT EXISTS idx_analytics_event    ON analytics(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_users_member       ON users(is_member);
            CREATE INDEX IF NOT EXISTS idx_books_tags         ON books USING GIN(tags);
            CREATE INDEX IF NOT EXISTS idx_books_semester     ON books(semester_id);
            CREATE INDEX IF NOT EXISTS idx_solutions_book     ON solution_manuals(book_uid);
            CREATE INDEX IF NOT EXISTS idx_notes_tags         ON notes USING GIN(tags);
            CREATE INDEX IF NOT EXISTS idx_notes_course       ON notes(course_code);
            CREATE INDEX IF NOT EXISTS idx_notes_semester     ON notes(semester_id)
        """)

        existing = await conn.fetchval("SELECT COUNT(*) FROM semesters")
        if existing == 0:
            await conn.execute("""
                INSERT INTO semesters (uid, name, courses, is_current)
                VALUES ('sp26', 'Spring 2026', '[]', TRUE)
            """)

        # Migration: add semester_id to tables that didn't have it
        for tbl in ('psqs', 'solves', 'vidocs', 'utilities', 'waivers', 'regpay'):
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='{tbl}' AND column_name='semester_id'
                    ) THEN
                        ALTER TABLE {tbl}
                            ADD COLUMN semester_id INTEGER REFERENCES semesters(id) ON DELETE SET NULL;
                    END IF;
                END $$;
            """)

        # Backfill semester_id=current for existing rows that have NULL
        current_sem_id = await conn.fetchval(
            "SELECT id FROM semesters WHERE is_current = TRUE LIMIT 1"
        )
        if current_sem_id:
            for tbl in ('psqs', 'solves', 'vidocs', 'utilities', 'waivers', 'regpay'):
                await conn.execute(
                    f"UPDATE {tbl} SET semester_id = $1 WHERE semester_id IS NULL",
                    current_sem_id
                )

        # Deduplicate broadcast subscriptions (NULL course_code breaks UNIQUE constraint)
        await conn.execute("""
            DELETE FROM subscriptions a
            USING subscriptions b
            WHERE a.id > b.id
              AND a.user_id = b.user_id
              AND a.course_code IS NULL
              AND b.course_code IS NULL
              AND a.category = b.category
        """)

        # Partial unique index to prevent future duplicates for NULL course_code
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_global_unique
            ON subscriptions (user_id, category)
            WHERE course_code IS NULL
        """)

    logger.info("All tables, triggers, and indexes ready.")


async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

# ── Multi-keyword search helper ──────────────────────────────────────────────

def build_token_where(query: str, columns: list, start_idx: int = 1) -> tuple:
    """
    Build AND-per-token WHERE clauses for multi-keyword ILIKE search.

    For each token, generates: (col1 ILIKE $n OR col2 ILIKE $n OR ...)
    All token blocks are joined with AND — so ALL keywords must match somewhere.

    Returns (where_clause: str, params: list, next_idx: int)

    Example:
        where, params, idx = build_token_where("dbms note", ["title", "tags::text"], start_idx=2)
        # where = "(title ILIKE $2 OR tags::text ILIKE $2) AND (title ILIKE $3 OR tags::text ILIKE $3)"
        # params = ["%dbms%", "%note%"]
        # idx = 4
    """
    tokens = [t.strip() for t in query.strip().split() if t.strip()]
    if not tokens:
        tokens = [query.strip()]

    clauses = []
    params = []
    idx = start_idx

    for token in tokens:
        col_conditions = " OR ".join(f"{col} ILIKE ${idx}" for col in columns)
        clauses.append(f"({col_conditions})")
        params.append(f"%{token}%")
        idx += 1

    where = " AND ".join(clauses)
    return where, params, idx