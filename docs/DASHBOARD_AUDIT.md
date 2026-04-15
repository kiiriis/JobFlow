# Dashboard Logic Audit

**Date:** 2026-04-15
**Scope:** Web dashboard (`/linkedin` feed), scoring engine, LinkedIn store, scanner pipeline, CI workflow
**Store size at time of audit:** 1,061 jobs across 23,178 lines of JSON

---

## Table of Contents

1. [Security Vulnerabilities](#1-security-vulnerabilities)
2. [Logic Bugs / False Reasoning](#2-logic-bugs--false-reasoning)
3. [Data Integrity Issues](#3-data-integrity-issues)
4. [Performance Issues](#4-performance-issues)
5. [Suggestions for Better Results](#5-suggestions-for-better-results)
6. [Prioritized Fix List](#6-prioritized-fix-list)

---

## 1. Security Vulnerabilities

### A. XSS via Inline JavaScript Handlers — HIGH

**File:** `jobflow/web/templates/_partials/linkedin_row.html:55`

```html
onclick="dismissJob(this, '{{ job._key }}')"
```

`_key` is the raw job URL. Jinja2 HTML-escapes it, but the browser decodes HTML entities in attributes before executing JS. A crafted URL like `x');alert(1);//` becomes `&#39;` in HTML, but the browser decodes it back to `'` before running the JavaScript. Same issue on line 44 with `hx-patch="/api/linkedin/jobs/{{ job._key }}/status"`.

**Fix:** Use `data-key` attributes with an event listener, or use `{{ job._key|tojson }}` (which produces a properly JS-escaped quoted string) instead of wrapping in manual quotes.

---

### B. XSS via Client-Side HTML Injection — MEDIUM

**File:** `jobflow/web/templates/linkedin.html:552-557`

`renderBucketCards()` builds HTML via string concatenation from `c.key` and `c.label` without escaping, injected via `innerHTML`. The data originates server-side from `strftime` so it is low-risk in practice, but the pattern is dangerous if the data source changes.

**Fix:** Use `textContent` for data values, or escape before inserting into HTML strings.

---

### C. No CSRF Protection — MEDIUM

Every POST/PATCH endpoint is unprotected:
- `/api/scan/trigger`
- `/api/linkedin/jobs/.../status`
- `/api/tailor/generate`
- `/api/linkedin/refresh`

A cross-origin page can trigger scans, change job statuses, or start tailor sessions.

**Fix:** Add Flask-WTF or a custom CSRF token to all state-changing endpoints.

---

### D. No Authentication — MEDIUM

The Render deployment is publicly accessible. Anyone who can reach the server can:
- Trigger LinkedIn scans (rate-limit risk with LinkedIn)
- Modify job statuses
- Submit arbitrary JDs for tailoring (invokes Claude CLI)
- Trigger `git pull --rebase` on the server

**Fix:** Add basic auth, API key gating, or IP allowlisting at minimum.

---

### E. Path Traversal Weakness in `/api/file/` — LOW

**File:** `jobflow/web/__init__.py:254`

```python
if not str(requested).startswith(str(output_dir)):
```

Uses string prefix comparison instead of `Path.is_relative_to()`. If `output_dir` is `/data/output` and `/data/output_secret/` exists, files in it would pass the check.

**Fix:** Replace with `requested.is_relative_to(output_dir)` (Python 3.9+).

---

### F. No Containment Check on Tailor PDF Endpoints — LOW

**File:** `jobflow/web/__init__.py:452-471`

`api_tailor_pdf` and `api_tailor_download` serve `session["pdf_path"]` without verifying it is within an expected directory, unlike `/api/file/` which at least attempts a containment check.

**Fix:** Add the same directory containment check used by `/api/file/`.

---

## 2. Logic Bugs / False Reasoning

### A. `_rescore_entry` Diverges from `evaluate_job` — CRITICAL

**Files:** `linkedin_store.py:68-175` vs `filter.py:436-578`

`_rescore_entry` duplicates the scoring logic from `evaluate_job` but with critical differences:

| Check | `evaluate_job` | `_rescore_entry` |
|---|---|---|
| Text analyzed | Full description (3-5K chars) | `description_preview` (2K chars max) |
| Non-US location | Hard-reject | **Completely skipped** |
| Location score | +10 US / -10 non-US | **Hardcoded +10** (line 145) |
| Competition age | Actual hours since posting | **Hardcoded 24** (line 147) |

**Impact:**
- Every job in the store gets a free +10 location bonus regardless of actual location.
- Jobs rejected for non-US locations during initial scanning could pass rescoring.
- Keyword matches and sponsorship phrases past the 2K char mark are missed.
- Competition scores are inaccurate for every job.

**Fix:** Have `_rescore_entry` construct a `JobPosting` from the store dict and call `evaluate_job()` directly, eliminating the duplicated logic entirely.

---

### B. `recommended` Is Always False Without OpenAI Key — HIGH

**File:** `linkedin_store.py:173`

```python
entry["recommended"] = False  # only AI can recommend
```

If `OPENAI_API_KEY` is not set, the AI scoring step in `cli.py:322` is skipped entirely. The "Recommended" filter chip on the dashboard will always show 0 jobs. The entire recommendation feature is dead unless the OpenAI secret is configured and functioning.

**Fix:** Add an algorithmic fallback:
```python
if ai_score is not None:
    entry["recommended"] = int(ai_score) >= 7
else:
    entry["recommended"] = score_pct >= 70  # algorithmic fallback
```

---

### C. `scan_results.json` Accumulation Prevents Job Pruning — HIGH

**Files:** `cli.py:296-308`, `scan-jobs.yml:47-68`

The pipeline works as follows:
1. The CLI scan command **appends** new jobs to `scan_results.json` and caps at 500.
2. The CI merge step processes **all 500 entries** every run.
3. `merge_scan_results` updates `last_seen = now` for every existing job it encounters.
4. `prune_old_jobs` keeps jobs where `last_seen >= cutoff` (7 days ago).

Since all 500 jobs in `scan_results.json` get their `last_seen` refreshed every CI run, they **never expire**. The 7-day retention window is effectively infinite for any job that ever entered the top 500 by score.

**Fix:** Either truncate `scan_results.json` after merging into the store, or only merge newly-added entries (tracked by a timestamp or index watermark).

---

### D. Pre-dedup Loses Multi-Location Postings — MEDIUM

**File:** `linkedin_store.py:196-207`

Pre-dedup groups scan results by `company|title` (lowercased). If Google has "Software Engineer" in NYC and "Software Engineer" in Seattle, only one survives. This is especially problematic for large companies that post identical titles across many locations.

**Fix:** Include location in the dedup combo key: `f"{co}|{title}|{loc}"`.

---

### E. Senior Salary Threshold Is Too Low for Big Tech New Grad — MEDIUM

**File:** `filter.py:132`

```python
SENIOR_SALARY_PATTERN = r"\$1[3-9]\d[,.]?\d{3}|\$[2-9]\d\d[,.]?\d{3}"
```

This matches salaries of $130K+. However:
- Google L3 new grad base: ~$137K
- Meta E3 new grad base: ~$135K
- Apple ICT2 new grad base: ~$140K

A JD stating "$140,000 - $170,000" with no explicit "new grad" phrasing is **hard-rejected**. This systematically filters out the highest-quality new grad roles at top companies.

**Fix:** Raise the threshold to $160K, or make it configurable, or add Big Tech companies from `BIG_TECH` as an exception to the salary check.

---

### F. Sort Tiebreaker Flips with Sort Direction — LOW

**File:** `linkedin_store.py:434-436`

```python
reverse = sort_dir == "desc"
rest.sort(key=sort_key, reverse=reverse)
```

The `reverse` flag applies to the entire sort tuple `(primary, ai_score, algo_score)`. When sorting by title ascending, jobs with **lower** AI scores appear first within each title group. The tiebreaker should always be descending regardless of primary sort direction.

**Fix:** Negate the tiebreaker values so they sort descending regardless:
```python
return (primary, -ai, -algo)  # tiebreakers always favor higher scores
```

---

### G. Time Filtering Uses `first_seen` Which May Be Stale — LOW

**File:** `linkedin_store.py:387`

Time range filtering (This Hour, Today, Yesterday) uses `first_seen`, which is set to `date_posted` when available. LinkedIn's `date_posted` is often a date (not datetime), parsed as midnight UTC. A job posted yesterday but first scanned today would appear under "Yesterday" rather than "Today", confusing users who expect to see recently discovered jobs.

---

## 3. Data Integrity Issues

### A. Race Conditions on `linkedin_jobs.json` — HIGH

**File:** `jobflow/web/__init__.py`

Three threads access the store file concurrently with no locking:

| Thread | Operation | Lines |
|---|---|---|
| Auto-pull loop | `_do_linkedin_merge()` → read + write | 498-510 |
| Scan background | `_run_scan()` → `save_store()` | 680-688 |
| Request handler | `api_linkedin_status()` → `load_store()` + `save_store()` | 591-604 |

Two threads calling `save_store` simultaneously causes one to overwrite the other's changes. A status update made between another thread's `load_store` and `save_store` is silently lost.

**Fix:** Add a `threading.Lock` around all store read-modify-write operations.

---

### B. No Atomic Writes — MEDIUM

**File:** `linkedin_store.py:52-55`

```python
def save_store(path: Path, store: dict) -> None:
    path.write_text(json.dumps(store, indent=2))
```

A crash or power loss mid-write leaves a corrupted or partial JSON file. `load_store` has a `JSONDecodeError` catch that returns an empty store, so a crash during write effectively deletes all job data.

**Fix:** Write to a temporary file in the same directory, then use `os.rename()` (atomic on POSIX) to replace the original.

---

### C. Double Rescore on Every Merge — LOW

**Files:** `jobflow/web/__init__.py:487-492`, `linkedin_store.py:256`

During every merge cycle:
1. `merge_scan_results` calls `_rescore_entry()` on each job.
2. Then the `backfill_job` loop calls `_rescore_entry()` again on every job.

Every job is scored twice per merge for no benefit.

**Fix:** Remove the `backfill_job` loop since `merge_scan_results` already rescores all jobs. Or have `backfill_job` skip rescoring if the job was already scored in this cycle.

---

## 4. Performance Issues

### A. Store Loaded from Disk on Every Request

**File:** `jobflow/web/__init__.py:549`

Every `/api/linkedin/jobs` call (triggered by any filter change, sort change, or search keystroke) reads the full 23K-line JSON file from disk and parses it. With active dashboard use, this can be dozens of disk reads per minute.

**Fix:** Cache the parsed store in memory with a file mtime check. Invalidate when `save_store` is called.

---

### B. Triple Iteration Over All Jobs per Request

Each `/api/linkedin/jobs` request calls three functions that each iterate all 1,061 jobs independently:
- `get_filtered_jobs()` — builds the filtered result list
- `get_filtered_counts()` — counts jobs per status/level
- `get_time_counts()` — computes time bucket counts

**Fix:** Combine into a single pass that returns all three results simultaneously.

---

### C. No Pagination

All matching jobs are rendered into HTML at once and sent in a single response. The CSS `max-height` with overflow handles the visual presentation, but the full HTML payload is still generated, sent, and parsed by the browser.

**Fix:** Add server-side pagination (e.g., 50 jobs per page) with a "load more" HTMX trigger.

---

## 5. Suggestions for Better Results

### A. Unify Scoring Logic

The highest-impact single fix. Have `_rescore_entry` construct a `JobPosting` from the store dict and delegate to `evaluate_job()`. This eliminates the hardcoded location score, the missing non-US check, the truncated description, and any future drift between the two code paths.

### B. Add More LinkedIn Search Terms

Currently only 5 search terms are used:
```python
LINKEDIN_SEARCH_TERMS = [
    "Software Engineer New Grad",
    "Entry Level Software Engineer",
    "Software Engineer 1",
    "New Grad Machine Learning Engineer",
    "Entry Level AI Engineer",
]
```

Consider adding:
- "Junior Software Engineer"
- "Associate Software Engineer"
- "SDE 1" / "SDE I"
- "Platform Engineer New Grad"
- "Backend Engineer Entry Level"
- "New Grad Data Engineer"
- "Entry Level Cloud Engineer"

### C. Add Description Search to Dashboard

The text search (`linkedin_store.py:409-411`) only covers company, title, and location. Users cannot search for tech stack keywords like "pytorch", "kubernetes", or "fastapi". Adding `description_preview` to the searchable text enables stack-based filtering.

### D. Add Location and Experience Filters

The dashboard computes location and experience data for every job but provides no UI filters for them. Adding:
- A location dropdown (Remote / specific metro areas)
- An experience range filter (0-1 yr, 1-2 yrs, 2-3 yrs)

would let users narrow results more precisely.

### E. Expand the Company Blocklist

Only 15 companies are currently blocked. Known aggregators to consider adding:
- Revature, Infosys (contract shops posting entry-level roles)
- Kforce, TEKsystems, Insight Global (staffing agencies)
- Any company that consistently appears with "no sponsorship" in the JD

### F. Normalize LinkedIn URLs for Dedup

LinkedIn job URLs can vary with tracking parameters (`?trk=...`, `?refId=...`). Stripping query parameters before using the URL as a dedup key would reduce duplicate entries.

---

## 6. Prioritized Fix List

Ranked by impact and effort:

| Priority | Issue | Severity | Effort |
|---|---|---|---|
| 1 | Unify `_rescore_entry` with `evaluate_job` | Critical | Medium |
| 2 | Fix `scan_results.json` accumulation / never-expire | High | Low |
| 3 | Fix XSS in dismiss button onclick handler | High | Low |
| 4 | Add algorithmic recommendation fallback | High | Low |
| 5 | Add `threading.Lock` for store access | High | Low |
| 6 | Atomic writes for `linkedin_jobs.json` | Medium | Low |
| 7 | Raise senior salary threshold ($130K -> $160K) | Medium | Low |
| 8 | Fix pre-dedup losing multi-location postings | Medium | Low |
| 9 | Add CSRF protection | Medium | Medium |
| 10 | Cache store in memory | Medium | Medium |
| 11 | Combine triple iteration into single pass | Low | Medium |
| 12 | Add more LinkedIn search terms | Low | Low |
| 13 | Add description search to dashboard | Low | Low |
| 14 | Add pagination | Low | Medium |
| 15 | Add basic authentication | Medium | Medium |
