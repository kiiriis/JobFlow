"""Local AI scoring using Claude CLI (claude -p) with batching.

Usage:
    python scripts/ai_score_local.py

Reads DATABASE_URL from .env, fetches unscored jobs, scores them in batches
of 15 with Claude, and updates the DB directly. ~15 jobs per Claude call
instead of 1 — roughly 10x faster.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from jobflow.db import get_conn, put_conn, init_db

PROFILE = (ROOT / "config" / "profile.txt").read_text().strip()

BATCH_SIZE = 15

BATCH_PROMPT = """You are a job relevance scorer for a new grad / entry-level software engineer on F1 OPT visa looking for their first full-time role in the US.

## Candidate Profile
{profile}

## HARD REJECT — score MUST be 0
Give a score of 0 ONLY if ANY of these are true. Be strict — only these 5 conditions warrant a 0:

1. **Explicit no sponsorship**: The posting explicitly says "no sponsorship", "will not sponsor", "cannot sponsor", "must be authorized to work without sponsorship", "US citizen only", "permanent resident only", "green card required". Also score 0 if it requires security clearance (TS/SCI, Secret, DoD). NOTE: "Must be authorized to work in the US" ALONE is NOT a rejection — OPT holders ARE authorized.

2. **3+ years experience required**: The minimum required experience is 3 or more years. "1-2 years" or "2+ years" is fine. "3+ years" is NOT.

3. **Senior/Staff/Lead role**: Clearly senior-level, staff, principal, architect, VP, director, or management. Must be obvious from title or JD — don't assume.

4. **Not a software engineering role at all**: QA-only, technical writing, product management, sales engineering, IT support. NOTE: Frontend, Full-Stack, iOS, Android, Data Science WITH coding, DevOps WITH development — these ARE software engineering. Score them low (2-4) if poor fit, but NOT 0.

5. **Not US-based**: Located outside the US with no remote-US option.

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


def build_jobs_block(batch):
    """Format a batch of jobs for the prompt. Sends the full JD — Claude's
    200K context easily handles 15 full descriptions per batch."""
    parts = []
    for i, (url, company, title, location, desc, *_) in enumerate(batch, 1):
        parts.append(f"### Job {i}\nTitle: {title}\nCompany: {company}\nLocation: {location}\nDescription: {desc or ''}\n")
    return "\n".join(parts)


def score_batch_with_claude(batch):
    """Score a batch of jobs using a single claude -p call."""
    jobs_block = build_jobs_block(batch)
    prompt = BATCH_PROMPT.format(profile=PROFILE, jobs_block=jobs_block)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        text = result.stdout.strip()
        if not text:
            return None

        # Parse JSON array — handle fences, backticks
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        text = text.strip("`")
        # Extract JSON array
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            text = m.group()

        scores = json.loads(text)
        return scores
    except Exception as e:
        print(f"  Batch error: {e}")
        return None


def main():
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set (check .env)")
        sys.exit(1)

    init_db()

    # Fetch unscored + Groq-scored jobs (Claude rescores Groq, skips already-Claude-scored)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, company, title, location, description_preview, ai_model
                FROM jobs
                WHERE ai_score IS NULL OR ai_model = 'groq'
                ORDER BY first_seen DESC
            """)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    total = len(rows)
    unscored = sum(1 for r in rows if r[5] is None)
    groq_rescore = sum(1 for r in rows if r[5] == 'groq')
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Found {total} jobs to score ({batches} batches of {BATCH_SIZE})")
    print(f"  {unscored} unscored, {groq_rescore} Groq→Claude rescore")
    if not rows:
        print("Nothing to score!")
        return

    scored = 0
    failed = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Batch {batch_num}/{total_batches} ({len(batch)} jobs) ---")

        scores = score_batch_with_claude(batch)
        if not scores or len(scores) != len(batch):
            # Fallback: if batch fails or count mismatch, mark as failed
            for url, company, title, *_ in batch:
                print(f"  SKIP | {company} — {title}")
                failed += 1
            continue

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for (url, company, title, *_), score_data in zip(batch, scores):
                    ai_score = max(0, min(10, int(score_data.get("score", 5))))
                    ai_reason = str(score_data.get("reason", ""))[:200]
                    score_pct = ai_score * 10
                    recommended = ai_score >= 7

                    cur.execute("""
                        UPDATE jobs
                        SET ai_score = %s, ai_reason = %s,
                            score_pct = %s, recommended = %s,
                            ai_model = 'claude'
                        WHERE url = %s
                    """, (ai_score, ai_reason, score_pct, recommended, url))

                    scored += 1
                    tag = "REC" if recommended else f"  {ai_score}"
                    print(f"  {tag} | {company} — {title}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  DB error: {e}")
            failed += len(batch)
        finally:
            put_conn(conn)

    print(f"\nDone: {scored} scored, {failed} failed out of {total} total")


if __name__ == "__main__":
    main()
