# TODO

## Task 1 — Fix location filtering (still getting non-US jobs)

**Priority:** High

The LinkedIn scanner is set to `location="United States"` but some jobs from Canada and other countries still leak through. Need to investigate:

- Is python-jobspy returning non-US results despite the location filter?
- Should there be a post-scan location filter that rejects jobs with non-US locations?
- Check if the hard-reject pipeline in `filter.py` needs a location validation step (currently it trusts the search parameter and gives a flat `loc_score = 10`)

**Goal:** Zero non-US jobs in the feed.

## Task 2 — AI-score every job description with a free model (no API key)

**Priority:** High

Currently AI scoring uses OpenAI (`OPENAI_API_KEY` required). Find a way to score every job's description with an AI model for free, without requiring an API key. Options to explore:

- Local models (Ollama, llama.cpp) — possible in CI?
- Free-tier APIs (Groq, Together, etc.)
- Claude CLI (`claude -p`) — already installed, used for tailor
- Hugging Face Inference API (free tier, rate limited)

**Goal:** Every job gets an `ai_score` (0-10) so the Recommended filter is useful. Currently `Rec 0` because no jobs are AI-scored.

## Task 3 — Fix time chips to show today only (midnight-based, not rolling 24h)

**Priority:** High

The time bucket chips on the LinkedIn feed mix yesterday and today. As shown in the screenshot at 11:07 PM, the chips show "11 PM, 6 PM, 5:30 PM, 3 PM, 1:30 PM, 10:30 AM, 11 PM" — that last "11 PM" is from yesterday, not today. The default view and chips should be strictly today (12:00 AM local time onward), not a rolling 24-hour window.

**Current behavior:** Chips show last 24 hours (e.g., 11 PM yesterday to 11 PM today).
**Expected behavior:** Chips show only today's buckets (12:00 AM onward). Yesterday's jobs belong under the "Yesterday" tab only.

Files to investigate:
- `linkedin_store.py` — `get_time_counts()` uses `diff_hours < 24` for bucket inclusion
- `db.py` — `get_time_counts()` uses `twenty_four_ago` for bucket query
- The default tab on page load should be "Today", not "All Time"

## Task 4 — Address remaining security concerns from DB migration audit

**Priority:** Low

See `DB_MIGRATION_AUDIT.md` for full details. Key open items:

- **Issue #15:** Upsert overwrites `recommended` and `score_pct` for AI-scored jobs (regression from upsert refactor) — fix the `ON CONFLICT` clause with `CASE WHEN` guards
- **Issue #8:** Race condition in scan trigger (low risk, single-user app)
- General: review f-string SQL construction patterns for long-term safety
