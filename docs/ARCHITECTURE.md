# JobFlow Architecture

## System Overview

JobFlow is a job scanning, scoring, and resume tailoring system for new grad / entry-level software engineering roles. It runs as a CLI tool + Flask web dashboard, with GitHub Actions for automated hourly scanning.

```
GitHub Actions (hourly cron)
    |
    v
jobflow scan --platform linkedin --new --hours 1
    |
    v
data/ci/scan_results.json  ──push──>  GitHub repo
                                           |
                                           v
                                    Render redeploy
                                           |
                                           v
Flask Web Dashboard  <──merge──  data/ci/linkedin_jobs.json
    |
    v
User browses and filters jobs (tailoring is CLI-only)
```

## Directory Structure

```
JobFlow/
├── config/
│   ├── config.yaml          # Local config (resume paths, output dirs)
│   ├── config.ci.yaml       # CI config (outputs to data/ci/)
│   └── job_boards.json      # 82 companies across Lever/Greenhouse/Ashby
├── data/
│   ├── ci/                  # CI output (git-tracked, pushed by Actions)
│   │   ├── scan_results.json    # Raw scan output
│   │   ├── linkedin_jobs.json   # Persistent store with user statuses
│   │   └── seen_jobs.json       # Dedup tracking
│   └── output/              # Local output (gitignored)
├── jobflow/                 # Python package
│   ├── cli.py               # Typer CLI commands
│   ├── config.py            # YAML config loader
│   ├── models.py            # JobPosting, FilterResult dataclasses
│   ├── filter.py            # Multi-signal scoring engine
│   ├── scanner.py           # Job board scanners (Lever, Greenhouse, Ashby, LinkedIn, GitHub)
│   ├── linkedin_store.py    # LinkedIn job persistence + filtering + dedup
│   ├── tracker.py           # CSV-based application tracking
│   ├── tailor.py            # Resume merging + prompt building
│   ├── latex.py             # pdflatex compilation
│   ├── scraper.py           # Job description parser
│   └── web/
│       ├── __init__.py      # Flask app factory + all routes
│       ├── static/style.css # Custom CSS (Atriveo-inspired dark theme)
│       └── templates/       # Jinja2 templates
├── .github/workflows/
│   └── scan-jobs.yml        # Hourly LinkedIn scan + Render ping
├── wsgi.py                  # Gunicorn WSGI entry point
├── Procfile                 # Render process definition
└── render.yaml              # Render Blueprint
```

## Data Flow

### Scanning Pipeline
1. GitHub Actions cron triggers every hour
2. `jobflow scan --platform linkedin --new --hours 1` runs
3. jobflow.linkedin_scraper scrapes LinkedIn guest API with 8 search terms (up to 200 results each; descriptions fetched only for new jobs)
4. Each job passes through `evaluate_job()` — multi-signal scoring
5. Jobs scoring >= 30% saved to `data/ci/scan_results.json`
6. Deduplication against `data/ci/seen_jobs.json`
7. Results committed and pushed to GitHub

### Web Dashboard Data Flow
1. On startup, Flask merges `scan_results.json` into `linkedin_jobs.json`
2. Deduplication by company+title (keeps best entry per combo)
3. All jobs re-scored with current filter logic
4. Untracked jobs past their 3-day `expires_at` TTL are pruned; Tracking/Applied jobs are kept
5. Background thread (local only) does `git pull` every hour for fresh data
6. On Render: data updates via auto-redeploy on GitHub push

### Resume Tailoring Flow (CLI-only)
1. `jobflow apply <url> --paste` scrapes/structures the JD, scores it, and writes a tailoring prompt
2. User feeds the prompt to Claude to get tailored LaTeX sections
3. `jobflow save --dir <path>` merges the base-resume preamble + education with the tailored sections and compiles a PDF with pdflatex

The web dashboard no longer tailors resumes — tailoring lives entirely in the CLI.

## Key Design Decisions

- **Dual-backend storage** — PostgreSQL/Neon when `DATABASE_URL` is set, else JSON files (simple, git-friendly, works on Render free tier)
- **Server-side rendering with HTMX** — no SPA complexity, partial HTML swaps
- **Multi-signal scoring** — adapted from Atriveo's approach but tuned for Python/ML/Backend stack
- **Deduplication by company+title** — same role posted in multiple cities collapsed to one
- **Timezone-aware filtering** — client sends `tz` offset, server computes local today/yesterday
- **Render deployment** — auto-deploys on push, kept alive by hourly GitHub Actions ping
