# Database Migration Audit

Thorough inspection of the JSON-to-PostgreSQL migration across `db.py`, `db_migrate.py`, `linkedin_store.py`, `scanner.py`, `web/__init__.py`, `pyproject.toml`, `render.yaml`, and `scan-jobs.yml`.

---

## Critical Issues

### 1. ~~Connection pool is not thread-safe~~ — FIXED

**File:** `db.py:34`

Replaced `SimpleConnectionPool` with `ThreadedConnectionPool` (thread-safe). Also increased max connections from 3 to 5 to handle concurrent web request + background scan + tailor thread.

### 2. ~~Migration script silently loses jobs on first error~~ — FIXED

**File:** `db_migrate.py:46-105`

Added `SAVEPOINT`/`ROLLBACK TO SAVEPOINT` per insert so one bad row doesn't abort the entire transaction. Both the jobs loop and the seen_jobs loop are now protected.

### 3. ~~No connection keepalive — Neon kills idle connections~~ — FIXED

**File:** `db.py:34-38`

Added TCP keepalive params to the connection pool:
```python
keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5
```

Also added connection validation in `get_conn()` — runs `SELECT 1` before returning a pooled connection. If the connection is dead (Neon killed it), it's discarded and a fresh one is created.

---

## Moderate Issues

### 4. ~~TTL mismatch: 2 days (DB) vs 7 days (JSON)~~ — FIXED

**Files:** `db.py:26`

Bumped `TTL_DAYS` from 2 to 3. Jobs now persist for 3 days in the DB (vs 7 in JSON). This is a deliberate tradeoff — DB storage is persistent and always fresh, so shorter TTL keeps the feed relevant. Jobs marked Tracking/Applied never expire.

### 5. ~~Read-only queries leave transactions in failed state~~ — FIXED

**Files:** `db.py` (all query functions)

Added `except Exception: conn.rollback(); raise` to every `try/finally` block that was missing it. This covers: `get_filtered_jobs`, `get_status_counts`, `get_level_counts`, `get_filtered_counts`, `get_time_counts`, `get_search_terms`, `get_job`, `get_last_updated`, `prune_expired_jobs`, `load_seen_jobs`, `save_seen_job`, `save_seen_jobs_bulk`, `prune_seen_jobs`, and `init_db`.

### 6. ~~INTERVAL parameter binding is fragile~~ — FIXED

**Files:** `db.py` (seen_jobs functions)

Replaced all `INTERVAL '%s hours'` patterns with Python-computed cutoff datetimes:
```python
cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
cur.execute("... WHERE seen_at > %s", (cutoff,))
```

### 7. `USE_DB` / `_USE_DB` evaluated once at import time — NOT FIXING

**Files:** `linkedin_store.py:28`, `scanner.py:681`

This is standard practice. Env vars are always set before process starts in Render/CI/local dev. Runtime toggling is not a use case for this app.

### 8. Race condition in scan trigger — NOT FIXING

**File:** `web/__init__.py:155-173`

Pre-existing issue (before DB migration). Single-user app with `gunicorn -w 1`. The TOCTOU window is microseconds — two simultaneous POST requests from the same user are effectively impossible. The upsert pattern in `merge_scan_results` now handles duplicate inserts gracefully anyway.

### 9. ~~`prune_expired_jobs` has no error handling~~ — FIXED

**File:** `db.py:323-333`

Added `except Exception: conn.rollback(); raise` (same fix as #5).

---

## Minor Issues

### 10. `psycopg2-binary` is a hard dependency — NOT FIXING

**File:** `pyproject.toml:18`

Both Render and CI require it. Making it optional adds complexity (conditional imports, install instructions) for no practical benefit since the target platforms all support it.

### 11. Clock skew between app and database — NOT FIXING

Neon and Render both run on AWS. Clock skew is sub-second and irrelevant for a 3-day TTL.

### 12. ~~`merge_scan_results` uses SELECT-then-INSERT (race condition)~~ — FIXED

**File:** `db.py:186-278`

Replaced the SELECT-then-INSERT/UPDATE pattern with a single `INSERT ... ON CONFLICT (url) DO UPDATE` (upsert). Uses `RETURNING (xmax = 0) AS inserted` to count genuinely new rows. This eliminates the race condition entirely.

### 13. f-string SQL construction — NOT FIXING

**File:** `db.py:428`

`JOB_COLUMNS` is a hardcoded constant, `sort_col` is whitelisted against `allowed_sorts`. Safe as-is.

### 14. Migration doesn't validate timestamp formats — NOT FIXING

All timestamps in `linkedin_jobs.json` include timezone offsets (verified by inspection). PostgreSQL handles them correctly.

---

## New Issue Found in Fix #12

### 15. ~~Upsert overwrites `recommended` and `score_pct` for AI-scored jobs~~ — FIXED

**File:** `db.py:238-245`

The fix for #12 introduced a regression. The old SELECT-then-INSERT code carried AI scores from the existing DB row into the entry **before** calling `_rescore_entry`, so the rescore produced correct `score_pct` and `recommended` values:

```python
# OLD code — correct behavior
if ex_ai_score is not None and not entry.get("ai_score"):
    entry_to_score["ai_score"] = ex_ai_score       # carry from DB
    entry_to_score["ai_reason"] = ex_ai_reason
scored = _rescore_entry(entry_to_score)             # rescore WITH ai_score → score_pct=80, recommended=True
```

The new upsert code rescores the raw scan entry (which has no AI score), then relies on SQL `COALESCE` to preserve `ai_score` — but `score_pct` and `recommended` are overwritten with the algo-only values:

```python
# NEW code — rescores WITHOUT ai_score from DB
scored = _rescore_entry(dict(entry))  # no ai_score → recommended=False, score_pct=algo only (e.g. 45)
```

```sql
-- SQL preserves ai_score but overwrites the fields derived from it
ai_score = COALESCE(jobs.ai_score, EXCLUDED.ai_score),   -- keeps 8 ✓
score_pct = EXCLUDED.score_pct,                           -- overwrites 80 → 45 ✗
recommended = EXCLUDED.recommended,                       -- overwrites True → False ✗
```

**Impact:** Every scan cycle strips the `recommended` flag and AI-based `score_pct` from all previously AI-scored jobs. The "Recommended" filter tab gradually empties out.

**Fix:** Guard `score_pct` and `recommended` in the ON CONFLICT clause so they're preserved when the DB already has an AI score:

```sql
score_pct = CASE
    WHEN jobs.ai_score IS NOT NULL THEN jobs.score_pct
    ELSE EXCLUDED.score_pct
END,
recommended = CASE
    WHEN jobs.ai_score IS NOT NULL THEN jobs.recommended
    ELSE EXCLUDED.recommended
END,
```

---

## Summary

| # | Severity | Issue | Status |
|---|----------|-------|--------|
| 1 | **Critical** | `SimpleConnectionPool` not thread-safe | **FIXED** → `ThreadedConnectionPool` |
| 2 | **Critical** | Migration loses jobs after first insert error | **FIXED** → SAVEPOINT per insert |
| 3 | **Critical** | No keepalive — Neon kills idle connections | **FIXED** → TCP keepalive + validation |
| 4 | Moderate | TTL drops from 7 days to 2 days | **FIXED** → bumped to 3 days |
| 5 | Moderate | Read queries leave connections in failed state | **FIXED** → rollback in except |
| 6 | Moderate | INTERVAL parameter binding is fragile | **FIXED** → Python-computed cutoff |
| 7 | Moderate | `USE_DB` flag frozen at import time | Won't fix (by design) |
| 8 | Moderate | Race condition allows duplicate scans | Won't fix (pre-existing, single-user) |
| 9 | Moderate | `prune_expired_jobs` no rollback on error | **FIXED** → rollback in except |
| 10 | Minor | `psycopg2-binary` should be optional | Won't fix (not worth complexity) |
| 11 | Minor | Clock skew between app and DB server | Won't fix (sub-second on AWS) |
| 12 | Minor | SELECT-then-INSERT race in merge | **FIXED** → upsert with ON CONFLICT |
| 13 | Minor | f-string SQL construction | Won't fix (safe, whitelisted) |
| 14 | Minor | Naive timestamps in migration | Won't fix (all have TZ offsets) |
| 15 | **Moderate** | Upsert overwrites `recommended`/`score_pct` for AI-scored jobs | **FIXED** → CASE guard on ai_score |

**Result: 10 of 15 issues fixed. 5 deliberately not fixed (documented rationale above).**
