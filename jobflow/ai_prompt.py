"""Shared AI-scoring prompt and parsing — the single source of truth used by
every scorer: the local CLI script (scripts/ai_score_local.py), the server-side
Anthropic-API scorer (jobflow/ai_scorer_anthropic.py), and the local CLI client
(`jobflow score`). Keeping the prompt and the parse/normalize logic here means
all three score jobs identically; only the transport (subprocess CLI vs API)
differs.
"""

import json
import re

from .filter import STAFFING_BLOCK_REASON, text_has_blocked_source  # noqa: F401 (re-exported)

BATCH_SIZE = 15

BATCH_PROMPT = """You are a job relevance scorer for a new grad / entry-level software engineer on F1 OPT visa looking for their first full-time role in the US.

## Candidate Profile
{profile}

## HARD REJECT — score MUST be 0
Give a score of 0 ONLY if ANY of these are true. Be strict — only these 6 conditions warrant a 0:

1. **Explicit no sponsorship**: The posting explicitly says "no sponsorship", "will not sponsor", "cannot sponsor", "must be authorized to work without sponsorship", "US citizen only", "permanent resident only", "green card required". Also score 0 if it requires security clearance (TS/SCI, Secret, DoD). NOTE: "Must be authorized to work in the US" ALONE is NOT a rejection — OPT holders ARE authorized.

2. **3+ years experience required**: The minimum required experience is 3 or more years. "1-2 years" or "2+ years" is fine. "3+ years" is NOT.

3. **Senior/Staff/Lead role**: Clearly senior-level, staff, principal, architect, VP, director, or management. Must be obvious from title or JD — don't assume.

4. **Not a software engineering role at all**: QA-only, technical writing, product management, sales engineering, IT support. NOTE: Frontend, Full-Stack, iOS, Android, Data Science WITH coding, DevOps WITH development — these ARE software engineering. Score them low (2-4) if poor fit, but NOT 0.

5. **Not US-based**: Located outside the US with no remote-US option.

6. **Blocked staffing/spam source**: The job is from jobright.ai, Remotehunter, Quik Hire Staffing, Beacon Fire, Helic & Co., Jack and Jill, or Jobs Via Dice.

IMPORTANT: The candidate's "Avoid" preferences (e.g., "Avoid: Frontend-only") should LOWER the score (2-4) but NEVER cause a score of 0. A frontend SWE role is still a software engineering role — it's just a weak fit, not a hard reject.

## SCORING GUIDE (only if no hard reject applies)

**9-10 — Perfect fit:** Entry-level/new grad SWE, ML, Backend, Data Engineer. Python/ML/backend stack. Sponsors visas. Reputable company.
**7-8 — Strong fit:** SWE at right level, good stack overlap, US-based, no sponsorship denial.
**5-6 — Decent fit:** Relevant SWE but weaker stack match (Java, .NET, frontend). Level ambiguous.
**3-4 — Weak fit:** SWE but poor overlap (iOS, Salesforce, embedded, frontend-only). Borderline exp.
**1-2 — Very poor fit:** Barely related to skills. Multiple weak signals.

## Jobs to Score

{jobs_block}

## Instructions
Return ONLY a valid JSON array with one object per job, in the same order. Nothing else:
[{{"id": 1, "score": <0-10>, "reason": "<one sentence>"}}, ...]"""


def build_jobs_block(batch) -> str:
    """Format a batch of jobs for the prompt.

    Each row is a tuple starting (url, company, title, location, desc, ...);
    trailing fields are ignored.
    """
    parts = []
    for i, (url, company, title, location, desc, *_) in enumerate(batch, 1):
        parts.append(
            f"### Job {i}\nTitle: {title}\nCompany: {company}\n"
            f"Location: {location}\nDescription: {desc or ''}\n"
        )
    return "\n".join(parts)


def normalize_row_score(score_data: dict) -> tuple[int, str, int, bool]:
    """Convert one model score object into stored fields:
    (ai_score 0-10, ai_reason<=200 chars, score_pct = ai_score*10, recommended = ai_score>=7).
    """
    ai_score = max(0, min(10, int(score_data.get("score", 5))))
    ai_reason = str(score_data.get("reason", ""))[:200]
    score_pct = ai_score * 10
    recommended = ai_score >= 7
    return ai_score, ai_reason, score_pct, recommended


def eligible_ai_score(ai_score, rescore: bool = False) -> bool:
    """True when a job should be scored. Any non-null ai_score (incl. 0) means
    already judged; skip unless rescore is explicit.
    """
    return rescore or ai_score is None


def is_blocked_staffing_source(row) -> bool:
    """True if a job row (url, company, title, location, desc, ...) is from a
    blocked staffing/spam source — short-circuited to 0 without a model call.
    """
    url, company, title, location, desc, *_ = row
    haystack = " ".join(str(value or "") for value in (url, company, title, location, desc))
    return text_has_blocked_source(haystack)


def parse_scores(text: str):
    """Parse a model's text response into a list of score objects.

    Tolerates markdown fences, stray backticks, and surrounding prose by
    extracting the first JSON array. Returns None on empty/unparseable input.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    text = text.strip("`")
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        text = m.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  Batch error: could not parse model output as JSON: {e}")
        return None
