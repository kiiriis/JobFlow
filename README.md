# JobFlow

Automated job scanner + resume tailoring system for new grad / entry-level software engineering roles. Scans LinkedIn hourly via GitHub Actions, scores jobs against your tech stack, and serves a real-time dashboard.

**Live at:** [jobflow.onrender.com](https://jobflow.onrender.com) (or your Render URL)

## How It Works

```
GitHub Actions (hourly cron)
    |
    v
Scan LinkedIn (6 search terms x 200 results)
    |
    v
Score & filter (multi-signal engine: keywords, synergy, level, experience)
    |
    v
Commit to GitHub --> Render auto-deploys --> Live dashboard
```

The dashboard shows jobs ranked by relevance to your profile, with filtering by time, level, and status.

## Features

- **Hourly LinkedIn scanning** via GitHub Actions + python-jobspy
- **Multi-signal scoring** — keyword matching, synergy combos, level detection, experience fit, recency, H1B bonus
- **Real-time web dashboard** — Atriveo-inspired dark theme with sidebar stats, hourly cards, sortable table
- **Resume tailoring** — Claude AI rewrites your LaTeX resume per job description
- **Application tracking** — CSV-based status management
- **Auto-deployment** — Render free tier with GitHub Actions keep-alive

## Quick Start

```bash
git clone https://github.com/kiiriis/JobFlow.git
cd JobFlow
pip install -e .

# Run locally
jobflow web

# Or scan jobs from CLI
jobflow scan --platform linkedin --hours 4
```

**Requirements:** Python 3.11+ | Optional: pdflatex (for resume PDFs), Claude CLI (for tailoring)

## Scanning

```bash
# Scan all platforms (Lever + Greenhouse + Ashby + LinkedIn + GitHub)
jobflow scan

# LinkedIn only, last hour, only new jobs
jobflow scan --platform linkedin --new --hours 1

# Specific platform
jobflow scan --platform greenhouse --hours 24
```

**Sources:**
- **LinkedIn** — 6 search terms via python-jobspy (new grad, entry level, junior, ML, AI, SDE I)
- **Lever API** — 11 companies (Spotify, Palantir, Plaid, etc.)
- **Greenhouse API** — 40 companies (Stripe, Airbnb, Databricks, etc.)
- **Ashby API** — 31 companies (OpenAI, Ramp, Figma, etc.)
- **GitHub repos** — SimplifyJobs + Jobright aggregators

## Scoring Engine

Jobs are scored 0-100% match against your personal tech stack:

| Signal | Points | Example |
|--------|--------|---------|
| Keyword match | 3-10 each | Python(10), PyTorch(8), AWS(7), FastAPI(7) |
| Synergy combos | +6 to +10 | Python + FastAPI + AWS = +10 bonus |
| Level detection | +5 to +20 | "New Grad" = +20, "Entry" = +15 |
| Experience fit | +0 to +10 | 0-2 years = +10 (sweet spot) |
| Recency | -5 to +10 | < 6h = +10, > 48h = -5 |
| H1B mention | +8 | "will sponsor" in JD |
| US location | +10 | US city/state/remote |

**Hard disqualifiers:** no visa sponsorship, US citizen required, security clearance, non-US location.

See [docs/SCORING.md](docs/SCORING.md) for full details.

## Web Dashboard

The LinkedIn feed at `/linkedin` features:

- **Time filtering** — This Hour / Today / Yesterday / All Time + scrollable hourly cards
- **Sortable table** — Sort by match %, score, level, recency
- **Filter chips** — All / Recommended / New Grad / Entry / Mid / Tracking / Applied
- **Search** — Real-time text search across company, title, location
- **Sidebar stats** — Match score distribution, level breakdown, top companies, experience required
- **Status tracking** — Mark jobs as Tracking / Applied / Not Interested
- **Timezone-aware** — All times shown in your local timezone

## Resume Tailoring

```bash
# Via web dashboard: paste JD at /tailor
# Via CLI:
jobflow apply "https://..." --paste -t "SWE" -c "Stripe" -l "SF"
jobflow save --dir data/output/Stripe_SWE_2026-04-09
```

Claude AI tailors your LaTeX resume:
1. Auto-selects variant (SE / ML / AppDev) based on JD keywords
2. Preserves preamble + header + education
3. Rewrites experience, projects, skills sections
4. Compiles to PDF, auto-condenses if > 1 page

## Deployment

Deployed on Render free tier with auto-deploy from GitHub.

```
render.yaml     — Render Blueprint (auto-configures service)
Procfile        — gunicorn -w 1 wsgi:app
wsgi.py         — WSGI entry point
```

GitHub Actions hourly cron scans LinkedIn, commits data, pushes to GitHub. Push triggers Render redeploy. Same workflow pings the Render URL to prevent free-tier sleep.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for setup instructions.

## Project Structure

```
JobFlow/
├── config/
│   ├── config.yaml          # Local config
│   ├── config.ci.yaml       # CI config (GitHub Actions)
│   └── job_boards.json      # 82 company API endpoints
├── data/ci/                 # CI scan output (git-tracked)
├── jobflow/
│   ├── cli.py               # CLI commands
│   ├── config.py            # Config loader
│   ├── models.py            # JobPosting, FilterResult
│   ├── filter.py            # Scoring engine
│   ├── scanner.py           # Job scanners
│   ├── linkedin_store.py    # LinkedIn job store + dedup
│   ├── tracker.py           # CSV tracking
│   ├── tailor.py            # Resume merging
│   ├── latex.py             # PDF compilation
│   └── web/                 # Flask dashboard
├── docs/                    # Documentation
├── .github/workflows/       # Hourly scan cron
├── wsgi.py                  # Production WSGI
├── Procfile                 # Render process
└── render.yaml              # Render Blueprint
```

## Documentation

| Doc | Content |
|-----|---------|
| [Architecture](docs/ARCHITECTURE.md) | System overview, data flow, design decisions |
| [Scoring](docs/SCORING.md) | Full scoring engine breakdown |
| [API](docs/API.md) | HTTP endpoints reference |
| [Data Models](docs/DATA_MODELS.md) | JSON/CSV schemas |
| [Deployment](docs/DEPLOYMENT.md) | Render setup, GitHub Actions, env vars |
| [Frontend](docs/FRONTEND.md) | CSS architecture, JS functions, HTMX patterns |
| [Scanning](docs/SCANNING.md) | Platforms, search terms, dedup, rate limiting |
| [Tailoring](docs/TAILORING.md) | Resume flow, Claude integration |
| [CLI](docs/CLI.md) | All commands with examples |

## License

MIT
