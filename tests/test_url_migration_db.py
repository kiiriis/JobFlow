"""Simulate normalize_existing_urls() against an in-memory fake cursor.

We can't reach Neon from tests, but the function is mostly plumbing around
_merge_job_rows() (covered in test_url_normalization.py). What this file
*does* verify end-to-end:

  - Which SQL statements are issued, in what order
  - That every row in each table ends up under the canonical URL
  - That collisions collapse correctly and the surviving row carries the
    right user status / AI score / timestamps
  - That already-canonical rows are skipped (no UPDATE/INSERT issued)
  - Counters in the return value are accurate
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from jobflow import db as jdb
from jobflow.db import JOB_COLUMNS


class FakeCursor:
    """Minimal in-memory Postgres substitute for the tables we touch."""

    def __init__(self, jobs, seen, dismissed):
        self.jobs = {row["url"]: dict(row) for row in jobs}
        self.seen = {row["url"]: row["seen_at"] for row in seen}
        self.dismissed = {row["url"]: row["dismissed_at"] for row in dismissed}
        self._last_result: list = []
        self.sql_log: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        sql_norm = " ".join(sql.split())
        self.sql_log.append((sql_norm, params))
        s = sql_norm.upper()

        if s.startswith("SELECT") and "FROM JOBS" in s:
            self._last_result = [
                tuple(row.get(c) for c in JOB_COLUMNS)
                for row in self.jobs.values()
            ]
        elif s.startswith("SELECT URL, SEEN_AT FROM SEEN_JOBS"):
            self._last_result = [(u, t) for u, t in self.seen.items()]
        elif s.startswith("SELECT URL, DISMISSED_AT FROM DISMISSED_JOBS"):
            self._last_result = [(u, t) for u, t in self.dismissed.items()]
        elif s.startswith("DELETE FROM JOBS WHERE URL = ANY"):
            for u in params[0]:
                self.jobs.pop(u, None)
        elif s.startswith("DELETE FROM SEEN_JOBS WHERE URL = ANY"):
            for u in params[0]:
                self.seen.pop(u, None)
        elif s.startswith("DELETE FROM DISMISSED_JOBS WHERE URL = ANY"):
            for u in params[0]:
                self.dismissed.pop(u, None)
        elif s.startswith("INSERT INTO JOBS"):
            row = dict(zip(JOB_COLUMNS, params))
            # Simulate ON CONFLICT DO NOTHING
            self.jobs.setdefault(row["url"], row)
        elif s.startswith("INSERT INTO SEEN_JOBS"):
            # ON CONFLICT DO UPDATE: always take latest param
            self.seen[params[0]] = params[1]
        elif s.startswith("INSERT INTO DISMISSED_JOBS"):
            # ON CONFLICT DO NOTHING
            self.dismissed.setdefault(params[0], params[1])
        else:
            raise AssertionError(f"Unexpected SQL: {sql_norm[:200]}")

    def fetchall(self):
        return self._last_result

    # Context manager plumbing used via `with conn.cursor() as cur:`
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cur = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _row(url, **kw):
    """Build a jobs row with sane defaults for every JOB_COLUMN."""
    base = {c: None for c in JOB_COLUMNS}
    base.update({
        "url": url, "company": "", "title": "", "location": "",
        "description_preview": "", "search_term": "", "date_posted": "",
        "variant": "se", "reason": "",
        "first_seen": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "score": 0, "score_pct": 0, "ai_score": None, "ai_reason": "",
        "ai_model": None, "recommended": False, "level": "Unknown",
        "min_exp": None, "max_exp": None, "competition": 0, "keyword_hits": 0,
        "status": "", "h1b": False, "reject_reason": None,
        "expires_at": None, "source": "linkedin",
    })
    base.update(kw)
    return base


@pytest.fixture
def run_migration():
    """Install a fake conn/cursor and run normalize_existing_urls()."""
    def _run(jobs, seen=None, dismissed=None):
        cur = FakeCursor(jobs=jobs, seen=seen or [], dismissed=dismissed or [])
        conn = FakeConn(cur)
        with patch.object(jdb, "get_conn", return_value=conn), \
             patch.object(jdb, "put_conn", lambda c: None):
            result = jdb.normalize_existing_urls()
        return result, cur, conn
    return _run


# ----------------------------------------------------------------------------
# jobs table
# ----------------------------------------------------------------------------

class TestJobsMigration:
    def test_already_canonical_row_is_left_alone(self, run_migration):
        rows = [_row("https://linkedin.com/jobs/view/1", status="Applied")]
        result, cur, conn = run_migration(rows)
        assert result == {"jobs_merged": 0, "jobs_rekeyed": 0,
                          "seen_rekeyed": 0, "dismissed_rekeyed": 0}
        # No INSERT/DELETE for jobs should have been issued
        jobs_sql = [s for s, _ in cur.sql_log if "JOBS" in s.upper() and "SELECT" not in s.upper()]
        assert jobs_sql == []
        assert conn.committed is True

    def test_single_row_with_tracking_is_rekeyed(self, run_migration):
        rows = [_row("https://linkedin.com/jobs/view/1?trk=foo", status="Applied")]
        result, cur, _ = run_migration(rows)
        assert result["jobs_rekeyed"] == 1
        assert result["jobs_merged"] == 0
        assert "https://linkedin.com/jobs/view/1" in cur.jobs
        assert "https://linkedin.com/jobs/view/1?trk=foo" not in cur.jobs
        assert cur.jobs["https://linkedin.com/jobs/view/1"]["status"] == "Applied"

    def test_duplicate_urls_merge_into_one(self, run_migration):
        rows = [
            _row("https://linkedin.com/jobs/view/1?trk=a", status=""),
            _row("https://linkedin.com/jobs/view/1?trk=b", status="Applied"),
            _row("https://linkedin.com/jobs/view/1?trk=c", status=""),
        ]
        result, cur, _ = run_migration(rows)
        assert result["jobs_merged"] == 1
        assert result["jobs_rekeyed"] == 0
        assert list(cur.jobs.keys()) == ["https://linkedin.com/jobs/view/1"]
        # Applied-status row wins
        assert cur.jobs["https://linkedin.com/jobs/view/1"]["status"] == "Applied"

    def test_merge_carries_ai_score_from_loser(self, run_migration):
        rows = [
            _row("https://x.com/1?a=1", status="Applied", ai_score=None),
            _row("https://x.com/1?a=2", status="", ai_score=8,
                 ai_reason="ML fit", ai_model="gpt-4o-mini"),
        ]
        result, cur, _ = run_migration(rows)
        assert result["jobs_merged"] == 1
        merged = cur.jobs["https://x.com/1"]
        assert merged["status"] == "Applied"
        assert merged["ai_score"] == 8
        assert merged["ai_reason"] == "ML fit"
        assert merged["ai_model"] == "gpt-4o-mini"

    def test_mixed_bag_counts(self, run_migration):
        """One already-canonical, one tracking-only rekey, one merge of three."""
        rows = [
            # Already canonical — should NOT be touched
            _row("https://linkedin.com/jobs/view/100", status="Applied"),
            # Single tracking-laden row — rekey only
            _row("https://linkedin.com/jobs/view/200?trk=x", status=""),
            # Three-way collision at /300
            _row("https://linkedin.com/jobs/view/300?a=1"),
            _row("https://linkedin.com/jobs/view/300?a=2", status="Applied"),
            _row("https://linkedin.com/jobs/view/300?a=3"),
        ]
        result, cur, _ = run_migration(rows)
        assert result["jobs_merged"] == 1
        assert result["jobs_rekeyed"] == 1
        assert set(cur.jobs.keys()) == {
            "https://linkedin.com/jobs/view/100",
            "https://linkedin.com/jobs/view/200",
            "https://linkedin.com/jobs/view/300",
        }
        assert cur.jobs["https://linkedin.com/jobs/view/300"]["status"] == "Applied"

    def test_empty_url_row_is_skipped_not_crashed(self, run_migration):
        rows = [
            _row(""),  # blank URL — should be silently skipped
            _row("https://x.com/1?trk=foo"),
        ]
        result, cur, _ = run_migration(rows)
        assert result["jobs_rekeyed"] == 1
        assert "https://x.com/1" in cur.jobs

    def test_idempotent_second_run_is_noop(self, run_migration):
        rows = [_row("https://linkedin.com/jobs/view/1?trk=foo", status="Applied")]
        # First run
        result1, cur, conn = run_migration(rows)
        assert result1["jobs_rekeyed"] == 1
        # Second run: feed the already-migrated state
        post_state = [dict(r) for r in cur.jobs.values()]
        result2, cur2, _ = run_migration(post_state)
        assert result2 == {"jobs_merged": 0, "jobs_rekeyed": 0,
                           "seen_rekeyed": 0, "dismissed_rekeyed": 0}


# ----------------------------------------------------------------------------
# seen_jobs table
# ----------------------------------------------------------------------------

class TestSeenJobsMigration:
    def test_already_canonical_seen_row_left_alone(self, run_migration):
        seen = [{"url": "https://linkedin.com/jobs/view/1",
                 "seen_at": datetime(2026, 4, 1, tzinfo=timezone.utc)}]
        result, cur, _ = run_migration([], seen=seen)
        assert result["seen_rekeyed"] == 0
        assert list(cur.seen.keys()) == ["https://linkedin.com/jobs/view/1"]

    def test_tracking_param_seen_url_is_rekeyed(self, run_migration):
        seen = [{"url": "https://linkedin.com/jobs/view/1?trk=x",
                 "seen_at": datetime(2026, 4, 1, tzinfo=timezone.utc)}]
        result, cur, _ = run_migration([], seen=seen)
        assert result["seen_rekeyed"] == 1
        assert list(cur.seen.keys()) == ["https://linkedin.com/jobs/view/1"]

    def test_duplicate_seen_urls_keep_latest_timestamp(self, run_migration):
        early = datetime(2026, 1, 1, tzinfo=timezone.utc)
        late = datetime(2026, 4, 1, tzinfo=timezone.utc)
        seen = [
            {"url": "https://x.com/1?a=1", "seen_at": early},
            {"url": "https://x.com/1?a=2", "seen_at": late},
        ]
        result, cur, _ = run_migration([], seen=seen)
        assert result["seen_rekeyed"] == 1
        assert cur.seen["https://x.com/1"] == late


# ----------------------------------------------------------------------------
# dismissed_jobs table
# ----------------------------------------------------------------------------

class TestDismissedJobsMigration:
    def test_tracking_param_dismissed_url_is_rekeyed(self, run_migration):
        dismissed = [{"url": "https://linkedin.com/jobs/view/1?trk=x",
                      "dismissed_at": datetime(2026, 4, 1, tzinfo=timezone.utc)}]
        result, cur, _ = run_migration([], dismissed=dismissed)
        assert result["dismissed_rekeyed"] == 1
        assert list(cur.dismissed.keys()) == ["https://linkedin.com/jobs/view/1"]

    def test_duplicate_dismissed_keeps_earliest(self, run_migration):
        """First dismissal wins — a user dismissing the same job twice shouldn't
        reset the dismissal timestamp."""
        early = datetime(2026, 1, 1, tzinfo=timezone.utc)
        late = datetime(2026, 4, 1, tzinfo=timezone.utc)
        dismissed = [
            {"url": "https://x.com/1?a=1", "dismissed_at": late},
            {"url": "https://x.com/1?a=2", "dismissed_at": early},
        ]
        result, cur, _ = run_migration([], dismissed=dismissed)
        assert cur.dismissed["https://x.com/1"] == early


# ----------------------------------------------------------------------------
# Transaction / error handling
# ----------------------------------------------------------------------------

class TestTransactionHandling:
    def test_commit_on_success(self, run_migration):
        _, _, conn = run_migration([_row("https://x.com/1?trk=a")])
        assert conn.committed is True
        assert conn.rolled_back is False

    def test_rollback_on_error(self):
        """If a SQL call blows up mid-migration, the commit must not fire
        and rollback is called — so we don't leave the DB half-migrated."""
        class ExplodingCursor(FakeCursor):
            def execute(self, sql, params=()):
                if "DELETE FROM JOBS" in sql.upper():
                    raise RuntimeError("simulated DB failure")
                return super().execute(sql, params)

        cur = ExplodingCursor(
            jobs=[_row("https://x.com/1?trk=a", status="Applied")],
            seen=[], dismissed=[],
        )
        conn = FakeConn(cur)
        with patch.object(jdb, "get_conn", return_value=conn), \
             patch.object(jdb, "put_conn", lambda c: None):
            with pytest.raises(RuntimeError, match="simulated"):
                jdb.normalize_existing_urls()
        assert conn.committed is False
        assert conn.rolled_back is True
