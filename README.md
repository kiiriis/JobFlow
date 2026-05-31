# JobFlow

Automated LinkedIn job scanner and dashboard for Milan's new grad / entry-level semiconductor hardware search.

This branch is tuned for:

- ASIC Design Engineer
- ASIC Verification Engineer
- ASIC Physical Design Engineer
- SoC Design Engineer
- SoC Verification Engineer
- SoC Physical Design Engineer
- FPGA Design Engineer
- RTL / VLSI / Silicon Design or Verification Engineer
- GPU ASIC Engineer

It keeps the same international-student constraints: US-based roles only, visa-sponsorship friendly, OPT/H1B/CPT compatible, and no citizenship-only or clearance-required postings.

## Quick Start

```bash
git clone https://github.com/<your-username>/JobFlow.git
cd JobFlow
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest
jobflow web
```

For Milan's full setup path, use:

[docs/MILAN_SETUP.md](docs/MILAN_SETUP.md)

That guide covers GitHub ownership, Neon Postgres, GitHub Actions, local dashboard setup, optional AI scoring, troubleshooting, and daily usage. Render is optional and not required for Milan's local workflow.

## How It Works

```text
GitHub Actions
    -> LinkedIn hardware searches
    -> hardware-specific scoring/filtering
    -> Neon Postgres
    -> local jobflow web dashboard at /linkedin
```

The default workflow scans LinkedIn every 30 minutes with hardware-focused search terms from `jobflow/scanner.py`. Milan can run the dashboard on his laptop while GitHub Actions keeps Neon updated in the background.

## Important Files

| Path | Purpose |
|------|---------|
| `docs/MILAN_SETUP.md` | Milan's complete setup and deployment guide |
| `jobflow/scanner.py` | LinkedIn search terms and strict hardware title prefilter |
| `jobflow/filter.py` | ASIC/SoC/FPGA/GPU scoring, hard rejects, sponsorship filters |
| `jobflow/ai_scorer.py` | Optional Groq AI relevance scoring prompt |
| `scripts/ai_score_local.py` | Optional local Codex CLI scorer for Neon DB jobs |
| `config/profile.txt` | Milan-specific candidate profile for AI scoring |
| `.github/workflows/scan-jobs.yml` | Scheduled LinkedIn scan and Neon merge |
| `render.yaml` | Optional Render web service configuration |
| `data/ci/` | Empty JSON fallback store for non-DB deployments |

## Required Services

- GitHub repo under Milan's account
- Neon Postgres, configured as `DATABASE_URL`
- Optional Groq key as `GROQ_API_KEY` for AI scoring
- Optional local Codex CLI if Milan wants to score DB jobs from his laptop without Groq
- Optional Render web service only if Milan later wants a hosted dashboard

## Local Commands

```bash
# Run dashboard locally
DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow web

# Scan recent LinkedIn jobs
DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin --save --hours 24

# Run tests
pytest

# Optional: score Neon jobs using signed-in local Codex CLI
DATABASE_URL="<neon-url>" python scripts/ai_score_local.py --hours 24
```

## Documentation

| Doc | Content |
|-----|---------|
| [Milan Setup](docs/MILAN_SETUP.md) | End-to-end GitHub, Neon, GitHub Actions, and local dashboard setup |
| [Scoring](docs/SCORING.md) | Hardware scoring rules and target-role guard |
| [Scanning](docs/SCANNING.md) | LinkedIn search terms, filters, and scan output |
| [API](docs/API.md) | HTTP endpoints reference |
| [Data Models](docs/DATA_MODELS.md) | JSON/CSV/Postgres-shaped data fields |
| [Frontend](docs/FRONTEND.md) | Dashboard UI notes |
| [CLI](docs/CLI.md) | Command-line reference |

## Notes

- The active CI job files are intentionally empty so Milan starts with a clean feed.
- Resume tailoring code remains available, but Milan-specific resume files are not included yet.
- Use Neon for persistent jobs and statuses; JSON files are only fallback storage.
