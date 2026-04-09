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
- `/` — Redirects to /linkedin
- `/linkedin` — LinkedIn job feed (main dashboard)
- `/boards` — Job Boards placeholder
- `/scan` — Scanner page
- `/tailor` — Resume tailor page

## Scoring
Multi-signal scoring (0-100%) for Python/ML/Backend stack. See docs/SCORING.md.

## Filter Criteria
- New grad / entry-level / SDE 1 roles only
- USA-based positions
- Must NOT deny visa sponsorship
- OPT/F1 friendly
- Software engineering roles only

## Deployment
Render.com free tier + GitHub Actions hourly cron. See docs/DEPLOYMENT.md.
