# Multi-User Architecture

JobFlow started as a single-user tool and now runs as a self-serve, multi-tenant platform:
sign in with Google, get a private per-user feed scored by your own profile, and choose how
(if at all) AI scores your jobs. This doc explains how that works end to end.

The design goal: **one shared job pool, scored per user.** Postings are scanned and stored once;
each user's preferences produce their own scored view.

---

## Run modes

Two independent switches, both auto-detected — no code changes to flip between them:

| Switch | Off | On |
|--------|-----|-----|
| **Auth** (`GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`) | Single-user: no login, every request is the operator (`user_id=1`). | Multi-user: Google login required. |
| **DB** (`DATABASE_URL`) | JSON store (`data/ci/linkedin_jobs.json`), single-user/local. | PostgreSQL (Neon); required for multi-user. |

`jobflow/auth.py::auth_enabled()` gates auth; `jobflow/linkedin_store.py::is_db_enabled()` gates
the backend. Local dev with neither set behaves exactly like the original single-user app.

---

## Data model

The shared `jobs` table holds the **raw posting only**. Everything per-user lives in separate,
`user_id`-scoped tables (all in `jobflow/db.py::init_db()`):

| Table | Purpose |
|-------|---------|
| `jobs` | Shared posting: url, company, title, location, description, date, source. |
| `users` | Identity: `google_sub`, `email`, name, picture, `is_active`. |
| `user_profiles` | Per-user config: `filter_config` (JSONB), `ai_profile_text`, `search_terms`, `ai_provider`, `ai_api_key_enc` (BYTEA), `ai_model`, `pairing_token`. |
| `user_job_state` | Per-user, per-job: `(user_id, url)` → score, `score_pct`, `ai_score`, `ai_reason`, `recommended`, `status`, `level`, TTL. |
| `user_dismissed_jobs` | Per-user soft-deletes so a dismissed job never re-appears for that user. |

Every feed query JOINs `user_job_state s` to `jobs j` and filters `s.user_id = %s`. **No query
function runs without a `user_id`** — that's the core tenant-isolation guarantee.

---

## Auth flow (`jobflow/auth.py`)

1. `resolve_request_user()` runs `before_request`: sets `g.user_id` from the Flask session (or
   `DEFAULT_USER_ID` in single-user mode), and redirects unauthenticated users to `/auth/login`
   (401 for `/api/*`).
2. `/auth/google` → Google OAuth → `/auth/google/callback`.
3. In the callback:
   - **Operator match** — if the email equals `JOBFLOW_OPERATOR_EMAIL`, `bind_operator()` attaches
     the Google identity to `user_id=1` (which owns the migrated single-user data + AI scores).
   - **Everyone else** — `get_or_create_user()` (lookup-first by `google_sub` *or* `email`, so it
     never trips the UNIQUE constraints) creates the account and kicks off `backfill_user()` to
     score the existing pool for them in the background.
4. Only emails in `ALLOWED_EMAILS` (plus the operator) may sign in.

**Behind a proxy:** the app installs `ProxyFix` and honors `OAUTH_REDIRECT_URI` so the OAuth
session/state round-trips correctly over HTTPS on Render.

---

## Scoring fan-out

- **Scan/merge** (`db.merge_scan_results`): upsert the shared posting, then **fan out** — for each
  active user, run `evaluate_job()` with *their* `FilterProfile` and upsert `user_job_state`,
  respecting that user's dismissals. Status / AI score / first-seen are preserved across re-scores.
- **New user** (`db.backfill_user`): score the whole existing pool once, on signup.
- **Profile edit** (Settings): re-runs the user's scoring so their feed reflects the change.

The per-user knobs live in `jobflow/filter_profile.py::FilterProfile` (stack + weights, synergy
combos, sponsorship/US gates, seniority band, recommend bar). `DEFAULT_PROFILE` reproduces the
original single-user behavior exactly.

---

## AI scoring — two per-user methods

Rule-based scores are always present. AI scoring is opt-in per user via
`user_profiles.ai_provider` (`none` | `anthropic` | `local-cli`). All paths share one prompt and
parser (`jobflow/ai_prompt.py`) and write through `db.apply_user_ai_scores(user_id, …)`.

### `anthropic` — server-side, bring your own key
- Key is encrypted with Fernet (`jobflow/crypto.py`, keyed off `JOBFLOW_SECRET_KEY`) and stored in
  `user_profiles.ai_api_key_enc`. It's **never** returned to the client or logged — Settings shows
  only "key set ✓".
- The dashboard **AI Score** button runs an in-process job that decrypts the key, scores the user's
  unscored jobs via the Anthropic Messages API (`jobflow/ai_scorer_anthropic.py`) using their chosen
  model, and writes their `user_job_state`. Runs are **capped per click** (friend pays their own key).

### `local-cli` — free, bring your own compute
- The user pairs a local client with a **pairing token** (generated in Settings; regenerate to
  revoke) and runs it on their own machine:
  ```bash
  pip install "git+https://github.com/kiiriis/JobFlow"
  jobflow login --token <token>
  jobflow score
  ```
- `jobflow login` stores `{server, token}` in `~/.jobflow/credentials.json`.
- `jobflow score` calls two token-authed endpoints:
  - `POST /api/score/pending` → returns the user's unscored jobs + their `ai_profile_text` + model.
  - `POST /api/score/submit` → writes the scores back.
  - Both are **exempt from the login gate** but strictly scoped to the token's user
    (`db.get_user_by_pairing_token`) — a token can only read/write its own jobs.
- Scoring runs with the user's **signed-in** `claude`/`codex` CLI via `jobflow/ai_local.py`.

### Operator
The operator (server owner) can also score directly from the dashboard **AI Score** button, which
shells out to the server's local CLI subprocess (`scripts/ai_score_local.py --user-id 1`). No API
key or token needed.

---

## TTL & pruning

- Per-user rows in `user_job_state` expire on the usual TTL unless the user set the job to
  Tracking/Applied.
- A shared posting is pruned only when **no** `user_job_state` references it and it's past the pool
  retention window — so a posting one user is tracking never disappears for others.

---

## Migration (single-user → multi-user)

`db.migrate_to_multiuser()` (also `jobflow migrate-multiuser`) is idempotent: it seeds the operator
user (`user_id=1`) and copies the legacy per-job columns from `jobs` into `user_job_state(1)` plus
`dismissed_jobs` into `user_dismissed_jobs(1)`. `init_db()` runs the multi-tenant DDL on every
startup (also idempotent), so a fresh deploy provisions all tables automatically.

---

## Tenant-isolation checklist

- Every read/write is scoped by `user_id`; feed queries JOIN `user_job_state`.
- Token endpoints resolve the user *from the token*, never from a client-supplied id.
- Encrypted API keys are decrypted only inside the scorer; never serialized to responses/logs.
- Regenerating a pairing token invalidates the previous one.
