# JobFlow

CLI tool that automatically scans 80+ company job boards for new grad / entry-level software engineering positions, filters by relevance, tailors your LaTeX resume per job, and tracks applications locally.

Built to run from within [Claude Code](https://claude.ai/claude-code) — Claude acts as the agent that does the thinking (filtering, resume tailoring), while the CLI handles orchestration and I/O.

## What It Does

```
jobflow scan
  |
  +--> Lever API (11 companies)
  +--> Greenhouse API (40 companies)
  +--> Ashby API (31 companies)
  +--> LinkedIn guest API (4 search queries x 2 pages)
  +--> GitHub new-grad repos (SimplifyJobs + Jobright.ai)
  |
  +--> Filter: new grad? USA? OPT-friendly? SWE role?
  |
  +--> Relevant jobs displayed + saved to scan_results.json
```

```
jobflow apply <url> --paste -t "SDE 1" -c "Stripe" -l "SF"
  |
  +--> Parse job description
  +--> Filter (score 0-100, should_apply true/false)
  +--> Auto-select resume variant (SE / ML / AppDev)
  +--> Claude tailors resume using master prompt
  +--> Merge into full .tex + compile PDF
  +--> Track in applications.csv
```

## Setup

```bash
# Clone and install
git clone https://github.com/kiiriis/JobFlow.git
cd JobFlow
pip install -e .

# Initialize
jobflow init

# Place your base resumes in resumes/base/
# Edit config/config.yaml with your paths
```

**Requirements:**
- Python 3.11+
- `pdflatex` for PDF compilation (optional — `brew install --cask mactex`)

## Usage

### Scan for jobs

```bash
# Scan all sources (Lever + Greenhouse + Ashby + LinkedIn + GitHub repos)
jobflow scan

# Only jobs posted in the last 24 hours
jobflow scan --hours 24

# Only show new jobs since last scan (deduplication)
jobflow scan --new

# Scan a specific platform
jobflow scan --platform greenhouse
jobflow scan --platform linkedin
jobflow scan --platform github
```

### Apply to a job

```bash
# Paste a job description and process it
jobflow apply "https://jobs.lever.co/company/123" \
  --paste \
  --title "Software Engineer, New Grad" \
  --company "Stripe" \
  --location "San Francisco, CA"

# Save tailored resume (after Claude generates the sections)
jobflow save --dir data/output/Stripe_SWE_2026-03-28

# Process a job from scan results by index
jobflow process 3
```

### Track applications

```bash
# View all tracked applications
jobflow list
```

## Project Structure

```
JobFlow/
├── config/
│   ├── config.yaml            # Paths, settings
│   └── job_boards.json        # 82 API endpoints + 60 career page URLs
├── resumes/
│   ├── base/                  # Base LaTeX resumes (SE, ML, AppDev variants)
│   └── prompt.md              # Master resume tailoring prompt
├── data/
│   ├── applications.csv       # Application tracker
│   └── output/                # Generated resumes + scan results
│       └── {Company}_{Role}_{Date}/
│           ├── tailored_resume.tex
│           ├── tailored_resume.pdf
│           ├── job_description.txt
│           └── metadata.json
├── jobflow/
│   ├── cli.py                 # Typer CLI (scan, apply, save, process, list, init)
│   ├── scanner.py             # Lever/Greenhouse/Ashby/LinkedIn/GitHub scanners
│   ├── filter.py              # Job relevance scoring + visa/location filters
│   ├── tailor.py              # Resume section extraction + merging
│   ├── latex.py               # pdflatex compilation
│   ├── tracker.py             # CSV operations
│   ├── scraper.py             # Job posting text parser
│   ├── config.py              # Config loader
│   └── models.py              # JobPosting, FilterResult dataclasses
└── pyproject.toml
```

## Filter Criteria

Jobs are scored 0-100 and filtered by:

| Criteria | Effect |
|---|---|
| Entry-level / new grad / SDE 1 signals | +30 score |
| Senior / staff / lead / 3+ YOE | -30 score |
| USA location detected | +10 score |
| Non-US location (India, UK, etc.) | Disqualified |
| "No sponsorship" / "US citizen required" | Disqualified |
| Score < 40 | Skipped |

Resume variant is auto-selected based on JD keywords:
- **ML/AI** keywords (PyTorch, ML, data science) -> `ml` variant
- **Full-stack/frontend** keywords (React, frontend) -> `appdev` variant
- Default -> `se` variant

## Job Board Coverage

### API-scanned automatically (82 companies)
- **Greenhouse (40):** Stripe, Anthropic, Databricks, Airbnb, Discord, Coinbase, Cloudflare, Waymo, Figma, Vercel, Datadog, Duolingo, Dropbox, Lyft, Pinterest, MongoDB, GitLab, Elastic, Brex, Verkada, Scale AI, Block, and more
- **Ashby (31):** OpenAI, Cursor, Perplexity, Ramp, Notion, Snowflake, Replit, ElevenLabs, Cohere, Modal, Supabase, Neon, Confluent, Vanta, PostHog, and more
- **Lever (11):** Spotify, Palantir, Plaid, Mistral, Zoox, and more

### Aggregator sources
- **LinkedIn** guest API — 4 search queries, new grad + entry level SWE
- **GitHub repos** — SimplifyJobs/New-Grad-Positions + Jobright.ai 2026 new grad list

### Career pages (60+ companies, Playwright required)
Big Tech, enterprise, semiconductors, quant/trading firms, fintech/banking, healthcare, and devtools companies stored in `config/job_boards.json` for manual or Playwright-based scraping.

## How Resume Tailoring Works

1. **Base resume** selected (SE, ML, or AppDev variant)
2. **Preamble + header + education** preserved untouched from base
3. **Experience, Projects, Skills** rewritten by Claude following the master prompt:
   - ATS-optimized bullet points
   - Action Verb + Deliverable + How + Measurable Impact
   - Tech stack realigned to match JD
   - Realistic, interview-defensible metrics only
   - Exactly 3 bullets per role
4. **Merged** back into full `.tex` and compiled to PDF

## Dependencies

Just two:
- `typer[all]` — CLI framework with rich output
- `pyyaml` — Config file parsing

No external API keys. No cloud services. Claude Code itself is the LLM agent.

## License

MIT
