"""Local AI scoring using the signed-in Claude or Codex CLI with batching.

Usage:
    python scripts/ai_score_local.py                       # defaults to --engine claude
    python scripts/ai_score_local.py --engine codex
    python scripts/ai_score_local.py --limit 50
    python scripts/ai_score_local.py --hours 12
    python scripts/ai_score_local.py --hours 12 --limit 100
    python scripts/ai_score_local.py --backend json --hours 24
    python scripts/ai_score_local.py --backend json --hours 24 --rescore

Reads DATABASE_URL from .env when available, fetches unscored jobs, scores
them in batches of 15 with the local Claude or Codex CLI session, and updates
Postgres or data/ci/linkedin_jobs.json. This uses the signed-in Claude/Codex
app/CLI account instead of an API key.
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
from jobflow.linkedin_store import save_store
from jobflow.filter import STAFFING_BLOCK_REASON, text_has_blocked_source

PROFILE_PATH = ROOT / "config" / "profile.txt"
PROFILE_EXAMPLE_PATH = ROOT / "config" / "profile.example.txt"
JSON_STORE_PATH = ROOT / "data" / "ci" / "linkedin_jobs.json"

BATCH_SIZE = 15
ENGINES = ("claude", "codex")

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Score recent/unscored JobFlow jobs with the signed-in Claude or Codex CLI.",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="claude",
        help="Which signed-in CLI to score with. Default: claude.",
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
    parser.add_argument(
        "--backend",
        choices=("auto", "db", "json"),
        default="auto",
        help="Storage backend to score. Default: auto tries DB, then JSON.",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Include jobs that already have any AI score. Default only scores unscored rows.",
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


def parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def eligible_ai_score(ai_score, rescore: bool = False) -> bool:
    """Return True when a job should be scored.

    Any non-null AI score, including 0, means the job has already been judged by
    an AI engine and should be skipped unless --rescore is explicit.
    """
    return rescore or ai_score is None


def normalize_row_score(score_data: dict) -> tuple[int, str, int, bool]:
    """Convert model output into stored scoring fields."""
    ai_score = max(0, min(10, int(score_data.get("score", 5))))
    ai_reason = str(score_data.get("reason", ""))[:200]
    score_pct = ai_score * 10
    recommended = ai_score >= 7
    return ai_score, ai_reason, score_pct, recommended


def is_blocked_staffing_source(row):
    """Return True if a DB job row came from a blocked staffing/spam source."""
    url, company, title, location, desc, *_ = row
    haystack = " ".join(str(value or "") for value in (url, company, title, location, desc))
    return text_has_blocked_source(haystack)


def load_profile() -> str:
    """Load the local candidate profile used for AI scoring."""
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text().strip()
    print(f"ERROR: {PROFILE_PATH} not found")
    print(f"Create it with: cp {PROFILE_EXAMPLE_PATH} {PROFILE_PATH}")
    print("Then edit it with your real skills, target roles, visa needs, and preferences.")
    sys.exit(1)


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


def score_batch_with_codex(batch, profile: str):
    """Score a batch of jobs using one signed-in `codex exec` call."""
    jobs_block = build_jobs_block(batch)
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=jobs_block)

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
            text = result.stdout
        return parse_scores(text)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None
    finally:
        if output_path:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass


def score_batch_with_claude(batch, profile: str):
    """Score a batch of jobs using one signed-in `claude -p` (headless) call."""
    jobs_block = build_jobs_block(batch)
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=jobs_block)

    if not shutil.which("claude"):
        print("  Batch error: claude CLI not found on PATH")
        return None

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--output-format", "text",
                "--allowedTools", "",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: claude exited with code {result.returncode}: {err[:500]}")
            return None
        return parse_scores(result.stdout)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None


def score_batch(batch, profile: str, engine: str):
    """Dispatch a batch to the selected scoring engine."""
    if engine == "codex":
        return score_batch_with_codex(batch, profile)
    return score_batch_with_claude(batch, profile)


def connect_db():
    """Initialize and return True when Postgres is reachable."""
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        init_db()
        return True
    except Exception as e:
        print(f"DB unavailable, using JSON fallback: {e}")
        return False


def fetch_db_rows(args):
    """Fetch eligible rows from Postgres."""
    conditions = []
    params = []
    if not args.rescore:
        conditions.append("ai_score IS NULL")
    if args.hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        conditions.append("first_seen >= %s")
        params.append(since)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT url, company, title, location, description_preview, ai_model
        FROM jobs
        {where_clause}
        ORDER BY first_seen DESC
    """
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        put_conn(conn)


def load_json_store() -> dict:
    """Load the JSON fallback store."""
    if not JSON_STORE_PATH.exists():
        print(f"ERROR: {JSON_STORE_PATH} not found")
        sys.exit(1)
    try:
        return json.loads(JSON_STORE_PATH.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse {JSON_STORE_PATH}: {e}")
        sys.exit(1)


def fetch_json_rows(store: dict, args):
    """Fetch eligible rows from data/ci/linkedin_jobs.json."""
    rows = []
    cutoff = None
    if args.hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    for key, job in store.get("jobs", {}).items():
        ai_score = job.get("ai_score")
        ai_model = job.get("ai_model")
        if not eligible_ai_score(ai_score, args.rescore):
            continue
        if cutoff:
            first_seen = parse_iso(job.get("first_seen", ""))
            if not first_seen or first_seen < cutoff:
                continue
        url = job.get("url") or key
        rows.append((
            url,
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("description_preview", ""),
            ai_model,
            key,
        ))

    rows.sort(
        key=lambda row: parse_iso(store.get("jobs", {}).get(row[6], {}).get("first_seen", ""))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if args.limit:
        rows = rows[:args.limit]
    return rows


def update_db_blocked(blocked_rows, engine):
    """Persist blocked-source scores to Postgres."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for url, *_ in blocked_rows:
                cur.execute("""
                    UPDATE jobs
                    SET ai_score = 0, ai_reason = %s,
                        score_pct = 0, recommended = false,
                        ai_model = %s
                    WHERE url = %s
                """, (STAFFING_BLOCK_REASON, engine, url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_db_scores(scored_batch, scores, engine):
    """Persist engine scores to Postgres."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for (url, *_), score_data in zip(scored_batch, scores):
                ai_score, ai_reason, score_pct, recommended = normalize_row_score(score_data)
                cur.execute("""
                    UPDATE jobs
                    SET ai_score = %s, ai_reason = %s,
                        score_pct = %s, recommended = %s,
                        ai_model = %s
                    WHERE url = %s
                """, (ai_score, ai_reason, score_pct, recommended, engine, url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_json_blocked(store: dict, blocked_rows, engine):
    """Persist blocked-source scores to the JSON store in memory."""
    jobs = store.get("jobs", {})
    for url, *rest in blocked_rows:
        key = rest[5] if len(rest) > 5 else url
        job = jobs.get(key)
        if not job:
            continue
        job["ai_score"] = 0
        job["ai_reason"] = STAFFING_BLOCK_REASON
        job["score_pct"] = 0
        job["recommended"] = False
        job["ai_model"] = engine


def update_json_scores(store: dict, scored_batch, scores, engine):
    """Persist engine scores to the JSON store in memory."""
    jobs = store.get("jobs", {})
    for (url, *rest), score_data in zip(scored_batch, scores):
        key = rest[5] if len(rest) > 5 else url
        job = jobs.get(key)
        if not job:
            continue
        ai_score, ai_reason, score_pct, recommended = normalize_row_score(score_data)
        job["ai_score"] = ai_score
        job["ai_reason"] = ai_reason
        job["score_pct"] = score_pct
        job["recommended"] = recommended
        job["ai_model"] = engine


def print_summary(rows, args, backend: str):
    """Print scoring summary for either backend."""
    total = len(rows)
    unscored = sum(1 for r in rows if r[5] is None)
    rescored = total - unscored
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Engine: {args.engine}")
    print(f"Backend: {backend}")
    print(f"Found {total} jobs to score ({batches} batches of {BATCH_SIZE})")
    if args.hours:
        print(f"  Window: first_seen in past {args.hours:g} hours")
    if args.limit:
        print(f"  Limit: latest {args.limit} eligible jobs")
    if args.rescore:
        print(f"  {unscored} unscored, {rescored} already scored")
    else:
        print(f"  {unscored} unscored")


def save_json_store(store: dict):
    """Write the JSON store using the same atomic helper as the dashboard."""
    save_store(JSON_STORE_PATH, store)


def main():
    args = parse_args()
    profile = load_profile()

    store = None
    if args.backend == "json":
        backend = "json"
        store = load_json_store()
        rows = fetch_json_rows(store, args)
    elif args.backend == "db":
        if not connect_db():
            print("ERROR: DB backend requested, but DATABASE_URL is missing or unavailable.")
            sys.exit(1)
        backend = "db"
        rows = fetch_db_rows(args)
    else:
        if connect_db():
            backend = "db"
            rows = fetch_db_rows(args)
        else:
            backend = "json"
            store = load_json_store()
            rows = fetch_json_rows(store, args)

    total = len(rows)
    print_summary(rows, args, backend)
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
        scorable_batch = []
        for row in batch:
            if is_blocked_staffing_source(row):
                blocked_rows.append(row)
            else:
                scorable_batch.append(row)

        if blocked_rows:
            try:
                if backend == "db":
                    update_db_blocked(blocked_rows, args.engine)
                else:
                    update_json_blocked(store, blocked_rows, args.engine)
                for _, company, title, *_ in blocked_rows:
                    scored += 1
                    print(f"    0 | {company} — {title} ({STAFFING_BLOCK_REASON})")
            except Exception as e:
                print(f"  {backend.upper()} error: {e}")
                failed += len(blocked_rows)

        if not scorable_batch:
            continue

        scores = score_batch(scorable_batch, profile, args.engine)
        if not scores or len(scores) != len(scorable_batch):
            # Fallback: if batch fails or count mismatch, mark as failed
            for url, company, title, *_ in scorable_batch:
                print(f"  SKIP | {company} — {title}")
                failed += 1
            continue

        try:
            if backend == "db":
                update_db_scores(scorable_batch, scores, args.engine)
            else:
                update_json_scores(store, scorable_batch, scores, args.engine)
            for (_, company, title, *_), score_data in zip(scorable_batch, scores):
                ai_score, _, _, recommended = normalize_row_score(score_data)
                scored += 1
                tag = "REC" if recommended else f"  {ai_score}"
                print(f"  {tag} | {company} — {title}")
        except Exception as e:
            print(f"  {backend.upper()} error: {e}")
            failed += len(scorable_batch)

    if backend == "json":
        save_json_store(store)
        print(f"Saved JSON scores to {JSON_STORE_PATH}")

    print(f"\nDone: {scored} scored, {failed} failed out of {total} total")


if __name__ == "__main__":
    main()
