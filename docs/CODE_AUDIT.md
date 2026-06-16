# JobFlow — Code Quality Audit

_Last updated: 2026-06-16. Supersedes the historical `DASHBOARD_AUDIT.md`._

This document captures (1) a deep map of the codebase, (2) an honest quality
assessment, (3) the issues fixed in the June 2026 production-cleanup pass, and
(4) the findings left open, with rationale. It is meant to be the single
orientation doc for anyone picking up the project.

---

## 1. What the system is

An automated new-grad/entry-level SWE job pipeline:

```
GitHub Actions (cron */30)                         Render (Flask, gunicorn -w 1)
        │                                                    │
   jobflow scan ──► scanner.py ──► filter.evaluate_job ──► store ◄── web dashboard
        │            (LinkedIn,        (rule-based            │        (/linkedin feed)
        │             Lever, GH,        0–100% score +        │
        │             Ashby, GH         hard-reject flags)    │
        │             repos)                                  ▼
        │                                       AI scorer (overrides rule score)
        │                                       • scripts/ai_score_local.py  (canonical:
        ▼                                         signed-in Claude/Codex CLI, no API key)
   git commit / DB write                         • jobflow/ai_scorer.py      (legacy:
                                                   Groq Llama-4-Scout, GROQ_API_KEY)
```

**Storage is dual-backend.** `DATABASE_URL` set → PostgreSQL (Neon); otherwise
JSON files under `data/ci/`. `linkedin_store.is_db_enabled()` is the switch;
every web route tries the DB and falls back to JSON.

**The web app is one page** (the `/linkedin` feed) plus its JSON/fragment API.
Application tracking, the `/tailor`/`/scan`/`/boards` pages, `/api/stats`, and the
sidebar analytics panel were removed in June 2026. `tracker.py`, `tailor.py`, and
`latex.py` remain as CLI-only modules.

### Module map

| Module | LOC | Responsibility |
|--------|----:|----------------|
| `filter.py` | ~720 | Rule-based scoring + hard-reject pipeline. The heart. |
| `scanner.py` | ~760 | Multi-platform fetch (LinkedIn via jobspy, Lever/GH/Ashby REST, GH repos), dedup. |
| `linkedin_store.py` | ~900 | JSON store: merge, re-score, prune, filter, time buckets. Defines shared helpers DB reuses. |
| `db.py` | ~1070 | PostgreSQL backend mirroring the store API; one-shot URL canonicalization. |
| `web/__init__.py` | ~680 | Flask factory, feed/scan/AI-score routes, DB-with-JSON-fallback plumbing. |
| `cli.py` | ~500 | Typer CLI: scan, apply, save, process, list, status, init, web, normalize-urls. |
| `ai_scorer.py` | ~240 | Legacy/optional Groq scorer. |
| `scripts/ai_score_local.py` | ~620 | Canonical AI scorer (local Claude/Codex CLI, batched). |
| `tailor.py` / `latex.py` | ~250 | LaTeX merge + pdflatex compile (CLI resume flow). |
| `tracker.py` | ~240 | CSV application tracker (CLI `list`/`status`). |
| `models.py`, `config.py`, `scraper.py`, `db_migrate.py` | ~340 | Dataclasses, YAML config, JD parser, JSON→PG migration. |

---

## 2. Overall assessment

**Strengths**
- Genuinely well-commented: most modules open with a docstring that explains
  intent and data flow, not just mechanics.
- Clean dual-backend abstraction — the web layer is agnostic to JSON vs. Postgres.
- Careful real-world hardening: URL canonicalization for dedup, atomic JSON
  writes, connection validation against Neon idle-timeout, retry/backoff on fetch,
  timezone-aware time buckets, defensive `try/except` around the DB path.
- Strong test coverage (338 tests) over the parts that matter most (filter,
  store merge/prune, buckets, URL normalization, web smoke).

**Principal weakness: a second copy of the scoring logic.**
`linkedin_store._rescore_entry()` re-implements `filter.evaluate_job()` against
dict rows instead of `JobPosting` objects. The two must be kept in lockstep by
hand, and they had already drifted (see 3.3). This is the highest-leverage
refactor remaining (see 4.1).

**Secondary theme: drift.** The code had evolved faster than its tests and docs —
an intentional design change (keep hard-rejected jobs) left the test suite red and
several docstrings describing removed behavior. Most of this pass was paying that
down.

---

## 3. Issues fixed in this pass (behavior-preserving)

### 3.1 Red test suite → green (86 failures → 0)
- **`should_apply` cluster (52):** `evaluate_job` was changed so hard-rejects
  return `should_apply=True` with a `reject_reason` (jobs are kept for AI
  re-scoring), but `test_filter.py` still asserted the old `should_apply is False`.
  The boolean was **always `True`** — a dead field. Removed `should_apply` from
  `FilterResult` entirely; "rejected" is now `reject_reason != ""`. Updated all
  readers (`scanner`, `cli`, `web`) and tests.
- **Bucket cluster (34):** `_bucket_minutes()` was simplified to a constant 30
  (the cron is `*/30 * * * *`), but `test_buckets.py` still asserted dynamic
  30/60/240-min buckets. Rewrote those tests to the fixed-30-min reality.

### 3.2 Dead `should_apply` branches removed
The always-`True` flag meant the apply/skip split in `print_scan_results`,
`cli.scan`, and `web._run_scan` was permanently one-sided (skip count always 0).
These now split on `reject_reason` for display while still **storing every job**
(behavior preserved). `cli.apply`'s unreachable "filtered out → Skipped" branch
was removed.

### 3.3 `_rescore_entry` ↔ `evaluate_job` divergence (real bug)
`_rescore_entry` was **missing the non-US location hard-reject** that
`evaluate_job` has. A non-US GitHub-sourced job that `evaluate_job` flagged at
scan time would be silently "un-rejected" and given a positive score on the next
store merge. Extracted a shared `filter.is_us_location()` helper (with module-level
`US_STATE_ABBREVS` / `US_LOCATION_PATTERNS`) and called it from both paths so they
stay consistent.

### 3.4 Triplicated staffing/spam blocklist → one source
The aggregator blocklist + compact-matching logic was copy-pasted in
`ai_scorer.py`, `scripts/ai_score_local.py`, and partially `filter.py`. Unified
into `filter.STAFFING_SOURCE_BLOCKLIST` + `STAFFING_BLOCK_REASON` +
`text_has_blocked_source()`; both AI scorers now import it.

### 3.5 Misleading docstrings / docs corrected
- `linkedin_store._rescore_entry` said AI scores come from **"GPT-4o-mini"** —
  nothing uses GPT. Corrected to point at the real scorers.
- `__init__.py`, `ai_scorer.py`, `cli.py` now mark the Groq path as legacy/optional
  and name `scripts/ai_score_local.py` as canonical.
- Bucket docstrings claimed dynamic weekday/weekend sizing; corrected to fixed 30.
- `tailor.py`/`latex.py`/`tracker.py` docstrings referenced removed web routes
  (`/api/tailor/generate`, `/api/stats`); corrected to CLI-only.
- `docs/`: rewrote `API.md` to the real routes; fixed `SCORING.md` (incl.
  `SCORE_MAX_RAW` 130→140 and the thresholds table), `DATA_MODELS.md`,
  `ARCHITECTURE.md`, `TAILORING.md`, `FRONTEND.md`, `README.md`; added a
  "superseded" banner to `DASHBOARD_AUDIT.md`.

### 3.6 Dead code removed
- `latex.get_page_count()` — orphaned with the removed auto-condense feature
  (also dropped the now-unused `import re`).
- `linkedin_store.get_sidebar_stats()` — orphaned with the removed sidebar panel.
- `linkedin_store.RECOMMENDED_THRESHOLD` (superseded by `filter.RECOMMENDED_MIN_PCT`)
  and `linkedin_store.USE_DB` (a load-time snapshot nothing read).

---

## 4. Open findings & recommendations (not changed)

Ordered by leverage. None block correctness today; all are deliberate "leave it"
calls for a behavior-preserving pass.

### 4.1 Unify the two scoring implementations — **high value**
`evaluate_job(JobPosting)` and `_rescore_entry(dict)` encode the same pipeline
twice. Recommendation: have `_rescore_entry` build a `JobPosting` from the row,
call `evaluate_job`, and map the result back — or extract a single
`score(title, company, location, description, first_seen)` core both call. This
eliminates a whole class of drift bugs (3.3 was one). Medium effort; needs the
store/db tests as a guard plus a few new equivalence tests.

### 4.2 Duplicated US-pattern list inside `evaluate_job`
The location **bonus** still uses a second, slightly different inline `us_patterns`
list (separate from the hard-reject `is_us_location`). Folding it into
`is_us_location` would change scores for blank/ambiguous locations (+10 vs −10),
so it was left to preserve behavior. Worth aligning when 4.1 is done.

### 4.3 Unused CRUD primitives in `db.py`
`get_job()`, `save_seen_job()`, and `prune_seen_jobs()` have no in-repo callers.
They form a coherent backend surface and may be used for ops/manual invocation, so
they were kept — but if they're not, delete them. (`prune_expired_jobs` and
`save_seen_jobs_bulk` ARE used; don't confuse them.)

### 4.4 Two AI-scoring code paths with a duplicated prompt
`ai_scorer.py` (single-job, Groq) and `ai_score_local.py` (batched, CLI) carry
near-identical scoring prompts. They serve different runtimes (unattended CI vs.
interactive dashboard) and the user chose to keep both. Consider extracting the
shared prompt text to one constant to prevent the rubrics from drifting apart.

### 4.5 Naming: underscore-prefixed cross-module API
`db.py` imports `_rescore_entry`, `_dedup_key`, `_parse_iso`, `_bucket_*` from
`linkedin_store`, and `linkedin_store` imports `_has_phrase` from `filter`. The
leading underscore signals "private" but these are a deliberate shared surface.
Either drop the underscore (promote to public) or relocate them to a small
internal module. Cosmetic; do it alongside 4.1.

### 4.6 Minor
- `extract_experience` uses character class `[-–to]+`, which also matches stray
  `t`/`o` between numbers. Works for current inputs; tighten to `(?:-|–|to)` if
  touched.
- `web._auto_pull_loop` swallows all exceptions silently — fine for a daemon, but
  a debug log line would help diagnose stale local data.
- `print_scan_results` renders the full relevant set; harmless for a CLI but large
  on big scans.

---

## 5. Test suite status

`python -m pytest` → **338 passed**. Coverage by area: filter rules & integration
(`test_filter`), store merge/prune/status (`test_store`), time buckets
(`test_buckets`), URL canonicalization (`test_url_normalization`,
`test_url_migration_db`), AI-scorer eligibility/blocklist (`test_ai_scorer`,
`test_ai_score_local`), and web routes (`test_web`). Gaps worth adding: an
equivalence test asserting `evaluate_job` and `_rescore_entry` agree on the same
job (would have caught 3.3), and a `db.py` test path (currently only the JSON
backend is exercised).
