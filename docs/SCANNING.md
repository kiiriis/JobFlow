# Job Scanning System

## Overview

JobFlow scans multiple job board platforms and aggregates results into a unified feed. The scanner runs both locally (via CLI) and in CI (via GitHub Actions hourly cron).

## Platforms

### LinkedIn (via python-jobspy)

**File**: `jobflow/scanner.py` — `scan_linkedin_jobspy()`

Uses the [python-jobspy](https://github.com/Bunsly/JobSpy) library to scrape LinkedIn.

**Search Terms** (6 queries, 200 results each):
1. "new grad software engineer 2025 2026"
2. "entry level software engineer"
3. "junior software engineer"
4. "new grad machine learning engineer"
5. "entry level AI engineer"
6. "SDE I new grad"

**Configuration:**
- Location: "United States"
- `linkedin_fetch_description`: True (fetches full JD)
- Deduplication by URL across search terms
- Random 2-4s delay between terms (rate limiting)

**Known Limitation:** LinkedIn doesn't expose `date_posted` — jobspy returns `None`. Jobs are timestamped with the scan time (when discovered).

### Lever API

**File**: `jobflow/scanner.py` — `scan_lever()`

Fetches from Lever's public JSON API (`/postings?mode=json`).

**Companies**: 11 (configured in `config/job_boards.json`)
- Example: Anduril, Cloudflare, Notion, Plaid, etc.

### Greenhouse API

**File**: `jobflow/scanner.py` — `scan_greenhouse()`

Fetches from Greenhouse's public JSON API (`/boards/{id}/jobs`).

**Companies**: 40 (configured in `config/job_boards.json`)
- Example: Stripe, Airbnb, Coinbase, DoorDash, etc.

### Ashby API

**File**: `jobflow/scanner.py` — `scan_ashby()`

Fetches from Ashby's public JSON API (`/posting-api/job-board/{id}`).

**Companies**: 31 (configured in `config/job_boards.json`)
- Example: Ramp, Figma, Linear, etc.

### GitHub Repos

**File**: `jobflow/scanner.py` — `scan_github_repos()`

Parses markdown tables from new-grad aggregator repos:
- [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
- [Jobright-AI/2025-New-Grad-Intern](https://github.com/Jobright-AI/2025-New-Grad-Intern)

## Pre-filters (Title-level)

Before full scoring, jobs are pre-filtered by title:

**Must match** (SWE_ROLE_KEYWORDS): software, engineer, developer, swe, sde, backend, frontend, full stack, machine learning, data scientist, ml engineer, ai engineer, applied scientist

**Role exclusion** (in evaluate_job): senior, staff, principal, lead, manager, director (only penalized, not hard-rejected, unless 3+ signals with 0 entry signals)

## Scan Output

Results saved to `scan_results.json`:

```json
[
  {
    "index": 1,
    "company": "Stripe",
    "title": "Software Engineer, New Grad",
    "location": "San Francisco, CA",
    "url": "https://...",
    "score": 45,
    "score_pct": 35,
    "level": "New Grad",
    "min_exp": 0,
    "max_exp": 2,
    "competition": 5,
    "variant": "se",
    "reason": "Stack +24; Synergy +10; ...",
    "description_preview": "...",
    "date_posted": ""
  }
]
```

Capped at 500 entries, sorted by score descending. Merges with existing results (dedup by URL).

## GitHub Actions Workflow

**File**: `.github/workflows/scan-jobs.yml`

```
Schedule: Every hour at :00 (cron: '0 * * * *')
Also: Manual trigger from Actions tab

Steps:
1. Checkout repo
2. Setup Python 3.12
3. pip install -e .
4. JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin --new --save --hours 1
5. git add data/ci/ && git commit && git push
6. curl Render URL (keep-alive)
```

The `--hours 1` flag means only jobs from the last hour. The `--new` flag deduplicates against `seen_jobs.json`. This ensures each hourly scan only captures genuinely new postings.

## Deduplication

Three levels:
1. **Within a scan**: By URL (across search terms)
2. **Across scans**: Via `seen_jobs.json` (with `--new` flag)
3. **In the store**: By company+title (same role in multiple cities collapsed)

## Rate Limiting

- LinkedIn: 2-4s random delay between search terms
- All platforms: 3-retry exponential backoff on HTTP 429
- Fetch timeout: 15s per request
