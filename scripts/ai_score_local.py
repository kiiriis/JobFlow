"""Local AI scoring using signed-in Codex CLI with batching.

Usage:
    python scripts/ai_score_local.py
    python scripts/ai_score_local.py --limit 50
    python scripts/ai_score_local.py --hours 12
    python scripts/ai_score_local.py --hours 12 --limit 100

Reads DATABASE_URL from .env, fetches unscored jobs, scores them in batches
of 15 with the local Codex CLI session, and updates the DB directly. This uses
the signed-in Codex app/CLI account instead of an API key.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
STAFFING_BLOCK_REASON = "Blocked staffing/spam source."
STAFFING_SOURCE_BLOCKLIST = (
    "jobright.ai",
    "remotehunter",
    "quik hire staffing",
    "beacon fire",
    "helic & co.",
    "helic and co",
    "jobs via dice",
)
STAFFING_SOURCE_BLOCKLIST_COMPACT = tuple(
    re.sub(r"[^a-z0-9]+", "", source.lower())
    for source in STAFFING_SOURCE_BLOCKLIST
)

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

6. **Blocked staffing/spam source**: The job is from jobright.ai, Remotehunter, Quik Hire Staffing, Beacon Fire, Helic & Co., or Jobs Via Dice.

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Score recent/unscored JobFlow DB jobs with the signed-in Codex CLI.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=0,
        help="Score only the latest N eligible jobs by first_seen. Default: no limit.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=0,
        help="Score only eligible jobs first seen in the past H hours. Example: --hours 12.",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be 0 or greater")
    if args.hours < 0:
        parser.error("--hours must be 0 or greater")
    return args


def build_jobs_block(batch):
    """Format a batch of jobs for the prompt."""
    parts = []
    for i, (url, company, title, location, desc, *_) in enumerate(batch, 1):
        parts.append(f"### Job {i}\nTitle: {title}\nCompany: {company}\nLocation: {location}\nDescription: {desc or ''}\n")
    return "\n".join(parts)


def is_blocked_staffing_source(row):
    """Return True if a DB job row came from a blocked staffing/spam source."""
    url, company, title, location, desc, *_ = row
    haystack = " ".join(
        str(value or "")
        for value in (url, company, title, location, desc)
    ).lower()
    compact_haystack = re.sub(r"[^a-z0-9]+", "", haystack)
    return any(source in haystack for source in STAFFING_SOURCE_BLOCKLIST) or any(
        source in compact_haystack for source in STAFFING_SOURCE_BLOCKLIST_COMPACT
    )


def score_batch_with_codex(batch):
    """Score a batch of jobs using one signed-in `codex exec` call."""
    jobs_block = build_jobs_block(batch)
    prompt = BATCH_PROMPT.format(profile=PROFILE, jobs_block=jobs_block)

    if not shutil.which("codex"):
        print("  Batch error: codex CLI not found on PATH")
        return None

    output_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="jobflow_codex_score_",
            suffix=".txt",
            delete=False,
        ) as tmp:
            output_path = Path(tmp.name)

        result = subprocess.run(
            [
                "codex", "exec",
                "--cd", str(ROOT),
                "--sandbox", "read-only",
                "--ignore-rules",
                "--output-last-message", str(output_path),
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: codex exited with code {result.returncode}: {err[:500]}")
            return None

        text = ""
        if output_path and output_path.exists():
            text = output_path.read_text(errors="replace").strip()
        if not text:
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
    finally:
        if output_path:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    args = parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set (check .env)")
        sys.exit(1)

    init_db()

    # Fetch unscored + Groq/Claude-scored jobs. Already-Codex-scored rows are
    # skipped so repeated runs only process new or older-scored jobs.
    conditions = ["(ai_score IS NULL OR ai_model IN ('groq', 'claude'))"]
    params = []
    if args.hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        conditions.append("first_seen >= %s")
        params.append(since)

    sql = f"""
        SELECT url, company, title, location, description_preview, ai_model
        FROM jobs
        WHERE {' AND '.join(conditions)}
        ORDER BY first_seen DESC
    """
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    total = len(rows)
    unscored = sum(1 for r in rows if r[5] is None)
    groq_rescore = sum(1 for r in rows if r[5] == 'groq')
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Found {total} jobs to score ({batches} batches of {BATCH_SIZE})")
    if args.hours:
        print(f"  Window: first_seen in past {args.hours:g} hours")
    if args.limit:
        print(f"  Limit: latest {args.limit} eligible jobs")
    claude_rescore = sum(1 for r in rows if r[5] == 'claude')
    print(f"  {unscored} unscored, {groq_rescore} Groq→Codex rescore, {claude_rescore} Claude→Codex rescore")
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

        blocked_rows = []
        codex_batch = []
        for row in batch:
            if is_blocked_staffing_source(row):
                blocked_rows.append(row)
            else:
                codex_batch.append(row)

        if blocked_rows:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    for url, company, title, *_ in blocked_rows:
                        cur.execute("""
                            UPDATE jobs
                            SET ai_score = 0, ai_reason = %s,
                                score_pct = 0, recommended = false,
                                ai_model = 'codex'
                            WHERE url = %s
                        """, (STAFFING_BLOCK_REASON, url))
                        scored += 1
                        print(f"    0 | {company} — {title} ({STAFFING_BLOCK_REASON})")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  DB error: {e}")
                failed += len(blocked_rows)
            finally:
                put_conn(conn)

        if not codex_batch:
            continue

        scores = score_batch_with_codex(codex_batch)
        if not scores or len(scores) != len(codex_batch):
            # Fallback: if batch fails or count mismatch, mark as failed
            for url, company, title, *_ in codex_batch:
                print(f"  SKIP | {company} — {title}")
                failed += 1
            continue

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for (url, company, title, *_), score_data in zip(codex_batch, scores):
                    ai_score = max(0, min(10, int(score_data.get("score", 5))))
                    ai_reason = str(score_data.get("reason", ""))[:200]
                    score_pct = ai_score * 10
                    recommended = ai_score >= 7

                    cur.execute("""
                        UPDATE jobs
                        SET ai_score = %s, ai_reason = %s,
                            score_pct = %s, recommended = %s,
                            ai_model = 'codex'
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
