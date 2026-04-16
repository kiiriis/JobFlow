"""One-off script to prune old jobs and AI-score the remaining ones.

Usage:
    DATABASE_URL=... GROQ_API_KEY=... python scripts/backfill_ai_scores.py

Steps:
    1. Delete all jobs from before today (midnight UTC)
    2. AI-score remaining (today's) unscored jobs via Groq
"""

import os
import sys
import time
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobflow.db import get_conn, put_conn
from jobflow.ai_scorer import _get_client, _load_profile, score_single_job


def main():
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY not set")
        sys.exit(1)

    client = _get_client()
    if not client:
        print("ERROR: Could not create Groq client")
        sys.exit(1)

    profile = _load_profile()
    if not profile:
        print("ERROR: config/profile.txt not found")
        sys.exit(1)

    # --- Step 1: Prune jobs from before today ---
    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            total_before = cur.fetchone()[0]

            cur.execute(
                "DELETE FROM jobs WHERE first_seen < %s",
                (today_midnight,),
            )
            deleted = cur.rowcount

            cur.execute("SELECT COUNT(*) FROM jobs")
            remaining = cur.fetchone()[0]

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    print(f"Pruned {deleted} old jobs (had {total_before}, kept {remaining} from today)")

    if remaining == 0:
        print("No jobs left to score")
        return

    # --- Step 2: AI-score unscored jobs ---
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, company, title, location, description_preview
                FROM jobs
                WHERE ai_score IS NULL
                ORDER BY first_seen DESC
            """)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    print(f"Scoring {len(rows)} unscored jobs (~{len(rows) * 2.1:.0f}s at 2.1s each)\n")
    if not rows:
        print("All jobs already scored!")
        return

    scored = 0
    failed = 0

    for i, (url, company, title, location, desc) in enumerate(rows, 1):
        job = {
            "url": url,
            "company": company,
            "title": title,
            "location": location,
            "description_preview": desc,
        }

        result = score_single_job(client, profile, job)
        if result:
            ai_score = result["ai_score"]
            ai_reason = result["ai_reason"]
            score_pct = ai_score * 10
            recommended = ai_score >= 7

            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE jobs
                        SET ai_score = %s,
                            ai_reason = %s,
                            score_pct = %s,
                            recommended = %s
                        WHERE url = %s
                    """, (ai_score, ai_reason, score_pct, recommended, url))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                put_conn(conn)

            scored += 1
            tag = "REC" if recommended else f"  {ai_score}"
            print(f"[{i}/{len(rows)}] {tag} | {company} — {title}")
        else:
            failed += 1
            print(f"[{i}/{len(rows)}] SKIP | {company} — {title}")

        # Rate limit: Groq free tier = 30 RPM
        time.sleep(2.1)

    print(f"\nDone: {scored} scored, {failed} failed/skipped out of {len(rows)} total")


if __name__ == "__main__":
    main()
