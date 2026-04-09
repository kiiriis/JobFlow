# Deployment Guide

## Architecture

```
GitHub Actions (hourly)
    |
    ├── Scans LinkedIn via python-jobspy
    ├── Commits data/ci/scan_results.json
    ├── Pushes to GitHub
    └── Pings Render URL (keep-alive)
            |
            v
Render.com (auto-deploy on push)
    |
    ├── Builds: pip install -e .
    ├── Runs: gunicorn -w 1 wsgi:app
    └── Serves: https://jobflow-xxx.onrender.com
```

## Render Setup

### Prerequisites
- GitHub account with the JobFlow repo
- Render.com account (free, sign up with GitHub)

### Steps

1. Go to [render.com](https://render.com) and sign in with GitHub
2. Click **New** -> **Web Service**
3. Select the `kiiriis/JobFlow` repository
4. Render auto-detects `render.yaml` — settings are pre-configured:
   - **Build Command**: `pip install -e .`
   - **Start Command**: `gunicorn -w 1 -b 0.0.0.0:$PORT wsgi:app`
   - **Environment**: `JOBFLOW_CONFIG=config/config.yaml`, `PYTHON_VERSION=3.12`
   - **Plan**: Free
5. Click **Create Web Service**
6. Wait 2-3 minutes for build + deploy
7. Copy your URL (e.g., `https://jobflow-xyz.onrender.com`)

### Update Keep-Alive URL

After first deploy, update the Render URL in the GitHub Actions workflow:

**File**: `.github/workflows/scan-jobs.yml`

```yaml
- name: Keep Render alive
  run: curl -sf "$RENDER_URL/linkedin" > /dev/null 2>&1 || true
  env:
    RENDER_URL: https://your-actual-url.onrender.com  # <-- Update this
```

Commit and push the change.

## How Data Stays Fresh

1. GitHub Actions runs `jobflow scan` every hour at :00
2. New jobs are committed to `data/ci/scan_results.json` and pushed
3. The push triggers Render auto-redeploy (~1-2 min build)
4. On startup, the Flask app merges `scan_results.json` into `linkedin_jobs.json`
5. Fresh data is served immediately

## Render Free Tier Notes

- **Sleep**: Free tier sleeps after 15 min of inactivity
- **Keep-alive**: The GitHub Actions hourly ping prevents sleeping
- **Cold start**: If the ping fails and the app sleeps, first visit takes ~30s to wake
- **Ephemeral filesystem**: Changes to files are lost on redeploy (this is fine — our data is in git)
- **750 free hours/month**: Enough for 24/7 with one service
- **Auto-deploy**: Every push to `main` triggers a new deploy

## Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| `JOBFLOW_CONFIG` | `config/config.yaml` | Points to config file |
| `PYTHON_VERSION` | `3.12` | Python version for Render |
| `RENDER` | (set automatically) | Detects Render environment; disables git-pull thread |
| `PORT` | (set automatically) | Port for gunicorn to bind |

## What Works on Render vs Local

| Feature | Render | Local |
|---------|--------|-------|
| LinkedIn job feed | Yes | Yes |
| Job filtering/search | Yes | Yes |
| Status tracking | Yes (resets on redeploy*) | Yes |
| Scanner page | Yes (triggers scan) | Yes |
| Resume tailor | No (needs Claude CLI + pdflatex) | Yes |
| Auto git-pull | No (uses redeploy instead) | Yes |

*Status changes are saved to `linkedin_jobs.json` but since Render's filesystem is ephemeral, they're lost on redeploy. For persistent status tracking, a database would be needed (future enhancement).

## GitHub Actions Workflow

**File**: `.github/workflows/scan-jobs.yml`

- **Schedule**: `cron: '0 * * * *'` (every hour at :00)
- **Manual trigger**: `workflow_dispatch` (from Actions tab)
- **Steps**:
  1. Checkout repo
  2. Setup Python 3.12
  3. `pip install -e .`
  4. `jobflow scan --platform linkedin --new --save --hours 1`
  5. Commit and push `data/ci/` changes
  6. Ping Render URL (keep-alive)

## Local Development

```bash
# Install
pip install -e .

# Run web dashboard
jobflow web --port 8080

# Run a scan manually
jobflow scan --platform linkedin --hours 4

# Process a specific job
jobflow process 1
```

The local app includes a background thread that does `git pull` every hour to fetch fresh CI scan data. This thread is disabled on Render (detected via `RENDER` env var).
