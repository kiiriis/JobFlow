# JobFlow

Automated job scanner and resume tailoring system for new grad / entry-level SWE positions.

## Project Structure

```
JobFlow/
├── config/
│   ├── config.yaml          # Local config (resume paths, output dirs)
│   ├── config.ci.yaml       # CI config (outputs to data/ci/)
│   └── job_boards.json      # 82 company API endpoints
├── data/ci/                 # CI output (git-tracked, pushed by GitHub Actions)
│   ├── scan_results.json    # Raw scan output
│   ├── linkedin_jobs.json   # Persistent store with user statuses
│   └── seen_jobs.json       # Dedup tracking
├── jobflow/
│   ├── cli.py               # Typer CLI commands
│   ├── config.py            # YAML config loader
│   ├── models.py            # JobPosting, FilterResult dataclasses
│   ├── filter.py            # Multi-signal scoring engine
│   ├── scanner.py           # Job scanners (Lever, Greenhouse, Ashby, LinkedIn, GitHub)
│   ├── linkedin_store.py    # LinkedIn job persistence + filtering + dedup
│   ├── tracker.py           # CSV-based application tracking
│   ├── tailor.py            # Resume merging + prompt building
│   ├── latex.py             # pdflatex compilation
│   ├── scraper.py           # Job description parser
│   └── web/
│       ├── __init__.py      # Flask app factory + all routes
│       ├── static/style.css # CSS
│       └── templates/       # Jinja2 templates
├── docs/                    # Comprehensive documentation (9 files)
├── .github/workflows/
│   └── scan-jobs.yml        # Hourly LinkedIn scan + Render ping
├── wsgi.py                  # Gunicorn WSGI entry point
├── Procfile                 # Render process definition
└── render.yaml              # Render Blueprint
```

## Commands
- `jobflow scan` — Scan all sources
- `jobflow scan --hours 1 --new --platform linkedin` — Hourly scan (used by CI)
- `jobflow apply <url> --paste -t "Title" -c "Company" -l "Location"` — Process a job
- `jobflow save --dir <path>` — Merge tailored sections + compile PDF
- `jobflow process <#>` — Process a job from scan results
- `jobflow list` — View tracked applications
- `jobflow web` — Launch web dashboard
- `jobflow init` — First-time setup

## Web Routes
The web app is a single page — the job feed. Everything else lives in the CLI.
- `/` — Redirects to /linkedin
- `/linkedin` — The job feed (filtering, sorting, time buckets, bulk actions)
- `/api/linkedin/*` — Feed data: jobs table fragment, meta counts, delete, refresh
- `/api/scan/*` — Background scan (powers the feed's "Scan Now" button)
- `/api/aiscore/*` — Local AI scoring runner: the feed's "AI Score" button runs
  `scripts/ai_score_local.py` as a subprocess (engine claude/codex, hours/limit/rescore)
  and streams live progress. Uses the signed-in CLI — no API key.

Removed from the web UI in June 2026: application tracking (CSV tracker, status
dropdowns, /api/stats), the /boards page, the /scan page, and the /tailor page.
`tracker.py` and `tailor.py` remain for CLI use only.

## Scoring
Multi-signal scoring (0-100%) for Python/ML/Backend stack. See docs/SCORING.md.
When a job has no AI score yet, `filter.algo_recommended()` flags high-scoring
entry-level jobs as Recommended (score_pct >= 65 and level New Grad/Entry).

## Filter Criteria
- New grad / entry-level / SDE 1 roles only
- USA-based positions
- Must NOT deny visa sponsorship
- OPT/F1 friendly
- Software engineering roles only

## Deployment
Render.com free tier + GitHub Actions hourly cron. See docs/DEPLOYMENT.md.
