# Milan JobFlow Setup Guide

This guide explains how Milan should run JobFlow without Render:

```text
GitHub Actions scans LinkedIn every 30 minutes
    -> jobs are saved in Neon Postgres
    -> Milan runs jobflow web locally
    -> the local dashboard reads the same Neon database
```

That means Milan does not need to keep his laptop awake for scans. GitHub does the recurring scan, Neon stores the jobs and dashboard statuses, and Milan opens the dashboard locally whenever he wants to review the latest roles.

## What This Branch Does

This branch is customized for Milan's strict new grad / entry-level semiconductor hardware job search:

- Target roles: ASIC Design, ASIC Verification, ASIC Physical Design, SoC Design, SoC Verification, SoC Physical Design, FPGA Design, RTL/VLSI, Silicon Design/Verification, and GPU ASIC.
- Excluded roles: embedded-only, firmware-only, generic SWE, backend, frontend, data, ML/AI, DevOps/SRE, IT/support, product, sales, software QA/testing, senior/staff/principal/lead, no-sponsorship, citizenship-only, and clearance-required roles.
- Location and visa constraints: US-based roles only, international-student friendly, OPT/H1B/CPT compatible.
- Scanner: LinkedIn-only workflow by default, using hardware-specific search terms.
- Storage: Neon Postgres when `DATABASE_URL` is configured. JSON files are only fallback storage.
- AI scoring: optional. Groq can run in GitHub Actions; local Codex scoring can run on Milan's laptop.

## Accounts Milan Needs

Required:

- GitHub account
- Neon account for Postgres

Optional:

- Groq account/API key for AI relevance scoring inside GitHub Actions
- Local Codex CLI login for local AI rescoring
- Render account only if Milan later wants a hosted web dashboard

Recommended setup: use Neon. Without Neon, GitHub Actions can still commit JSON files, but local status changes are harder to keep in sync. With Neon, Milan can mark jobs as Tracking, Applied, and Not Interested from his local dashboard, and those statuses persist.

## 1. Put The Code In Milan's GitHub

### Option A: Push This Branch To Milan's Own Repo

On Milan's machine:

```bash
git clone https://github.com/kiiriis/JobFlow.git
cd JobFlow
git fetch origin milan-jobflow
git switch -c milan-jobflow origin/milan-jobflow
```

Create a new empty GitHub repository under Milan's account, for example:

```text
https://github.com/<milan-github-username>/JobFlow.git
```

Point the local repo at Milan's GitHub and push:

```bash
git remote rename origin krish
git remote add origin https://github.com/<milan-github-username>/JobFlow.git
git push -u origin milan-jobflow
```

Recommended: make Milan's repo branch `main`, because GitHub defaults are simpler:

```bash
git branch -m milan-jobflow main
git push -u origin main
```

### Option B: Milan Forks On GitHub

If Milan forks Krish's repo on GitHub:

```bash
git clone https://github.com/<milan-github-username>/JobFlow.git
cd JobFlow
git checkout milan-jobflow
```

He can keep using `milan-jobflow`, or rename it to `main` in his fork.

## 2. Create Neon Postgres

1. Go to Neon.
2. Create a new project.
3. Use the default database, or create a database named `jobflow`.
4. Copy the Postgres connection string.

The connection string usually looks like:

```text
postgresql://user:password@host.neon.tech/dbname?sslmode=require
```

This same value is used as `DATABASE_URL` in two places:

- GitHub Actions secret, so the automatic scanner can write jobs.
- Milan's local terminal, so `jobflow web` can read jobs and save statuses.

The app creates its own tables automatically when `DATABASE_URL` is set.

## 3. Configure GitHub Actions Secrets

In Milan's GitHub repo:

1. Go to `Settings` -> `Secrets and variables` -> `Actions`.
2. Add these repository secrets:

| Secret | Required | Value |
|--------|----------|-------|
| `DATABASE_URL` | Yes | Neon Postgres connection string |
| `GROQ_API_KEY` | Optional | Groq API key for AI relevance scoring |

Workflow behavior:

- If `DATABASE_URL` is set, scans merge into Neon.
- If `DATABASE_URL` is missing, scans fall back to `data/ci/linkedin_jobs.json` and commit JSON back to the branch.
- The workflow pushes to the branch that ran it, not hardcoded `main`.

## 4. Enable And Run GitHub Actions

The workflow file is:

```text
.github/workflows/scan-jobs.yml
```

It runs every 30 minutes:

```yaml
schedule:
  - cron: '*/30 * * * *'
```

Run it manually once after adding `DATABASE_URL`:

1. Open Milan's GitHub repo.
2. Click `Actions`.
3. Select `Scan LinkedIn Jobs`.
4. Click `Run workflow`.
5. Choose the branch Milan uses, usually `main`.

The workflow runs:

```bash
JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin --save --hours 4
```

Then it initializes Neon tables and merges scan results into Postgres.

In a good first run, the logs should show:

```text
DATABASE_URL is set: true
Merged: <n> new jobs
```

If `Merged: 0 new jobs`, it may simply mean LinkedIn returned no matching roles in the last 4 hours. For testing, temporarily change the workflow scan window from `--hours 4` to `--hours 24`, commit, push, and run the workflow again.

## 5. Install JobFlow Locally

Milan needs Python 3.11+ or 3.12.

From the repo root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest
```

Expected test result for this branch:

```text
311 passed
```

The `config/profile.txt` file is already Milan-specific and should stay committed.

## 6. Run The Local Dashboard Against Neon

The local web app does not automatically read `.env`, so the simplest command is:

```bash
DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow web
```

Then open the printed local URL and go to:

```text
/linkedin
```

This local dashboard reads jobs directly from Neon. Milan does not need Render for this. He also does not need to manually pull JSON files when using Neon.

Useful local shell setup:

```bash
export DATABASE_URL="<neon-url>"
export JOBFLOW_CONFIG=config/config.ci.yaml
jobflow web
```

With this setup:

- GitHub Actions keeps finding latest jobs every 30 minutes.
- Neon stores all jobs.
- The local dashboard shows whatever is currently in Neon.
- Status changes from the local dashboard are saved back into Neon.

## 7. Daily Usage

### Review Jobs

Run:

```bash
source .venv/bin/activate
export DATABASE_URL="<neon-url>"
export JOBFLOW_CONFIG=config/config.ci.yaml
jobflow web
```

Open:

```text
/linkedin
```

Suggested workflow:

1. Check `Recommended`, `New Grad`, and `Entry` filters.
2. Open promising jobs.
3. Mark good ones as `Tracking`.
4. Mark submitted ones as `Applied`.
5. Mark irrelevant jobs as `Not Interested`.

Those statuses persist in Neon.

### Force A Fresh Scan

From GitHub:

```text
Actions -> Scan LinkedIn Jobs -> Run workflow
```

From local, if Milan wants to test the scanner manually:

```bash
DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin --save --hours 24
```

GitHub Actions is the normal automatic path. The local scan command is only for testing or debugging.

### Change Scan Frequency

Edit `.github/workflows/scan-jobs.yml`.

Common schedules:

```yaml
# Every 30 minutes
- cron: '*/30 * * * *'

# Every hour
- cron: '0 * * * *'

# Every 4 hours
- cron: '0 */4 * * *'
```

GitHub cron times are UTC.

## 8. Optional AI Scoring

### Option A: Groq In GitHub Actions

If `GROQ_API_KEY` is set as a GitHub Actions secret, scans can run AI relevance scoring automatically.

The Milan-specific AI prompt lives in:

```text
jobflow/ai_scorer.py
```

The Milan profile lives in:

```text
config/profile.txt
```

AI scoring is optional. Without Groq, the hardware keyword scoring still works.

### Option B: Local Codex AI Scoring

Milan can score jobs already stored in Neon from his laptop using the local Codex CLI. This is useful if he does not want to use Groq in GitHub Actions.

Requirements:

- `DATABASE_URL` points to Milan's Neon database.
- `codex` CLI is installed, authenticated, and available on `PATH`.
- `config/profile.txt` remains Milan-specific.

Run:

```bash
DATABASE_URL="<neon-url>" python scripts/ai_score_local.py --hours 24
```

Useful options:

```bash
# Score latest 50 eligible jobs
DATABASE_URL="<neon-url>" python scripts/ai_score_local.py --limit 50

# Score jobs first seen in the past 12 hours
DATABASE_URL="<neon-url>" python scripts/ai_score_local.py --hours 12

# Combine both
DATABASE_URL="<neon-url>" python scripts/ai_score_local.py --hours 24 --limit 100
```

What it does:

1. Reads unscored or Groq/Claude-scored jobs from Neon.
2. Sends batches to local `codex exec` with Milan's ASIC/SoC/FPGA/GPU scoring prompt.
3. Writes `ai_score`, `ai_reason`, `score_pct`, `recommended`, and `ai_model='codex'` back to Neon.
4. Blocks known staffing/spam sources before spending a Codex call.

## 9. Widen Or Narrow The Search

LinkedIn search terms live in:

```text
jobflow/scanner.py
```

The current terms include:

- `New Grad ASIC Design Engineer`
- `Entry Level ASIC Verification Engineer`
- `New Grad Physical Design Engineer`
- `New Grad SoC Verification Engineer`
- `Entry Level FPGA Design Engineer`
- `New Grad GPU ASIC Engineer`
- `Entry Level RTL Design Engineer`
- `Entry Level VLSI Engineer`

Hardware filters and scoring live in:

```text
jobflow/filter.py
```

Important sections:

- `TARGET_HARDWARE_TITLE_PATTERNS`
- `NON_TARGET_TITLE_PATTERNS`
- `STACK_CATEGORIES`
- `SYNERGY_COMBOS`
- `DISQUALIFYING_PHRASES`

After changing scanner or scoring behavior:

```bash
pytest
git add .
git commit -m "Tune Milan hardware job filters"
git push
```

GitHub Actions will use the updated code on the next run.

## 10. Data Files And Database

Active data files were intentionally reset for Milan:

```text
data/ci/scan_results.json  -> []
data/ci/linkedin_jobs.json -> {"jobs": {}, "last_updated": ""}
data/ci/seen_jobs.json     -> {}
```

When `DATABASE_URL` is set, Neon is the source of truth. The JSON files are fallback only.

The app initializes database tables automatically when `DATABASE_URL` is set:

- GitHub Actions calls `init_db()` before merging scan results.
- The local web dashboard calls `init_db()` when it starts.

If Milan ever has JSON data he wants to move into Neon:

```bash
DATABASE_URL="<neon-url>" python -m jobflow.db_migrate
```

## 11. Common Problems

### GitHub Actions Says `DATABASE_URL is set: false`

Fix:

1. Add `DATABASE_URL` under GitHub repo `Settings` -> `Secrets and variables` -> `Actions`.
2. Re-run the workflow manually.

### Local Dashboard Is Empty

Check:

1. Did GitHub Actions run successfully?
2. Is local `jobflow web` using the same `DATABASE_URL` as GitHub Actions?
3. Did the workflow log say `Merged: <n> new jobs`?
4. Did LinkedIn return zero results because the scan window was too narrow?

For testing, temporarily change the workflow scan command from:

```bash
jobflow scan --platform linkedin --save --hours 4
```

to:

```bash
jobflow scan --platform linkedin --save --hours 24
```

### Status Changes Disappear

This usually means the local dashboard was started without `DATABASE_URL`, so it used JSON fallback storage.

Start it with:

```bash
DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow web
```

### Workflow Push Fails

Check repo settings:

1. Go to `Settings` -> `Actions` -> `General`.
2. Under `Workflow permissions`, choose `Read and write permissions`.
3. Save.

The workflow needs write permission only for JSON fallback mode. With Neon configured, this is less important, but still fine to enable.

### LinkedIn Scan Is Slow Or Returns Few Jobs

LinkedIn scraping can be rate-limited, and strict hardware searches can be sparse. Re-run later, widen the scan window, or adjust search terms in `jobflow/scanner.py`.

## 12. Optional Render Later

Render is not needed for Milan's desired setup. Use Render only if Milan later wants a public hosted dashboard.

If he does deploy it, Render must use the same environment variables:

| Variable | Required | Value |
|----------|----------|-------|
| `DATABASE_URL` | Yes | Same Neon connection string used by GitHub Actions |
| `JOBFLOW_CONFIG` | Yes | `config/config.ci.yaml` |
| `PYTHON_VERSION` | Yes | `3.12` |
| `GROQ_API_KEY` | Optional | Same Groq key if AI scoring should be available |

The existing `render.yaml` is only for that optional hosted-dashboard path.

## 13. Quick Checklist

- [ ] Milan has his own GitHub repo with this branch pushed.
- [ ] `config/profile.txt` is Milan-specific and committed.
- [ ] Neon project created.
- [ ] `DATABASE_URL` added to GitHub Actions secrets.
- [ ] GitHub Actions workflow enabled and manually run once.
- [ ] Workflow logs show `DATABASE_URL is set: true`.
- [ ] Milan installed locally with `python -m venv .venv`, `pip install -e .`, and `pytest`.
- [ ] Milan runs local dashboard with `DATABASE_URL="<neon-url>" JOBFLOW_CONFIG=config/config.ci.yaml jobflow web`.
- [ ] `/linkedin` loads from the local dashboard.
- [ ] Status changes persist after refresh.
- [ ] Optional `GROQ_API_KEY` added to GitHub Actions.
- [ ] Optional local Codex AI scoring tested with `scripts/ai_score_local.py`.
