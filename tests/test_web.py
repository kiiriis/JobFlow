"""Tests for jobflow/web — Flask routes and API endpoints."""

import json
import re
import pytest


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
        r = client.get("/api/linkedin/jobs?level=Entry&q=python&sort=score_pct&dir=desc&tz=240")
        assert r.status_code == 200


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
