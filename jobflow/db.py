"""PostgreSQL database backend for JobFlow (Neon serverless).

Replaces JSON file storage with persistent PostgreSQL when DATABASE_URL is set.
All functions mirror the linkedin_store.py API so the rest of the codebase
can switch backends transparently.

Connection: uses psycopg2 with a threaded connection pool (1-3 connections),
TCP keepalive to survive Neon's idle timeout, and connection validation.
TTL: jobs auto-expire after 3 days unless user sets status to Tracking/Applied.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.pool
import psycopg2.extras

from .linkedin_store import (
    LINKEDIN_STATUSES, KEEP_STATUSES,
    _rescore_entry, _dedup_key, _parse_iso, format_recency,
    _bucket_minutes, _bucket_start, _bucket_label, _bucket_key,
    backfill_job,
)

TTL_DAYS = 3
_pool = None


def _get_pool():
    """Get or create the connection pool (thread-safe)."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 5,
            dsn=os.environ["DATABASE_URL"],
            sslmode="require",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    return _pool


def get_conn():
    """Get a validated connection from the pool.

    Checks that the connection is alive before returning it. If the
    connection was killed by Neon's idle timeout, it's discarded and
    a fresh one is returned.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Quick validation — detects dead connections from Neon idle timeout
        conn.cursor().execute("SELECT 1")
    except Exception:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    return conn


def put_conn(conn):
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def init_db():
    """Create tables if they don't exist. Idempotent."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    url                 TEXT PRIMARY KEY,
                    company             TEXT NOT NULL DEFAULT '',
                    title               TEXT NOT NULL DEFAULT '',
                    location            TEXT NOT NULL DEFAULT '',
                    description_preview TEXT NOT NULL DEFAULT '',
                    search_term         TEXT NOT NULL DEFAULT '',
                    date_posted         TEXT NOT NULL DEFAULT '',
                    variant             TEXT NOT NULL DEFAULT 'se',
                    reason              TEXT NOT NULL DEFAULT '',
                    first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    score               INTEGER NOT NULL DEFAULT 0,
                    score_pct           INTEGER NOT NULL DEFAULT 0,
                    ai_score            INTEGER,
                    ai_reason           TEXT NOT NULL DEFAULT '',
                    ai_model            TEXT,
                    recommended         BOOLEAN NOT NULL DEFAULT FALSE,
                    level               TEXT NOT NULL DEFAULT 'Unknown',
                    min_exp             INTEGER,
                    max_exp             INTEGER,
                    competition         INTEGER NOT NULL DEFAULT 0,
                    keyword_hits        INTEGER NOT NULL DEFAULT 0,
                    status              TEXT NOT NULL DEFAULT '',
                    h1b                 BOOLEAN NOT NULL DEFAULT FALSE,
                    reject_reason       TEXT,
                    expires_at          TIMESTAMPTZ
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
                CREATE INDEX IF NOT EXISTS idx_jobs_level ON jobs (level);
                CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen);
                CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs (last_seen);
                CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs (expires_at)
                    WHERE expires_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_jobs_company_title
                    ON jobs (LOWER(company), LOWER(title));

                CREATE TABLE IF NOT EXISTS seen_jobs (
                    url     TEXT PRIMARY KEY,
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_seen_jobs_seen_at ON seen_jobs (seen_at);
            """)
            # Migration for existing databases
            cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_model TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row, columns) -> dict:
    """Convert a database row to a job dict matching the JSON store format."""
    d = dict(zip(columns, row))
    for ts_field in ("first_seen", "last_seen", "expires_at"):
        if d.get(ts_field) and isinstance(d[ts_field], datetime):
            d[ts_field] = d[ts_field].isoformat()
    if d.get("ai_score") is None:
        d.pop("ai_score", None)
    if d.get("ai_reason") == "":
        d["ai_reason"] = ""
    return d


JOB_COLUMNS = [
    "url", "company", "title", "location", "description_preview",
    "search_term", "date_posted", "variant", "reason",
    "first_seen", "last_seen",
    "score", "score_pct", "ai_score", "ai_reason", "ai_model", "recommended",
    "level", "min_exp", "max_exp", "competition", "keyword_hits",
    "status", "h1b", "reject_reason", "expires_at",
]


def _expires_at_for_status(status: str):
    """Return expires_at value based on status. None = never expires."""
    if status in KEEP_STATUSES:
        return None
    return datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------

def merge_scan_results(scan_results: list[dict]) -> int:
    """Upsert scan results into the database. Returns count of new jobs inserted.

    Uses INSERT ... ON CONFLICT DO UPDATE (upsert) to avoid race conditions.
    - New jobs: inserted with expires_at = NOW() + 3 days
    - Existing jobs: last_seen updated, expires_at refreshed, description updated if longer
    - User status is always preserved
    - AI scores carried from scan if not already present
    - All jobs re-scored with current filter logic
    """
    if not scan_results:
        return 0

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires = now + timedelta(days=TTL_DAYS)

    # Pre-dedup scan results by company+title (same as JSON store logic)
    seen_combos: dict[str, dict] = {}
    for entry in scan_results:
        co = entry.get("company", "").lower().strip()
        title = entry.get("title", "").lower().strip()
        combo = f"{co}|{title}"
        if combo in seen_combos:
            if not seen_combos[combo].get("url") and entry.get("url"):
                seen_combos[combo] = entry
        else:
            seen_combos[combo] = entry
    deduped = list(seen_combos.values())

    conn = get_conn()
    new_count = 0
    try:
        with conn.cursor() as cur:
            for entry in deduped:
                url = entry.get("url", "")
                if not url:
                    continue

                # Re-score entry
                scored = _rescore_entry(dict(entry))
                posted = entry.get("date_posted", "") or now_iso
                desc = scored.get("description_preview", "")

                # Upsert: insert new or update existing
                cur.execute("""
                    INSERT INTO jobs (
                        url, company, title, location, description_preview,
                        search_term, date_posted, variant, reason,
                        first_seen, last_seen,
                        score, score_pct, ai_score, ai_reason, ai_model, recommended,
                        level, min_exp, max_exp, competition, keyword_hits,
                        status, h1b, reject_reason, expires_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (url) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        description_preview = CASE
                            WHEN LENGTH(EXCLUDED.description_preview) > LENGTH(jobs.description_preview)
                            THEN EXCLUDED.description_preview
                            ELSE jobs.description_preview
                        END,
                        score = EXCLUDED.score,
                        score_pct = CASE
                            WHEN jobs.ai_score IS NOT NULL THEN jobs.score_pct
                            ELSE EXCLUDED.score_pct
                        END,
                        ai_score = COALESCE(jobs.ai_score, EXCLUDED.ai_score),
                        ai_reason = CASE
                            WHEN jobs.ai_score IS NOT NULL THEN jobs.ai_reason
                            ELSE EXCLUDED.ai_reason
                        END,
                        ai_model = CASE
                            WHEN jobs.ai_score IS NOT NULL THEN jobs.ai_model
                            ELSE EXCLUDED.ai_model
                        END,
                        recommended = CASE
                            WHEN jobs.ai_score IS NOT NULL THEN jobs.recommended
                            ELSE EXCLUDED.recommended
                        END,
                        level = EXCLUDED.level,
                        min_exp = EXCLUDED.min_exp,
                        max_exp = EXCLUDED.max_exp,
                        competition = EXCLUDED.competition,
                        keyword_hits = EXCLUDED.keyword_hits,
                        reason = EXCLUDED.reason,
                        variant = EXCLUDED.variant,
                        reject_reason = EXCLUDED.reject_reason,
                        expires_at = CASE
                            WHEN jobs.status IN ('Tracking', 'Applied') THEN NULL
                            ELSE EXCLUDED.expires_at
                        END
                    RETURNING (xmax = 0) AS inserted
                """, (
                    url,
                    scored.get("company", ""),
                    scored.get("title", ""),
                    scored.get("location", ""),
                    desc,
                    scored.get("search_term", ""),
                    scored.get("date_posted", ""),
                    scored.get("variant", "se"),
                    scored.get("reason", ""),
                    posted, now_iso,
                    scored.get("score", 0),
                    scored.get("score_pct", 0),
                    scored.get("ai_score"),
                    scored.get("ai_reason", ""),
                    scored.get("ai_model"),
                    scored.get("recommended", False),
                    scored.get("level", "Unknown"),
                    scored.get("min_exp"),
                    scored.get("max_exp"),
                    scored.get("competition", 0),
                    scored.get("keyword_hits", 0),
                    "",  # status (only for new inserts, ON CONFLICT preserves existing)
                    False,  # h1b
                    scored.get("reject_reason"),
                    expires,
                ))
                row = cur.fetchone()
                if row and row[0]:  # xmax = 0 means it was an INSERT, not UPDATE
                    new_count += 1

            # Prune expired jobs
            cur.execute("DELETE FROM jobs WHERE expires_at IS NOT NULL AND expires_at < NOW()")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return new_count


def update_job_status(url: str, status: str) -> bool:
    """Update a job's status. Sets expires_at based on new status."""
    if status not in LINKEDIN_STATUSES and status != "":
        return False

    expires = _expires_at_for_status(status)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if status in KEEP_STATUSES:
                cur.execute(
                    "UPDATE jobs SET status = %s, expires_at = NULL WHERE url = %s",
                    (status, url),
                )
            else:
                cur.execute(
                    "UPDATE jobs SET status = %s, expires_at = %s WHERE url = %s",
                    (status, expires, url),
                )
            found = cur.rowcount > 0
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_job(url: str) -> bool:
    """Permanently delete a job from the database."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE url = %s", (url,))
            found = cur.rowcount > 0
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def prune_expired_jobs() -> int:
    """Delete jobs whose TTL has expired. Returns count deleted."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE expires_at IS NOT NULL AND expires_at < NOW()")
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def get_filtered_jobs(
    status: str = "",
    level: str = "",
    query: str = "",
    search_term: str = "",
    time_range: str = "",
    bucket_filter: str = "",
    sort_col: str = "last_seen",
    sort_dir: str = "desc",
    tz_offset: int = 0,
) -> list[dict]:
    """Return filtered, sorted job list. Mirrors linkedin_store.get_filtered_jobs()."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    conditions = []
    params = []

    if time_range == "hour":
        conditions.append("first_seen >= %s")
        params.append(now_utc - timedelta(hours=1))
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("first_seen >= %s")
        params.append(local_midnight.astimezone(timezone.utc))
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("first_seen >= %s AND first_seen < %s")
        params.append((local_midnight - timedelta(days=1)).astimezone(timezone.utc))
        params.append(local_midnight.astimezone(timezone.utc))

    if bucket_filter:
        try:
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start = local_start.astimezone(timezone.utc)
            bucket_end = bucket_start + timedelta(minutes=bm)
            conditions.append("first_seen >= %s AND first_seen < %s")
            params.append(bucket_start)
            params.append(bucket_end)
        except (ValueError, TypeError):
            pass

    if status == "Recommended":
        conditions.append("recommended = TRUE")
        conditions.append("COALESCE(status, '') NOT IN ('Not Interested')")
    elif status:
        conditions.append("status = %s")
        params.append(status)

    if level and level != "All":
        conditions.append("level = %s")
        params.append(level)

    if query:
        conditions.append("(LOWER(company) LIKE %s OR LOWER(title) LIKE %s OR LOWER(location) LIKE %s)")
        q_like = f"%{query.lower().strip()}%"
        params.extend([q_like, q_like, q_like])

    if search_term:
        conditions.append("search_term = %s")
        params.append(search_term)

    where = " AND ".join(conditions) if conditions else "TRUE"

    allowed_sorts = {
        "last_seen", "first_seen", "score_pct", "ai_score", "score",
        "competition", "min_exp", "level", "company", "title",
    }
    if sort_col not in allowed_sorts:
        sort_col = "last_seen"
    direction = "ASC" if sort_dir == "asc" else "DESC"

    order = f"""
        CASE WHEN status IN ('Applied', 'Not Interested') THEN 1 ELSE 0 END,
        {sort_col} {direction} NULLS LAST,
        COALESCE(ai_score, 0) DESC,
        score_pct DESC
    """

    sql = f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE {where} ORDER BY {order}"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    result = []
    for row in rows:
        d = _row_to_dict(row, JOB_COLUMNS)
        d["_key"] = d["url"]
        d["_recency"] = format_recency(d.get("first_seen", ""))
        result.append(d)
    return result


def get_status_counts() -> dict[str, int]:
    """Return count of jobs per status + recommended count."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'Tracking') AS tracking,
                    COUNT(*) FILTER (WHERE status = 'Applied') AS applied,
                    COUNT(*) FILTER (WHERE status = 'Not Interested') AS not_interested,
                    COUNT(*) FILTER (WHERE recommended = TRUE AND COALESCE(status, '') NOT IN ('Not Interested')) AS recommended
                FROM jobs
            """)
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return {
        "All": row[0],
        "Tracking": row[1],
        "Applied": row[2],
        "Not Interested": row[3],
        "Recommended": row[4],
    }


def get_level_counts() -> dict[str, int]:
    """Return count of jobs per level tag."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT level, COUNT(*) FROM jobs GROUP BY level")
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    counts = {"All": 0, "New Grad": 0, "Entry": 0, "Mid": 0, "Unknown": 0}
    for level, cnt in rows:
        if level in counts:
            counts[level] = cnt
        else:
            counts["Unknown"] = counts.get("Unknown", 0) + cnt
    counts["All"] = sum(v for k, v in counts.items() if k != "All")
    return counts


def get_filtered_counts(
    time_range: str = "",
    bucket_filter: str = "",
    tz_offset: int = 0,
    query: str = "",
    search_term: str = "",
) -> dict:
    """Compute status + level counts with time/search filters applied."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    conditions = []
    params = []

    if time_range == "hour":
        conditions.append("first_seen >= %s")
        params.append(now_utc - timedelta(hours=1))
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("first_seen >= %s")
        params.append(local_midnight.astimezone(timezone.utc))
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("first_seen >= %s AND first_seen < %s")
        params.append((local_midnight - timedelta(days=1)).astimezone(timezone.utc))
        params.append(local_midnight.astimezone(timezone.utc))

    if bucket_filter:
        try:
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start = local_start.astimezone(timezone.utc)
            bucket_end = bucket_start + timedelta(minutes=bm)
            conditions.append("first_seen >= %s AND first_seen < %s")
            params.append(bucket_start)
            params.append(bucket_end)
        except (ValueError, TypeError):
            pass

    if query:
        conditions.append("(LOWER(company) LIKE %s OR LOWER(title) LIKE %s OR LOWER(location) LIKE %s)")
        q_like = f"%{query.lower().strip()}%"
        params.extend([q_like, q_like, q_like])

    if search_term:
        conditions.append("search_term = %s")
        params.append(search_term)

    where = " AND ".join(conditions) if conditions else "TRUE"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'Tracking') AS tracking,
                    COUNT(*) FILTER (WHERE status = 'Applied') AS applied,
                    COUNT(*) FILTER (WHERE status = 'Not Interested') AS not_interested,
                    COUNT(*) FILTER (WHERE recommended = TRUE AND COALESCE(status, '') NOT IN ('Not Interested')) AS recommended,
                    COUNT(*) FILTER (WHERE level = 'New Grad') AS new_grad,
                    COUNT(*) FILTER (WHERE level = 'Entry') AS entry,
                    COUNT(*) FILTER (WHERE level = 'Mid') AS mid,
                    COUNT(*) FILTER (WHERE level = 'Unknown') AS unknown
                FROM jobs WHERE {where}
            """, params)
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return {
        "status": {
            "All": row[0],
            "Tracking": row[1],
            "Applied": row[2],
            "Not Interested": row[3],
            "Recommended": row[4],
        },
        "level": {
            "All": row[0],
            "New Grad": row[5],
            "Entry": row[6],
            "Mid": row[7],
            "Unknown": row[8],
        },
    }


def get_time_counts(tz_offset: int = 0, time_range: str = "") -> dict:
    """Return time tab counts + dynamic bucket breakdown matching the active time range."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = local_midnight.astimezone(timezone.utc)
    yesterday_start_utc = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)

    # Determine bucket query window based on active time range
    if time_range == "hour":
        bucket_start_utc = one_hour_ago
        bucket_end_utc = None
    elif time_range == "today":
        bucket_start_utc = today_start_utc
        bucket_end_utc = None
    elif time_range == "yesterday":
        bucket_start_utc = yesterday_start_utc
        bucket_end_utc = today_start_utc
    else:
        # "all" or empty — show all jobs' buckets
        bucket_start_utc = None
        bucket_end_utc = None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE first_seen >= %s) AS this_hour,
                    COUNT(*) FILTER (WHERE first_seen >= %s) AS today,
                    COUNT(*) FILTER (WHERE first_seen >= %s AND first_seen < %s) AS yesterday
                FROM jobs
            """, (one_hour_ago, today_start_utc, yesterday_start_utc, today_start_utc))
            tab_row = cur.fetchone()

            if bucket_start_utc and bucket_end_utc:
                cur.execute(
                    "SELECT first_seen FROM jobs WHERE first_seen >= %s AND first_seen < %s",
                    (bucket_start_utc, bucket_end_utc),
                )
            elif bucket_start_utc:
                cur.execute(
                    "SELECT first_seen FROM jobs WHERE first_seen >= %s",
                    (bucket_start_utc,),
                )
            else:
                cur.execute("SELECT first_seen FROM jobs")
            fs_rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    buckets = {}
    for (fs_dt,) in fs_rows:
        if fs_dt is None:
            continue
        if fs_dt.tzinfo is None:
            fs_dt = fs_dt.replace(tzinfo=timezone.utc)
        fs_local = fs_dt.astimezone(user_tz)
        bs = _bucket_start(fs_local)
        bk = _bucket_key(bs)
        if bk not in buckets:
            bm = _bucket_minutes(bs)
            buckets[bk] = {
                "key": bk,
                "label": _bucket_label(bs, bm),
                "count": 0,
                "minutes": bm,
                "start_iso": bs.isoformat(),
            }
        buckets[bk]["count"] += 1

    bucket_list = sorted(buckets.values(), key=lambda b: b["start_iso"], reverse=True)

    return {
        "this_hour": tab_row[0],
        "today": tab_row[1],
        "yesterday": tab_row[2],
        "buckets": bucket_list,
    }


def get_search_terms() -> list[str]:
    """Return sorted list of distinct search_term values."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT search_term FROM jobs WHERE search_term != '' ORDER BY search_term")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_job(url: str) -> dict | None:
    """Get a single job by URL."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE url = %s", (url,))
            row = cur.fetchone()
            if not row:
                return None
            d = _row_to_dict(row, JOB_COLUMNS)
            d["_key"] = d["url"]
            return d
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_last_updated() -> str:
    """Return the most recent last_seen timestamp across all jobs."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(last_seen) FROM jobs")
            row = cur.fetchone()
            if row and row[0]:
                return row[0].isoformat()
            return ""
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Seen jobs (dedup tracking)
# ---------------------------------------------------------------------------

SEEN_TTL_HOURS = 48


def load_seen_jobs() -> dict[str, str]:
    """Load seen job URLs, pruning entries older than 48h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url, seen_at FROM seen_jobs WHERE seen_at > %s", (cutoff,))
            return {row[0]: row[1].isoformat() for row in cur.fetchall()}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def save_seen_job(url: str) -> None:
    """Mark a job URL as seen."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seen_jobs (url, seen_at) VALUES (%s, NOW())
                ON CONFLICT (url) DO UPDATE SET seen_at = NOW()
            """, (url,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def save_seen_jobs_bulk(seen: dict[str, str]) -> None:
    """Bulk upsert seen jobs dict."""
    if not seen:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_jobs WHERE seen_at < %s", (cutoff,))
            args = [(url, ts) for url, ts in seen.items()]
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO seen_jobs (url, seen_at) VALUES (%s, %s)
                ON CONFLICT (url) DO UPDATE SET seen_at = EXCLUDED.seen_at
            """, args)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def prune_seen_jobs() -> int:
    """Delete seen_jobs entries older than 48h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_jobs WHERE seen_at < %s", (cutoff,))
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
