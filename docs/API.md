# API Reference

All routes are defined in `jobflow/web/__init__.py`. The dashboard is a single
page (the LinkedIn job feed); everything else is a JSON/HTML-fragment API that
the feed's JavaScript calls.

Storage is dual-backend: PostgreSQL (Neon) when `DATABASE_URL` is set, otherwise
the JSON store at `data/ci/linkedin_jobs.json`. Every endpoint tries the DB first
and transparently falls back to JSON.

## Page Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Redirects to `/linkedin` |
| `/health` | GET | JSON health check (`{"status": "ok"}`) for uptime monitoring |
| `/linkedin` | GET | The job feed (the only page) |

## Feed API

### GET /api/linkedin/jobs

Filtered, sorted job rows. Returns the `_partials/linkedin_tbody.html` fragment;
count metadata rides along in response headers so the page can update chips and
tiles without a second request.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | string | `""` | "Tracking", "Applied", "Not Interested", or "Recommended" (virtual, AI-score based) |
| `level` | string | `""` | "New Grad", "Entry", "Mid", "Unknown" |
| `q` | string | `""` | Text search (company, title, location) |
| `search_term` | string | `""` | Filter by the LinkedIn search term that found the job |
| `time` | string | `""` | "hour", "today", "yesterday", or "" (all) |
| `bucket` | string | `""` | Exact 30-min bucket key, e.g. `2026-06-16_13:30` |
| `sort` | string | `"first_seen"` | "first_seen", "last_seen", "score_pct", "score", "ai_score", "competition", "min_exp", "level", "company", "title" |
| `dir` | string | `"desc"` | "asc" or "desc" |
| `tz` | int | `0` | Browser timezone offset in minutes from UTC (`new Date().getTimezoneOffset()`) |
| `limit` | int | `250` | Max rows (clamped to 50–500) |
| `meta` | string | `"1"` | `"1"` to include time-bucket headers |

**Response Headers:**

| Header | Content |
|--------|---------|
| `X-Counts` | `{"All", "Tracking", "Applied", "Not Interested", "Recommended"}` |
| `X-Level-Counts` | `{"All", "New Grad", "Entry", "Mid", "Unknown"}` |
| `X-Time-Counts` | `{"this_hour", "today", "yesterday"}` (only when `meta=1`) |
| `X-Buckets` | JSON list of `{key, label, count, minutes, start_iso}` (only when `meta=1`) |
| `X-Total` | Total jobs matching the filters |
| `X-Displayed` | Rows actually returned (after `limit`) |

### GET /api/linkedin/meta

The same count metadata as JSON (no table rows). Accepts the same query params.

### DELETE /api/linkedin/jobs/&lt;key&gt;

Delete one job (`key` is the canonical URL). The URL is also recorded as
dismissed so it never reappears on a later scan. Returns `204 No Content`.

### POST /api/linkedin/jobs/bulk-delete

Delete many jobs. Body: `{"keys": [<url>, ...]}`. Returns
`{"requested": N, "deleted": M}`.

### POST /api/linkedin/refresh

Refresh the feed. On the DB backend, prunes expired jobs. On the JSON backend,
runs `git pull --rebase` then re-merges scan results. Returns `{"ok": true}`.

## Scan API (the feed's "Scan Now" button)

### POST /api/scan/trigger

Start a background scan. Body (form or JSON): `hours` (max age, 0 = no limit),
`new_only` (`"true"`/`"on"`/`"1"` to dedup against seen jobs). Returns
`{"running": true}`, or `409` if a scan is already running.

### GET /api/scan/status

Poll scan progress: `{"running", "error", "total", "relevant", "skipped"}`.
`relevant` is jobs not flagged by a hard-reject rule; `skipped` is flagged jobs
(still saved — the AI scorer is the real quality gate).

## AI Scoring API (the feed's "AI Score" button)

Runs `scripts/ai_score_local.py` as a subprocess against the signed-in
Claude/Codex CLI (no API key). One run at a time.

### POST /api/aiscore/trigger

Body (form or JSON): `engine` ("claude" | "codex"), `hours` (window, 0 = all),
`limit` (max jobs, 0 = no limit), `rescore` (re-score already-scored rows).
Returns a JSON status snapshot, or `400`/`409` on bad input / already running.

### GET /api/aiscore/status

JSON snapshot: `running`, `engine`, `total`, `batch`, `batches`, `scored`,
`failed`, `log` (tail of output lines), `ok`, `error`, timestamps.

### POST /api/aiscore/cancel

Request cancellation and kill the subprocess. Returns the latest snapshot.
