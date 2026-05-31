"""Tests for jobflow/web — Flask routes and API endpoints."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _test_store_path() -> Path:
    config_path = Path(os.environ["JOBFLOW_CONFIG"])
    return config_path.parent.parent / "data" / "ci" / "linkedin_jobs.json"


def _job(url: str, title: str, source: str = "linkedin") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "url": url,
        "company": "ExampleCo",
        "title": title,
        "location": "New York, NY",
        "score_pct": 82,
        "level": "Entry",
        "recommended": True,
        "status": "",
        "first_seen": now,
        "last_seen": now,
        "search_term": "New Grad ASIC Design Engineer",
        "source": source,
    }


def _write_store(jobs: list[dict]) -> None:
    _test_store_path().write_text(json.dumps({
        "jobs": {job["url"]: job for job in jobs},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }))


class TestPageRoutes:
    """Test that all pages load correctly."""

    def test_root_redirects(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/linkedin" in r.headers["Location"]

    def test_linkedin_page(self, client):
        r = client.get("/linkedin")
        assert r.status_code == 200
        assert b"LinkedIn Feed" in r.data
        assert b"bulk-actions" in r.data
        assert b"Open top 10" in r.data
        assert b"Delete top 10" in r.data

    def test_boards_page(self, client):
        r = client.get("/boards")
        assert r.status_code == 200
        assert b"Job Boards" in r.data

    def test_scan_page(self, client):
        r = client.get("/scan")
        assert r.status_code == 200

    def test_tailor_page(self, client):
        r = client.get("/tailor")
        assert r.status_code == 200

    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["status"] == "ok"

    def test_removed_routes_404(self, client):
        """Removed pages should return 404."""
        for path in ["/applications", "/application/1", "/dashboard"]:
            r = client.get(path)
            assert r.status_code == 404, f"{path} should be 404"


class TestLinkedInAPI:
    """Test LinkedIn jobs API endpoint."""

    def test_returns_html(self, client):
        r = client.get("/api/linkedin/jobs")
        assert r.status_code == 200
        assert r.content_type.startswith("text/html")

    def test_returns_count_headers(self, client):
        r = client.get("/api/linkedin/jobs")
        assert "X-Counts" in r.headers
        assert "X-Level-Counts" in r.headers
        assert "X-Time-Counts" in r.headers
        counts = json.loads(r.headers["X-Counts"])
        assert "All" in counts
        assert "Recommended" in counts

    def test_status_filter(self, client):
        r = client.get("/api/linkedin/jobs?status=Tracking")
        assert r.status_code == 200

    def test_level_filter(self, client):
        r = client.get("/api/linkedin/jobs?level=Entry")
        assert r.status_code == 200

    def test_search_filter(self, client):
        r = client.get("/api/linkedin/jobs?q=engineer")
        assert r.status_code == 200

    def test_sort_params(self, client):
        r = client.get("/api/linkedin/jobs?sort=score_pct&dir=desc")
        assert r.status_code == 200

    def test_time_filter(self, client):
        r = client.get("/api/linkedin/jobs?time=today&tz=240")
        assert r.status_code == 200

    def test_combined_filters(self, client):
        r = client.get("/api/linkedin/jobs?level=Entry&q=asic&sort=score_pct&dir=desc&tz=240")
        assert r.status_code == 200

    def test_rows_include_bulk_selection_metadata(self, client):
        _write_store([_job("https://example.com/1", "ASIC Design Engineer")])
        r = client.get("/api/linkedin/jobs?time=")
        assert r.status_code == 200
        assert b'class="job-select"' in r.data
        assert b'data-key="https://example.com/1"' in r.data

    def test_bulk_delete_json_jobs(self, client):
        _write_store([
            _job("https://example.com/1", "ASIC Design Engineer One"),
            _job("https://example.com/2", "ASIC Design Engineer Two"),
            _job("https://example.com/3", "ASIC Design Engineer Three"),
        ])

        r = client.post(
            "/api/linkedin/jobs/bulk-delete",
            json={"keys": ["https://example.com/1", "https://example.com/2"]},
        )
        assert r.status_code == 200
        assert r.get_json() == {"requested": 2, "deleted": 2}

        feed = client.get("/api/linkedin/jobs?time=")
        assert b"ASIC Design Engineer One" not in feed.data
        assert b"ASIC Design Engineer Two" not in feed.data
        assert b"ASIC Design Engineer Three" in feed.data

    def test_bulk_delete_empty_keys_is_noop(self, client):
        r = client.post("/api/linkedin/jobs/bulk-delete", json={})
        assert r.status_code == 200
        assert r.get_json() == {"requested": 0, "deleted": 0}

    def test_boards_bulk_delete_endpoint(self, client):
        _write_store([_job("https://github.com/example/job", "Repo Job", source="github")])
        r = client.post(
            "/api/boards/jobs/bulk-delete",
            json={"keys": ["https://github.com/example/job"]},
        )
        assert r.status_code == 200
        assert r.get_json() == {"requested": 1, "deleted": 1}


class TestLinkedInStatusUpdate:
    """Test job status PATCH endpoint."""

    def test_update_returns_html(self, client):
        # Use a key that may or may not exist — endpoint should not crash
        r = client.patch(
            "/api/linkedin/jobs/https%3A%2F%2Fexample.com%2F1/status",
            data={"status": "Tracking"},
        )
        assert r.status_code == 200

    def test_clear_status(self, client):
        r = client.patch(
            "/api/linkedin/jobs/https%3A%2F%2Fexample.com%2F1/status",
            data={"status": ""},
        )
        assert r.status_code == 200


class TestScanAPI:
    """Test scanner API endpoints."""

    def test_scan_status_when_idle(self, client):
        r = client.get("/api/scan/status")
        assert r.status_code == 200

    def test_stats_endpoint(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "total" in data
