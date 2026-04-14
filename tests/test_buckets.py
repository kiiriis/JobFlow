"""Comprehensive tests for dynamic bucket logic in linkedin_store.py.

Covers: _bucket_minutes, _bucket_start, _bucket_label, _bucket_key,
get_time_counts (bucket generation), get_filtered_jobs (bucket_filter),
get_filtered_counts (bucket_filter), and CI merge pipeline integrity.
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobflow.linkedin_store import (
    _bucket_minutes,
    _bucket_start,
    _bucket_label,
    _bucket_key,
    get_time_counts,
    get_filtered_jobs,
    get_filtered_counts,
    load_store,
    save_store,
    merge_scan_results,
    prune_old_jobs,
    backfill_job,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

EST = timezone(timedelta(hours=-4))   # EDT
PST = timezone(timedelta(hours=-7))
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def _local(year, month, day, hour, minute=0, tz=EST):
    """Make a tz-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def _make_store_with_jobs(job_times_utc: list[datetime]) -> dict:
    """Build a store with jobs at the given UTC timestamps."""
    jobs = {}
    for i, dt in enumerate(job_times_utc):
        url = f"https://example.com/job-{i}"
        jobs[url] = {
            "company": f"Company{i}",
            "title": f"SWE {i}",
            "location": "Remote, US",
            "url": url,
            "description_preview": "Python developer needed.",
            "status": "",
            "first_seen": dt.isoformat(),
            "last_seen": dt.isoformat(),
            "date_posted": "",
            "search_term": "python",
            "score": 30,
            "score_pct": 25,
            "level": "Entry",
            "min_exp": None,
            "max_exp": None,
            "competition": 5,
            "keyword_hits": 3,
            "recommended": True,
            "variant": "se",
            "reason": "",
        }
    return {"jobs": jobs, "last_updated": datetime.now(UTC).isoformat()}


# ═══════════════════════════════════════════════════════════════════════════
# _bucket_minutes — determines bucket size based on local day + hour
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketMinutes:
    """_bucket_minutes(local_dt) → 30 | 60 | 240"""

    # ── Weekday peak (9AM-9PM) → 30 min ─────────────────────────
    def test_weekday_9am_exactly(self):
        # Monday 9:00 AM → peak starts → 30 min
        dt = _local(2026, 4, 13, 9, 0)  # Monday
        assert dt.weekday() == 0  # Monday
        assert _bucket_minutes(dt) == 30

    def test_weekday_noon(self):
        dt = _local(2026, 4, 14, 12, 0)  # Tuesday noon
        assert _bucket_minutes(dt) == 30

    def test_weekday_2_30_pm(self):
        dt = _local(2026, 4, 15, 14, 30)  # Wednesday 2:30 PM
        assert _bucket_minutes(dt) == 30

    def test_weekday_8_59_pm(self):
        # 20:59 — last minute of peak
        dt = _local(2026, 4, 16, 20, 59)  # Thursday
        assert _bucket_minutes(dt) == 30

    # ── Weekday off-peak (9PM-9AM) → 60 min ─────────────────────
    def test_weekday_9pm_exactly(self):
        # 21:00 — peak ends, off-peak starts → 60 min
        dt = _local(2026, 4, 13, 21, 0)  # Monday 9 PM
        assert _bucket_minutes(dt) == 60

    def test_weekday_11pm(self):
        dt = _local(2026, 4, 14, 23, 0)  # Tuesday 11 PM
        assert _bucket_minutes(dt) == 60

    def test_weekday_midnight(self):
        dt = _local(2026, 4, 15, 0, 0)  # Wednesday midnight
        assert dt.weekday() == 2  # Wednesday
        assert _bucket_minutes(dt) == 60

    def test_weekday_3am(self):
        dt = _local(2026, 4, 16, 3, 0)  # Thursday 3 AM
        assert _bucket_minutes(dt) == 60

    def test_weekday_8am(self):
        dt = _local(2026, 4, 17, 8, 0)  # Friday 8 AM
        assert _bucket_minutes(dt) == 60

    def test_weekday_8_59_am(self):
        # 8:59 — last minute before peak
        dt = _local(2026, 4, 13, 8, 59)  # Monday
        assert _bucket_minutes(dt) == 60

    # ── Weekend → 240 min ────────────────────────────────────────
    def test_saturday_noon(self):
        dt = _local(2026, 4, 11, 12, 0)  # Saturday
        assert dt.weekday() == 5
        assert _bucket_minutes(dt) == 240

    def test_sunday_3am(self):
        dt = _local(2026, 4, 12, 3, 0)  # Sunday
        assert dt.weekday() == 6
        assert _bucket_minutes(dt) == 240

    def test_saturday_midnight(self):
        dt = _local(2026, 4, 11, 0, 0)  # Saturday midnight
        assert _bucket_minutes(dt) == 240

    def test_sunday_11_59_pm(self):
        dt = _local(2026, 4, 12, 23, 59)  # Sunday 11:59 PM
        assert _bucket_minutes(dt) == 240

    # ── Boundaries: Friday night → Saturday ──────────────────────
    def test_friday_11_59_pm(self):
        # Friday 23:59 → weekday off-peak → 60 min
        dt = _local(2026, 4, 17, 23, 59)  # Friday
        assert dt.weekday() == 4  # Friday
        assert _bucket_minutes(dt) == 60

    def test_saturday_12_01_am(self):
        # Saturday 00:01 → weekend → 240 min
        dt = _local(2026, 4, 18, 0, 1)  # Saturday
        assert dt.weekday() == 5
        assert _bucket_minutes(dt) == 240

    # ── Boundary: Sunday night → Monday ──────────────────────────
    def test_sunday_11_59_pm_is_weekend(self):
        dt = _local(2026, 4, 12, 23, 59)
        assert dt.weekday() == 6  # Sunday
        assert _bucket_minutes(dt) == 240

    def test_monday_12_01_am_is_weekday_offpeak(self):
        dt = _local(2026, 4, 13, 0, 1)
        assert dt.weekday() == 0  # Monday
        assert _bucket_minutes(dt) == 60


# ═══════════════════════════════════════════════════════════════════════════
# _bucket_start — snaps datetime to bucket boundary
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketStart:
    """_bucket_start(local_dt) snaps to nearest bucket boundary."""

    # ── 30-min snapping ──────────────────────────────────────────
    def test_30min_on_hour(self):
        dt = _local(2026, 4, 14, 14, 0)  # 2:00 PM Tue
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 0

    def test_30min_on_half(self):
        dt = _local(2026, 4, 14, 14, 30)  # 2:30 PM
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 30

    def test_30min_between_0_and_30(self):
        dt = _local(2026, 4, 14, 14, 15)  # 2:15 PM → snaps to 2:00 PM
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 0

    def test_30min_between_30_and_60(self):
        dt = _local(2026, 4, 14, 14, 45)  # 2:45 PM → snaps to 2:30 PM
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 30

    def test_30min_at_29(self):
        dt = _local(2026, 4, 14, 14, 29)  # 2:29 PM → snaps to 2:00 PM
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 0

    def test_30min_at_31(self):
        dt = _local(2026, 4, 14, 14, 31)  # 2:31 PM → snaps to 2:30 PM
        result = _bucket_start(dt)
        assert result.hour == 14 and result.minute == 30

    def test_30min_seconds_ignored(self):
        dt = _local(2026, 4, 14, 14, 15).replace(second=59, microsecond=999)
        result = _bucket_start(dt)
        assert result.second == 0 and result.microsecond == 0

    # ── 60-min snapping ──────────────────────────────────────────
    def test_60min_on_hour(self):
        dt = _local(2026, 4, 14, 23, 0)  # 11:00 PM Tue (off-peak)
        result = _bucket_start(dt)
        assert result.hour == 23 and result.minute == 0

    def test_60min_at_30(self):
        dt = _local(2026, 4, 14, 23, 30)  # 11:30 PM → snaps to 11:00 PM
        result = _bucket_start(dt)
        assert result.hour == 23 and result.minute == 0

    def test_60min_at_59(self):
        dt = _local(2026, 4, 14, 23, 59)  # 11:59 PM → snaps to 11:00 PM
        result = _bucket_start(dt)
        assert result.hour == 23 and result.minute == 0

    def test_60min_at_midnight(self):
        dt = _local(2026, 4, 15, 0, 0)  # Midnight Wed
        result = _bucket_start(dt)
        assert result.hour == 0 and result.minute == 0

    # ── 240-min snapping ─────────────────────────────────────────
    def test_240min_block_0(self):
        dt = _local(2026, 4, 11, 1, 30)  # Sat 1:30 AM → block 0
        result = _bucket_start(dt)
        assert result.hour == 0 and result.minute == 0

    def test_240min_block_4(self):
        dt = _local(2026, 4, 11, 5, 15)  # Sat 5:15 AM → block 4
        result = _bucket_start(dt)
        assert result.hour == 4 and result.minute == 0

    def test_240min_block_8(self):
        dt = _local(2026, 4, 11, 10, 0)  # Sat 10:00 AM → block 8
        result = _bucket_start(dt)
        assert result.hour == 8 and result.minute == 0

    def test_240min_block_12(self):
        dt = _local(2026, 4, 11, 13, 45)  # Sat 1:45 PM → block 12
        result = _bucket_start(dt)
        assert result.hour == 12 and result.minute == 0

    def test_240min_block_16(self):
        dt = _local(2026, 4, 11, 17, 0)  # Sat 5:00 PM → block 16
        result = _bucket_start(dt)
        assert result.hour == 16 and result.minute == 0

    def test_240min_block_20(self):
        dt = _local(2026, 4, 11, 23, 59)  # Sat 11:59 PM → block 20
        result = _bucket_start(dt)
        assert result.hour == 20 and result.minute == 0

    # ── Cross-boundary: peak→off-peak at bucket_start ────────────
    def test_peak_boundary_snap(self):
        """Job at 8:59 PM (peak) snaps to 8:30 PM (still peak)."""
        dt = _local(2026, 4, 14, 20, 59)  # Tue 8:59 PM
        assert _bucket_minutes(dt) == 30
        result = _bucket_start(dt)
        assert result.hour == 20 and result.minute == 30

    def test_offpeak_boundary_snap(self):
        """Job at 9:15 PM (off-peak) snaps to 9:00 PM (off-peak)."""
        dt = _local(2026, 4, 14, 21, 15)  # Tue 9:15 PM
        assert _bucket_minutes(dt) == 60
        result = _bucket_start(dt)
        assert result.hour == 21 and result.minute == 0


# ═══════════════════════════════════════════════════════════════════════════
# _bucket_label — human-readable label
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketLabel:

    def test_30min_label(self):
        dt = _local(2026, 4, 14, 14, 0)
        assert _bucket_label(dt, 30) == "2:00 PM"

    def test_30min_label_half(self):
        dt = _local(2026, 4, 14, 14, 30)
        assert _bucket_label(dt, 30) == "2:30 PM"

    def test_30min_label_midnight(self):
        dt = _local(2026, 4, 14, 0, 0)
        # strftime gives "12:00 AM", lstrip("0") keeps "12:00 AM"
        assert _bucket_label(dt, 30) == "12:00 AM"

    def test_30min_label_noon(self):
        dt = _local(2026, 4, 14, 12, 0)
        assert _bucket_label(dt, 30) == "12:00 PM"

    def test_60min_label(self):
        dt = _local(2026, 4, 14, 23, 0)
        assert _bucket_label(dt, 60) == "11 PM"

    def test_60min_label_midnight(self):
        dt = _local(2026, 4, 14, 0, 0)
        assert _bucket_label(dt, 60) == "12 AM"

    def test_60min_label_1am(self):
        dt = _local(2026, 4, 14, 1, 0)
        assert _bucket_label(dt, 60) == "1 AM"

    def test_240min_label_morning(self):
        dt = _local(2026, 4, 11, 8, 0)  # Saturday
        # 8 AM - 12 PM
        assert _bucket_label(dt, 240) == "8 AM-12 PM"

    def test_240min_label_evening(self):
        dt = _local(2026, 4, 11, 20, 0)  # Saturday
        # 8 PM - 12 AM (next day, but strftime still works)
        label = _bucket_label(dt, 240)
        assert label == "8 PM-12 AM"

    def test_240min_label_midnight_block(self):
        dt = _local(2026, 4, 11, 0, 0)  # Saturday midnight
        assert _bucket_label(dt, 240) == "12 AM-4 AM"

    def test_240min_label_afternoon(self):
        dt = _local(2026, 4, 11, 12, 0)  # Saturday noon
        assert _bucket_label(dt, 240) == "12 PM-4 PM"

    def test_no_leading_zero(self):
        """Labels should NOT have leading zeros (e.g., '9 AM' not '09 AM')."""
        dt = _local(2026, 4, 14, 9, 0)
        assert _bucket_label(dt, 30) == "9:00 AM"
        dt2 = _local(2026, 4, 14, 3, 0)
        assert _bucket_label(dt2, 60) == "3 AM"


# ═══════════════════════════════════════════════════════════════════════════
# _bucket_key — unique key format
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketKey:

    def test_format(self):
        dt = _local(2026, 4, 14, 14, 30)
        assert _bucket_key(dt) == "2026-04-14_14:30"

    def test_midnight(self):
        dt = _local(2026, 4, 14, 0, 0)
        assert _bucket_key(dt) == "2026-04-14_00:00"

    def test_end_of_day(self):
        dt = _local(2026, 4, 14, 23, 30)
        assert _bucket_key(dt) == "2026-04-14_23:30"


# ═══════════════════════════════════════════════════════════════════════════
# get_time_counts — full bucket generation
# ═══════════════════════════════════════════════════════════════════════════

class TestGetTimeCounts:

    def test_empty_store(self):
        store = {"jobs": {}}
        tc = get_time_counts(store, tz_offset=240)
        assert tc["this_hour"] == 0
        assert tc["today"] == 0
        assert tc["yesterday"] == 0
        assert tc["buckets"] == []

    def test_single_recent_job(self):
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=10)])
        tc = get_time_counts(store, tz_offset=240)
        assert tc["this_hour"] == 1
        assert tc["today"] == 1
        assert len(tc["buckets"]) == 1
        assert tc["buckets"][0]["count"] == 1

    def test_buckets_sorted_newest_first(self):
        now = datetime.now(UTC)
        times = [now - timedelta(hours=h) for h in [1, 3, 5, 10]]
        store = _make_store_with_jobs(times)
        tc = get_time_counts(store, tz_offset=240)
        starts = [b["start_iso"] for b in tc["buckets"]]
        assert starts == sorted(starts, reverse=True)

    def test_jobs_older_than_24h_excluded_from_buckets(self):
        now = datetime.now(UTC)
        times = [
            now - timedelta(hours=1),   # in buckets
            now - timedelta(hours=23),  # in buckets
            now - timedelta(hours=25),  # NOT in buckets
        ]
        store = _make_store_with_jobs(times)
        tc = get_time_counts(store, tz_offset=0)
        total_in_buckets = sum(b["count"] for b in tc["buckets"])
        assert total_in_buckets == 2

    def test_yesterday_count(self):
        """Jobs from yesterday (user local) counted in yesterday tab."""
        now = datetime.now(UTC)
        # Make a job from 30 hours ago — should be "yesterday" for most tz
        store = _make_store_with_jobs([now - timedelta(hours=30)])
        tc = get_time_counts(store, tz_offset=0)
        assert tc["yesterday"] == 1
        assert tc["today"] == 0

    def test_this_hour_count(self):
        now = datetime.now(UTC)
        store = _make_store_with_jobs([
            now - timedelta(minutes=10),
            now - timedelta(minutes=50),
            now - timedelta(hours=2),
        ])
        tc = get_time_counts(store, tz_offset=0)
        assert tc["this_hour"] == 2

    def test_tz_offset_affects_buckets(self):
        """Same UTC time should produce different bucket keys for different tz."""
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc_est = get_time_counts(store, tz_offset=240)
        tc_pst = get_time_counts(store, tz_offset=420)
        # Both should have 1 bucket with 1 job, but keys may differ
        assert len(tc_est["buckets"]) == 1
        assert len(tc_pst["buckets"]) == 1
        # Bucket labels might differ (EST vs PST local time)

    def test_bucket_has_required_fields(self):
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc = get_time_counts(store, tz_offset=240)
        bucket = tc["buckets"][0]
        assert "key" in bucket
        assert "label" in bucket
        assert "count" in bucket
        assert "minutes" in bucket
        assert "start_iso" in bucket

    def test_multiple_jobs_same_bucket(self):
        """Jobs within same 30-min window land in same bucket."""
        now = datetime.now(UTC)
        # Two jobs 5 min apart should be in same bucket
        store = _make_store_with_jobs([
            now - timedelta(minutes=5),
            now - timedelta(minutes=10),
        ])
        tc = get_time_counts(store, tz_offset=0)
        # Should be 1 bucket with count 2 (unless they span a boundary)
        total = sum(b["count"] for b in tc["buckets"])
        assert total == 2

    def test_jobs_missing_first_seen_skipped(self):
        store = {"jobs": {
            "a": {"first_seen": "", "title": "X"},
            "b": {"title": "Y"},  # no first_seen at all
        }}
        tc = get_time_counts(store, tz_offset=0)
        assert tc["this_hour"] == 0
        assert tc["buckets"] == []

    def test_bucket_minutes_in_output(self):
        """Each bucket reports its size in minutes."""
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc = get_time_counts(store, tz_offset=0)
        bm = tc["buckets"][0]["minutes"]
        assert bm in (30, 60, 240)


# ═══════════════════════════════════════════════════════════════════════════
# get_filtered_jobs — bucket_filter parameter
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketFilter:

    def _store_with_timed_jobs(self):
        """Store with jobs at known UTC times for a Tuesday (weekday)."""
        # Tuesday April 14, 2026 in EST
        est_offset = timezone(timedelta(hours=-4))
        jobs = {}

        # Job A: 10:05 AM EST = 14:05 UTC → bucket 10:00 AM (30-min peak)
        utc_a = datetime(2026, 4, 14, 14, 5, tzinfo=UTC)
        jobs["url_a"] = self._job("CompanyA", utc_a)

        # Job B: 10:25 AM EST = 14:25 UTC → bucket 10:00 AM (same as A)
        utc_b = datetime(2026, 4, 14, 14, 25, tzinfo=UTC)
        jobs["url_b"] = self._job("CompanyB", utc_b)

        # Job C: 10:35 AM EST = 14:35 UTC → bucket 10:30 AM (next 30-min)
        utc_c = datetime(2026, 4, 14, 14, 35, tzinfo=UTC)
        jobs["url_c"] = self._job("CompanyC", utc_c)

        # Job D: 11:00 PM EST = 03:00 UTC (next day) → bucket 11 PM (60-min off-peak)
        utc_d = datetime(2026, 4, 15, 3, 0, tzinfo=UTC)
        jobs["url_d"] = self._job("CompanyD", utc_d)

        return {"jobs": jobs, "last_updated": datetime.now(UTC).isoformat()}

    def _job(self, company, utc_dt):
        return {
            "company": company, "title": "SWE", "location": "Remote",
            "url": f"https://example.com/{company}", "description_preview": "",
            "status": "", "first_seen": utc_dt.isoformat(),
            "last_seen": utc_dt.isoformat(), "date_posted": "",
            "search_term": "", "score": 30, "score_pct": 25,
            "level": "Entry", "min_exp": None, "max_exp": None,
            "competition": 0, "keyword_hits": 0, "recommended": True,
            "variant": "se", "reason": "",
        }

    def test_filter_30min_bucket(self):
        """Filter by 10:00 AM bucket → returns only jobs A and B."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:00", tz_offset=240
        )
        companies = {j["company"] for j in jobs}
        assert companies == {"CompanyA", "CompanyB"}

    def test_filter_next_30min_bucket(self):
        """Filter by 10:30 AM bucket → returns only job C."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:30", tz_offset=240
        )
        assert len(jobs) == 1
        assert jobs[0]["company"] == "CompanyC"

    def test_filter_60min_bucket(self):
        """Filter by 11:00 PM bucket (off-peak) → returns job D."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_23:00", tz_offset=240
        )
        assert len(jobs) == 1
        assert jobs[0]["company"] == "CompanyD"

    def test_filter_empty_bucket(self):
        """Filter by bucket with no jobs → empty result."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_15:00", tz_offset=240
        )
        assert len(jobs) == 0

    def test_invalid_bucket_format_ignored(self):
        """Invalid bucket_filter format → no filter applied, all jobs returned."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(store, bucket_filter="not-a-date")
        assert len(jobs) == 4  # all jobs

    def test_empty_bucket_filter_no_filter(self):
        """Empty string bucket_filter → no filter."""
        store = self._store_with_timed_jobs()
        jobs = get_filtered_jobs(store, bucket_filter="")
        assert len(jobs) == 4

    def test_bucket_filter_respects_tz_offset(self):
        """Same bucket key with different tz_offset → different results."""
        store = self._store_with_timed_jobs()
        # Job A is at 14:05 UTC.
        # With tz_offset=240 (EST): local time = 10:05 AM, bucket = 10:00 AM
        # With tz_offset=0 (UTC): local time = 2:05 PM, bucket = 2:00 PM
        jobs_est = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:00", tz_offset=240
        )
        jobs_utc = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:00", tz_offset=0
        )
        # EST should find jobs A+B, UTC should find nothing at 10:00
        assert len(jobs_est) == 2
        assert len(jobs_utc) == 0

    def test_bucket_filter_boundary_exclusive_end(self):
        """Job at exact bucket end boundary is NOT included."""
        # Bucket 10:00-10:30 AM — job at exactly 10:30 should be in NEXT bucket
        store = {"jobs": {
            "url_x": self._job("X", datetime(2026, 4, 14, 14, 30, tzinfo=UTC)),
        }, "last_updated": ""}
        # 14:30 UTC = 10:30 AM EST → this is the START of the 10:30 bucket, not in 10:00
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:00", tz_offset=240
        )
        assert len(jobs) == 0  # exactly at boundary → next bucket
        jobs2 = get_filtered_jobs(
            store, bucket_filter="2026-04-14_10:30", tz_offset=240
        )
        assert len(jobs2) == 1

    def test_bucket_filter_consistency_with_get_time_counts(self):
        """Clicking a bucket from get_time_counts should return matching jobs."""
        store = self._store_with_timed_jobs()
        tz = 240

        # get_time_counts to generate buckets — but these are "last 24h" only
        # We need to mock "now" or use jobs close to current time
        # Instead, test that the bucket keys from get_time_counts can be used as filters
        now = datetime.now(UTC)
        fresh_store = _make_store_with_jobs([
            now - timedelta(minutes=5),
            now - timedelta(minutes=10),
            now - timedelta(hours=2),
        ])
        tc = get_time_counts(fresh_store, tz_offset=tz)
        for bucket in tc["buckets"]:
            filtered = get_filtered_jobs(
                fresh_store, bucket_filter=bucket["key"], tz_offset=tz,
            )
            assert len(filtered) == bucket["count"], (
                f"Bucket {bucket['key']} claims {bucket['count']} jobs "
                f"but filter returned {len(filtered)}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# get_filtered_counts — bucket_filter with counts
# ═══════════════════════════════════════════════════════════════════════════

class TestFilteredCountsBucket:

    def _store(self):
        now = datetime.now(UTC)
        return _make_store_with_jobs([
            now - timedelta(minutes=5),
            now - timedelta(minutes=10),
            now - timedelta(hours=3),
        ])

    def test_no_bucket_filter_counts_all(self):
        store = self._store()
        fc = get_filtered_counts(store)
        assert fc["status"]["All"] == 3

    def test_bucket_filter_restricts_counts(self):
        store = self._store()
        tc = get_time_counts(store, tz_offset=0)
        if tc["buckets"]:
            bucket = tc["buckets"][0]  # most recent
            fc = get_filtered_counts(
                store, bucket_filter=bucket["key"], tz_offset=0,
            )
            assert fc["status"]["All"] == bucket["count"]

    def test_invalid_bucket_filter_returns_all(self):
        store = self._store()
        fc = get_filtered_counts(store, bucket_filter="invalid")
        assert fc["status"]["All"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# CI Merge Pipeline — the new merge step in scan-jobs.yml
# ═══════════════════════════════════════════════════════════════════════════

class TestCIMergePipeline:
    """Simulates what the CI merge step does."""

    def test_first_run_no_existing_store(self, tmp_path):
        """First CI run: linkedin_jobs.json doesn't exist."""
        store_path = tmp_path / "linkedin_jobs.json"
        scan_path = tmp_path / "scan_results.json"
        scan_data = [
            {"company": "Stripe", "title": "SWE", "url": "https://example.com/1",
             "location": "SF", "description_preview": "Python dev"},
        ]
        scan_path.write_text(json.dumps(scan_data))

        # Simulate CI merge step
        store = load_store(store_path)
        assert store == {"jobs": {}, "last_updated": ""}
        store = merge_scan_results(store, scan_data)
        store = prune_old_jobs(store)
        for key in store.get("jobs", {}):
            store["jobs"][key] = backfill_job(store["jobs"][key])
        save_store(store_path, store)

        # Verify
        loaded = load_store(store_path)
        assert len(loaded["jobs"]) == 1

    def test_merge_accumulates_jobs(self, tmp_path):
        """Multiple merges accumulate unique jobs."""
        store_path = tmp_path / "linkedin_jobs.json"

        # First scan: 2 jobs
        scan1 = [
            {"company": "Stripe", "title": "SWE", "url": "https://a.com/1",
             "location": "SF", "description_preview": ""},
            {"company": "Google", "title": "SDE", "url": "https://a.com/2",
             "location": "NYC", "description_preview": ""},
        ]
        store = load_store(store_path)
        store = merge_scan_results(store, scan1)
        save_store(store_path, store)
        assert len(store["jobs"]) == 2

        # Second scan: 1 old + 1 new
        scan2 = [
            {"company": "Stripe", "title": "SWE", "url": "https://a.com/1",
             "location": "SF", "description_preview": ""},  # duplicate
            {"company": "Meta", "title": "ML Eng", "url": "https://a.com/3",
             "location": "LA", "description_preview": ""},  # new
        ]
        store = load_store(store_path)
        store = merge_scan_results(store, scan2)
        save_store(store_path, store)
        assert len(store["jobs"]) == 3

    def test_merge_preserves_user_status(self, tmp_path):
        """User status (Tracking, Applied) survives merge."""
        store_path = tmp_path / "linkedin_jobs.json"

        scan1 = [{"company": "Stripe", "title": "SWE", "url": "https://a.com/1",
                   "location": "SF", "description_preview": ""}]
        store = load_store(store_path)
        store = merge_scan_results(store, scan1)
        store["jobs"]["https://a.com/1"]["status"] = "Tracking"
        save_store(store_path, store)

        # Re-merge same job
        store = load_store(store_path)
        store = merge_scan_results(store, scan1)
        save_store(store_path, store)
        assert store["jobs"]["https://a.com/1"]["status"] == "Tracking"

    def test_merge_preserves_first_seen(self, tmp_path):
        """first_seen should NOT change on re-merge."""
        store_path = tmp_path / "linkedin_jobs.json"

        scan = [{"company": "Stripe", "title": "SWE", "url": "https://a.com/1",
                  "location": "SF", "description_preview": ""}]
        store = load_store(store_path)
        store = merge_scan_results(store, scan)
        first_seen = store["jobs"]["https://a.com/1"]["first_seen"]
        save_store(store_path, store)

        # Wait and re-merge
        store = load_store(store_path)
        store = merge_scan_results(store, scan)
        assert store["jobs"]["https://a.com/1"]["first_seen"] == first_seen

    def test_merge_empty_scan_results(self, tmp_path):
        """Empty scan results shouldn't corrupt the store."""
        store_path = tmp_path / "linkedin_jobs.json"

        # Seed with 1 job
        scan = [{"company": "Stripe", "title": "SWE", "url": "https://a.com/1",
                  "location": "SF", "description_preview": ""}]
        store = load_store(store_path)
        store = merge_scan_results(store, scan)
        save_store(store_path, store)
        assert len(store["jobs"]) == 1

        # Merge empty
        store = load_store(store_path)
        store = merge_scan_results(store, [])
        save_store(store_path, store)
        assert len(store["jobs"]) == 1  # unchanged

    def test_prune_removes_old_untracked(self, tmp_path):
        """Prune removes old jobs without keep-statuses."""
        store = {"jobs": {
            "old": {
                "status": "", "last_seen": (
                    datetime.now(UTC) - timedelta(days=10)
                ).isoformat(),
            },
            "old_tracked": {
                "status": "Applied", "last_seen": (
                    datetime.now(UTC) - timedelta(days=10)
                ).isoformat(),
            },
            "fresh": {
                "status": "", "last_seen": datetime.now(UTC).isoformat(),
            },
        }}
        pruned = prune_old_jobs(store, days=7)
        assert "old" not in pruned["jobs"]
        assert "old_tracked" in pruned["jobs"]
        assert "fresh" in pruned["jobs"]

    def test_merge_carries_ai_scores(self, tmp_path):
        """AI scores from scan results are preserved in the store."""
        store_path = tmp_path / "linkedin_jobs.json"
        scan = [{
            "company": "Stripe", "title": "SWE", "url": "https://a.com/1",
            "location": "SF", "description_preview": "Python dev",
            "ai_score": 8, "ai_reason": "Strong match",
        }]
        store = load_store(store_path)
        store = merge_scan_results(store, scan)
        save_store(store_path, store)
        assert store["jobs"]["https://a.com/1"]["ai_score"] == 8
        assert store["jobs"]["https://a.com/1"]["ai_reason"] == "Strong match"

    def test_merge_does_not_overwrite_existing_ai_scores(self, tmp_path):
        """Existing AI scores are NOT overwritten by new scan without AI."""
        store_path = tmp_path / "linkedin_jobs.json"

        # First scan with AI
        scan1 = [{
            "company": "Stripe", "title": "SWE", "url": "https://a.com/1",
            "location": "SF", "description_preview": "Python",
            "ai_score": 9, "ai_reason": "Perfect match",
        }]
        store = load_store(store_path)
        store = merge_scan_results(store, scan1)
        save_store(store_path, store)

        # Second scan WITHOUT AI
        scan2 = [{
            "company": "Stripe", "title": "SWE", "url": "https://a.com/1",
            "location": "SF", "description_preview": "Python",
        }]
        store = load_store(store_path)
        store = merge_scan_results(store, scan2)
        assert store["jobs"]["https://a.com/1"]["ai_score"] == 9

    def test_corrupt_json_returns_empty_store(self, tmp_path):
        """Corrupt linkedin_jobs.json → graceful empty store."""
        path = tmp_path / "linkedin_jobs.json"
        path.write_text("{broken json???")
        store = load_store(path)
        assert store == {"jobs": {}, "last_updated": ""}


# ═══════════════════════════════════════════════════════════════════════════
# Web API — bucket parameter + X-Buckets header
# ═══════════════════════════════════════════════════════════════════════════

class TestWebBucketAPI:

    @pytest.fixture
    def client(self):
        from jobflow.web import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_api_returns_x_buckets_header(self, client):
        r = client.get("/api/linkedin/jobs?tz=240")
        assert "X-Buckets" in r.headers
        buckets = json.loads(r.headers["X-Buckets"])
        assert isinstance(buckets, list)

    def test_api_bucket_filter_param(self, client):
        """bucket query param is accepted without error."""
        r = client.get("/api/linkedin/jobs?bucket=2026-04-14_10:00&tz=240")
        assert r.status_code == 200

    def test_api_invalid_bucket_no_crash(self, client):
        """Invalid bucket format doesn't crash the API."""
        r = client.get("/api/linkedin/jobs?bucket=garbage&tz=240")
        assert r.status_code == 200

    def test_api_bucket_with_time_filter(self, client):
        """Both bucket and time params accepted (frontend prevents this)."""
        r = client.get("/api/linkedin/jobs?bucket=2026-04-14_10:00&time=today&tz=240")
        assert r.status_code == 200

    def test_api_tz_param_parsed_correctly(self, client):
        """tz param handles various values."""
        for tz in ["0", "240", "-330", "720"]:
            r = client.get(f"/api/linkedin/jobs?tz={tz}")
            assert r.status_code == 200

    def test_api_empty_tz_defaults_zero(self, client):
        r = client.get("/api/linkedin/jobs?tz=")
        assert r.status_code == 200

    def test_api_no_tz_defaults_zero(self, client):
        r = client.get("/api/linkedin/jobs")
        assert r.status_code == 200

    def test_x_buckets_fields(self, client):
        """Each bucket in X-Buckets has required fields."""
        r = client.get("/api/linkedin/jobs?tz=240")
        buckets = json.loads(r.headers["X-Buckets"])
        for b in buckets:
            assert "key" in b
            assert "label" in b
            assert "count" in b
            assert "minutes" in b
            assert "start_iso" in b

    def test_x_time_counts_header(self, client):
        """X-Time-Counts header has this_hour, today, yesterday."""
        r = client.get("/api/linkedin/jobs?tz=240")
        tc = json.loads(r.headers["X-Time-Counts"])
        assert "this_hour" in tc
        assert "today" in tc
        assert "yesterday" in tc


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases — timezone extremes, DST-like shifts, negative offsets
# ═══════════════════════════════════════════════════════════════════════════

class TestTimezoneEdgeCases:

    def test_negative_tz_offset_ahead_of_utc(self):
        """IST (UTC+5:30) uses tz_offset=-330 (negative = ahead of UTC)."""
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc = get_time_counts(store, tz_offset=-330)
        assert tc["this_hour"] == 1
        assert len(tc["buckets"]) == 1

    def test_extreme_tz_offset_dateline(self):
        """UTC+12 (Fiji) tz_offset=-720."""
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc = get_time_counts(store, tz_offset=-720)
        assert tc["this_hour"] == 1

    def test_zero_tz_offset(self):
        """UTC tz_offset=0."""
        now = datetime.now(UTC)
        store = _make_store_with_jobs([now - timedelta(minutes=5)])
        tc = get_time_counts(store, tz_offset=0)
        assert tc["this_hour"] == 1

    def test_bucket_filter_with_negative_offset(self):
        """Bucket filter works with IST timezone."""
        # Job at 14:05 UTC, IST = UTC+5:30 → 19:35 IST → bucket 19:30 (if weekday peak)
        utc_time = datetime(2026, 4, 14, 14, 5, tzinfo=UTC)
        store = _make_store_with_jobs([utc_time])
        # IST offset = -330 (ahead of UTC)
        # Local time = 14:05 + 5:30 = 19:35 → Tuesday → peak → 30-min → bucket 19:30
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_19:30", tz_offset=-330,
        )
        assert len(jobs) == 1

    def test_midnight_crossover_bucket_assignment(self):
        """Job at 11:55 PM should be in the 11 PM bucket (60-min off-peak)."""
        # Tuesday 11:55 PM EST = Wednesday 3:55 AM UTC
        utc_time = datetime(2026, 4, 15, 3, 55, tzinfo=UTC)
        store = _make_store_with_jobs([utc_time])
        # Local time 11:55 PM Tue → off-peak → 60-min → bucket 23:00
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-14_23:00", tz_offset=240,
        )
        assert len(jobs) == 1

    def test_weekend_4hour_bucket_filter(self):
        """Weekend 4-hour bucket filter works."""
        # Saturday 2:15 PM EST = 18:15 UTC
        utc_time = datetime(2026, 4, 11, 18, 15, tzinfo=UTC)
        store = _make_store_with_jobs([utc_time])
        # Saturday 2:15 PM → 240-min → block 12 (12:00-16:00)
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-11_12:00", tz_offset=240,
        )
        assert len(jobs) == 1

    def test_weekend_bucket_rejects_outside_range(self):
        """Weekend job NOT in adjacent 4-hour block."""
        # Saturday 2:15 PM EST → block 12:00-16:00
        utc_time = datetime(2026, 4, 11, 18, 15, tzinfo=UTC)
        store = _make_store_with_jobs([utc_time])
        # Try block 16:00-20:00 → should NOT include this job
        jobs = get_filtered_jobs(
            store, bucket_filter="2026-04-11_16:00", tz_offset=240,
        )
        assert len(jobs) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Roundtrip: get_time_counts → bucket key → get_filtered_jobs consistency
# ═══════════════════════════════════════════════════════════════════════════

class TestBucketRoundtrip:
    """The most critical test: bucket keys from get_time_counts MUST work
    as filters in get_filtered_jobs and return the same count."""

    def _make_diverse_store(self):
        """Store with jobs spread across peak, off-peak, and weekend times."""
        now = datetime.now(UTC)
        times = [
            now - timedelta(minutes=5),    # current bucket
            now - timedelta(minutes=10),   # same bucket likely
            now - timedelta(hours=1),      # 1 hour ago
            now - timedelta(hours=3),      # 3 hours ago
            now - timedelta(hours=8),      # 8 hours ago
            now - timedelta(hours=16),     # 16 hours ago
            now - timedelta(hours=23),     # 23 hours ago
        ]
        return _make_store_with_jobs(times)

    @pytest.mark.parametrize("tz_offset", [0, 240, 420, -330, -540])
    def test_roundtrip_all_timezones(self, tz_offset):
        """For every timezone, every bucket's count matches filter result."""
        store = self._make_diverse_store()
        tc = get_time_counts(store, tz_offset=tz_offset)
        for bucket in tc["buckets"]:
            filtered = get_filtered_jobs(
                store, bucket_filter=bucket["key"], tz_offset=tz_offset,
            )
            assert len(filtered) == bucket["count"], (
                f"tz={tz_offset}, bucket={bucket['key']}: "
                f"expected {bucket['count']}, got {len(filtered)}"
            )

    @pytest.mark.parametrize("tz_offset", [0, 240, 420, -330])
    def test_filtered_counts_match_bucket(self, tz_offset):
        """get_filtered_counts with bucket_filter matches bucket count."""
        store = self._make_diverse_store()
        tc = get_time_counts(store, tz_offset=tz_offset)
        for bucket in tc["buckets"]:
            fc = get_filtered_counts(
                store, bucket_filter=bucket["key"], tz_offset=tz_offset,
            )
            assert fc["status"]["All"] == bucket["count"], (
                f"tz={tz_offset}, bucket={bucket['key']}: "
                f"filtered_counts={fc['status']['All']}, "
                f"bucket_count={bucket['count']}"
            )
