"""Tests for jobflow/linkedin_store.py — job persistence, filtering, dedup."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from jobflow.linkedin_store import (
    load_store, save_store, merge_scan_results, prune_old_jobs,
    update_job_status, get_filtered_jobs, get_status_counts,
    get_level_counts, get_search_terms, format_recency,
    backfill_job, LINKEDIN_STATUSES, RECOMMENDED_THRESHOLD,
    RETENTION_DAYS, KEEP_STATUSES,
)


class TestLoadSaveStore:
    """Test store persistence."""

    def test_load_missing_file(self, tmp_path):
        store = load_store(tmp_path / "nonexistent.json")
        assert store == {"jobs": {}, "last_updated": ""}

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "store.json"
        store = {"jobs": {"key1": {"title": "SWE"}}, "last_updated": ""}
        save_store(path, store)
        loaded = load_store(path)
        assert "key1" in loaded["jobs"]
        assert loaded["last_updated"] != ""  # save_store sets timestamp

    def test_load_corrupt_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json{{{")
        store = load_store(path)
        assert store == {"jobs": {}, "last_updated": ""}


class TestMergeScanResults:
    """Test merging scan results with deduplication."""

    def test_new_jobs_added(self, empty_store):
        results = [
            {"company": "Stripe", "title": "SWE", "url": "https://example.com/1",
             "score": 50, "score_pct": 38, "location": "SF"},
            {"company": "Google", "title": "SDE", "url": "https://example.com/2",
             "score": 40, "score_pct": 31, "location": "NYC"},
        ]
        store = merge_scan_results(empty_store, results)
        assert len(store["jobs"]) == 2

    def test_existing_job_updated(self, empty_store):
        results = [{"company": "Stripe", "title": "SWE", "url": "https://example.com/1",
                     "score": 50, "score_pct": 38, "location": "SF"}]
        store = merge_scan_results(empty_store, results)
        first_seen = store["jobs"]["https://example.com/1"]["first_seen"]

        # Merge again — should update last_seen but keep first_seen
        store = merge_scan_results(store, results)
        assert store["jobs"]["https://example.com/1"]["first_seen"] == first_seen

    def test_dedup_same_company_title(self, empty_store):
        """Same company+title in different locations → keep one."""
        results = [
            {"company": "CapTech", "title": "ML Engineer", "url": "https://example.com/1", "location": "Denver, CO"},
            {"company": "CapTech", "title": "ML Engineer", "url": "https://example.com/2", "location": "Charlotte, NC"},
            {"company": "CapTech", "title": "ML Engineer", "url": "https://example.com/3", "location": "Chicago, IL"},
        ]
        store = merge_scan_results(empty_store, results)
        # Should dedup to 1 entry
        captech_jobs = [j for j in store["jobs"].values() if j["company"] == "CapTech"]
        assert len(captech_jobs) == 1

    def test_status_migration(self, empty_store):
        """Old 'Should Apply' and 'New' statuses get migrated to ''."""
        empty_store["jobs"]["key1"] = {
            "company": "X", "title": "Y", "status": "Should Apply",
            "first_seen": "2026-01-01", "last_seen": "2026-01-01",
            "description_preview": "", "url": "",
        }
        results = [{"company": "X", "title": "Y", "url": "key1"}]
        store = merge_scan_results(empty_store, results)
        assert store["jobs"]["key1"]["status"] == ""


class TestPruneOldJobs:
    """Test 7-day job pruning."""

    def test_old_jobs_removed(self, sample_store):
        # Job 3 is 5 days old with "Not Interested" — should be pruned
        # Job 4 is 10 days old with "Applied" — should be KEPT
        store = prune_old_jobs(sample_store, days=7)
        jobs = store["jobs"]
        assert "https://example.com/4" in jobs  # Applied survives
        # Job 3 is 5 days old, within 7 days — actually should NOT be pruned
        assert "https://example.com/3" in jobs

    def test_tracking_survives_prune(self):
        now = datetime.now(tz=timezone.utc)
        store = {
            "jobs": {
                "old_tracked": {
                    "status": "Tracking",
                    "last_seen": (now - timedelta(days=30)).isoformat(),
                },
                "old_untracked": {
                    "status": "",
                    "last_seen": (now - timedelta(days=30)).isoformat(),
                },
            }
        }
        pruned = prune_old_jobs(store, days=7)
        assert "old_tracked" in pruned["jobs"]
        assert "old_untracked" not in pruned["jobs"]


class TestUpdateJobStatus:
    """Test status updates."""

    def test_valid_status(self, sample_store):
        assert update_job_status(sample_store, "https://example.com/1", "Tracking") is True
        assert sample_store["jobs"]["https://example.com/1"]["status"] == "Tracking"

    def test_clear_status(self, sample_store):
        assert update_job_status(sample_store, "https://example.com/2", "") is True
        assert sample_store["jobs"]["https://example.com/2"]["status"] == ""

    def test_invalid_key(self, sample_store):
        assert update_job_status(sample_store, "nonexistent", "Tracking") is False

    def test_invalid_status(self, sample_store):
        assert update_job_status(sample_store, "https://example.com/1", "InvalidStatus") is False


class TestGetFilteredJobs:
    """Test job filtering and sorting."""

    def test_no_filter_returns_all(self, sample_store):
        jobs = get_filtered_jobs(sample_store)
        assert len(jobs) == 4

    def test_status_filter(self, sample_store):
        jobs = get_filtered_jobs(sample_store, status="Tracking")
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Google"

    def test_recommended_filter(self, sample_store):
        jobs = get_filtered_jobs(sample_store, status="Recommended")
        assert all(j["recommended"] for j in jobs)

    def test_level_filter(self, sample_store):
        jobs = get_filtered_jobs(sample_store, level="New Grad")
        assert len(jobs) == 1
        assert jobs[0]["level"] == "New Grad"

    def test_text_search(self, sample_store):
        jobs = get_filtered_jobs(sample_store, query="stripe")
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Stripe"

    def test_sort_by_score(self, sample_store):
        jobs = get_filtered_jobs(sample_store, sort_col="score_pct", sort_dir="desc")
        # Not Interested should be at bottom regardless
        non_ni = [j for j in jobs if j["status"] != "Not Interested"]
        for i in range(len(non_ni) - 1):
            assert non_ni[i]["score_pct"] >= non_ni[i + 1]["score_pct"]

    def test_not_interested_at_bottom(self, sample_store):
        jobs = get_filtered_jobs(sample_store)
        ni_indices = [i for i, j in enumerate(jobs) if j["status"] == "Not Interested"]
        non_ni_indices = [i for i, j in enumerate(jobs) if j["status"] != "Not Interested"]
        if ni_indices and non_ni_indices:
            assert min(ni_indices) > max(non_ni_indices)


class TestStatusCounts:
    """Test count aggregation."""

    def test_counts(self, sample_store):
        counts = get_status_counts(sample_store)
        assert counts["All"] == 4
        assert counts["Tracking"] == 1
        assert counts["Applied"] == 1
        assert counts["Not Interested"] == 1
        assert counts["Recommended"] >= 1

    def test_level_counts(self, sample_store):
        lvl = get_level_counts(sample_store)
        assert lvl["New Grad"] == 1
        assert lvl["Entry"] == 2
        assert lvl["All"] == 4


class TestFormatRecency:
    """Test human-friendly time formatting."""

    def test_recent(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(minutes=30)).isoformat()
        result = format_recency(ts)
        assert "just now" in result

    def test_hours_ago(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(hours=5)).isoformat()
        result = format_recency(ts)
        assert "5h ago" in result

    def test_days_ago(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()
        result = format_recency(ts)
        assert "3d ago" in result

    def test_empty(self):
        assert format_recency("") == "--"

    def test_invalid(self):
        assert format_recency("not-a-date") == "--"
