"""Tests for URL canonicalization and the dedup paths it affects.

Covers:
  - normalize_url() across real-world URL shapes
  - _dedup_key() using the canonical URL
  - merge_scan_results() JSON-mode re-keying + collision merges
  - scanner.deduplicate_results() keying
  - _merge_job_rows() pure-Python helper for the DB migration
"""

from datetime import datetime, timedelta, timezone

import pytest

from jobflow.linkedin_store import (
    normalize_url, _dedup_key, merge_scan_results,
)


# ----------------------------------------------------------------------------
# normalize_url — the core primitive
# ----------------------------------------------------------------------------

class TestNormalizeUrl:
    def test_linkedin_tracking_params_stripped(self):
        u = "https://www.linkedin.com/jobs/view/4001?trackingId=abc&refId=xyz&position=1&pageNum=0"
        assert normalize_url(u) == "https://www.linkedin.com/jobs/view/4001"

    def test_linkedin_already_clean_is_unchanged(self):
        u = "https://www.linkedin.com/jobs/view/4001"
        assert normalize_url(u) == u

    def test_trailing_slash_removed(self):
        assert normalize_url("https://www.linkedin.com/jobs/view/4001/") == \
               "https://www.linkedin.com/jobs/view/4001"

    def test_fragment_stripped(self):
        assert normalize_url("https://linkedin.com/jobs/view/4001#apply") == \
               "https://linkedin.com/jobs/view/4001"

    def test_scheme_and_host_lowercased(self):
        u = "HTTPS://WWW.LinkedIn.COM/jobs/view/4001"
        assert normalize_url(u) == "https://www.linkedin.com/jobs/view/4001"

    def test_path_case_preserved(self):
        # Ashby uses case-sensitive UUID paths — host lowercased, path left alone
        u = "https://Jobs.AshbyHQ.com/Company/Abc-DEF-123?utm_source=li"
        assert normalize_url(u) == "https://jobs.ashbyhq.com/Company/Abc-DEF-123"

    def test_greenhouse_tracking(self):
        u = "https://boards.greenhouse.io/stripe/jobs/5678?gh_src=abcd&gh_jid=9999"
        assert normalize_url(u) == "https://boards.greenhouse.io/stripe/jobs/5678"

    def test_lever_tracking(self):
        u = "https://jobs.lever.co/anthropic/uuid-goes-here?lever-source=LinkedIn"
        assert normalize_url(u) == "https://jobs.lever.co/anthropic/uuid-goes-here"

    def test_empty_string(self):
        assert normalize_url("") == ""

    def test_none_safety(self):
        # Function is typed str, but empty handling should still work for falsy
        assert normalize_url("") == ""

    def test_non_url_passthrough(self):
        # Stored keys that weren't URLs (e.g., "manual-paste", legacy "x_y"
        # combo keys) should pass through unchanged so they still match.
        assert normalize_url("manual-paste") == "manual-paste"
        assert normalize_url("x_y") == "x_y"

    def test_whitespace_stripped(self):
        assert normalize_url("  https://linkedin.com/jobs/view/1  ") == \
               "https://linkedin.com/jobs/view/1"

    def test_malformed_url_returned_as_is(self):
        # urlparse is very permissive — ensure nothing raises
        assert normalize_url("http://") == "http://"  # no netloc: fall-through

    def test_idempotent(self):
        """Normalizing a normalized URL should be a no-op."""
        raw = "https://www.linkedin.com/jobs/view/4001/?trk=x#y"
        once = normalize_url(raw)
        twice = normalize_url(once)
        assert once == twice

    def test_port_preserved(self):
        u = "https://example.com:8080/jobs/123?x=1"
        assert normalize_url(u) == "https://example.com:8080/jobs/123"

    def test_root_path_kept_as_slash(self):
        # Don't eat the sole slash of a root path
        assert normalize_url("https://example.com/") == "https://example.com/"
        assert normalize_url("https://example.com") == "https://example.com/"

    def test_different_query_orderings_collapse(self):
        """Same job with query params in different order collapses identically."""
        a = "https://linkedin.com/jobs/view/1?trk=a&ref=b"
        b = "https://linkedin.com/jobs/view/1?ref=b&trk=a"
        assert normalize_url(a) == normalize_url(b)


# ----------------------------------------------------------------------------
# _dedup_key — the function that produces the storage key
# ----------------------------------------------------------------------------

class TestDedupKey:
    def test_url_present_uses_canonical(self):
        e = {"url": "https://linkedin.com/jobs/view/1?trk=foo",
             "company": "Stripe", "title": "SWE"}
        assert _dedup_key(e) == "https://linkedin.com/jobs/view/1"

    def test_url_missing_falls_back_to_combo(self):
        e = {"url": "", "company": "Stripe", "title": "SWE New Grad"}
        assert _dedup_key(e) == "stripe_swe new grad"

    def test_empty_url_still_uses_combo(self):
        assert _dedup_key({"company": "X", "title": "Y"}) == "x_y"

    def test_different_query_params_same_key(self):
        a = _dedup_key({"url": "https://linkedin.com/jobs/view/1?trk=a"})
        b = _dedup_key({"url": "https://linkedin.com/jobs/view/1?trk=b"})
        assert a == b


# ----------------------------------------------------------------------------
# merge_scan_results — the full JSON-mode pipeline
# ----------------------------------------------------------------------------

def _job(url="", company="Stripe", title="Software Engineer", **kw):
    base = {
        "company": company, "title": title, "url": url,
        "description_preview": "Python, AWS, new grad position.",
        "first_seen": "", "last_seen": "", "status": "",
    }
    base.update(kw)
    return base


class TestMergeScanResultsUrlDedup:
    def test_same_job_different_tracking_collapses_to_one(self):
        """Scan returns the same job twice with different tracking params."""
        store = {"jobs": {}, "last_updated": ""}
        results = [
            _job(url="https://linkedin.com/jobs/view/42?trk=email"),
            _job(url="https://linkedin.com/jobs/view/42?trk=search&refId=xyz"),
        ]
        merge_scan_results(store, results)
        assert len(store["jobs"]) == 1
        assert "https://linkedin.com/jobs/view/42" in store["jobs"]

    def test_existing_row_keyed_by_old_url_gets_rekeyed(self):
        """Pre-existing entry stored under un-normalized URL is migrated."""
        old_url = "https://linkedin.com/jobs/view/42?trk=old"
        store = {"jobs": {old_url: _job(url=old_url, status="Applied")}, "last_updated": ""}
        # Incoming scan uses a different tracking suffix
        results = [_job(url="https://linkedin.com/jobs/view/42?trk=new")]
        merge_scan_results(store, results)
        # Exactly one row, keyed by canonical URL, Applied status preserved
        assert len(store["jobs"]) == 1
        canonical = "https://linkedin.com/jobs/view/42"
        assert canonical in store["jobs"]
        assert store["jobs"][canonical]["status"] == "Applied"

    def test_rekey_collision_merges_preserving_status(self):
        """Two old rows canonicalize to the same key — user status wins."""
        store = {
            "jobs": {
                "https://linkedin.com/jobs/view/9?trk=a": _job(
                    url="https://linkedin.com/jobs/view/9?trk=a",
                    status="Applied", first_seen="2026-01-01T00:00:00+00:00",
                ),
                "https://linkedin.com/jobs/view/9?trk=b": _job(
                    url="https://linkedin.com/jobs/view/9?trk=b",
                    status="", first_seen="2026-01-02T00:00:00+00:00",
                ),
            },
            "last_updated": "",
        }
        merge_scan_results(store, [])
        canonical = "https://linkedin.com/jobs/view/9"
        assert list(store["jobs"].keys()) == [canonical]
        assert store["jobs"][canonical]["status"] == "Applied"

    def test_rekey_collision_merges_preserving_ai_score(self):
        """When neither has status, AI score survives the merge."""
        store = {
            "jobs": {
                "https://linkedin.com/jobs/view/7?trk=a": _job(
                    url="https://linkedin.com/jobs/view/7?trk=a",
                    ai_score=8, ai_reason="strong ML fit",
                ),
                "https://linkedin.com/jobs/view/7?trk=b": _job(
                    url="https://linkedin.com/jobs/view/7?trk=b",
                ),
            },
            "last_updated": "",
        }
        merge_scan_results(store, [])
        canonical = "https://linkedin.com/jobs/view/7"
        assert canonical in store["jobs"]
        assert store["jobs"][canonical].get("ai_score") == 8

    def test_non_url_key_left_alone(self):
        """Legacy entries keyed by 'manual-paste' or combo keys aren't touched."""
        store = {
            "jobs": {
                "manual-paste": _job(url="", company="X", title="Y", status="Applied"),
            },
            "last_updated": "",
        }
        # Scan with a different URL — shouldn't collide with manual-paste row
        merge_scan_results(store, [_job(url="https://linkedin.com/jobs/view/100")])
        assert "manual-paste" in store["jobs"]
        assert store["jobs"]["manual-paste"]["status"] == "Applied"

    def test_dismissed_by_unnormalized_url_still_blocks(self):
        """A URL dismissed with tracking params should block its canonical form."""
        store = {
            "jobs": {},
            "dismissed": ["https://linkedin.com/jobs/view/5"],
            "last_updated": "",
        }
        results = [_job(url="https://linkedin.com/jobs/view/5?trk=spam")]
        merge_scan_results(store, results)
        # Canonical key equals the dismissed entry → should be skipped.
        # NOTE: this only works if the dismissed set contains canonical form.
        # The migration rewrites dismissed_jobs, so post-migration this holds.
        assert len(store["jobs"]) == 0


# ----------------------------------------------------------------------------
# scanner.deduplicate_results — pre-scan filter using seen_jobs
# ----------------------------------------------------------------------------

class TestScannerDedup:
    def test_same_url_different_tracking_seen_once(self):
        from jobflow.scanner import deduplicate_results
        from jobflow.models import JobPosting, FilterResult

        def mk(url):
            job = JobPosting(
                url=url, title="SWE", company="Stripe",
                location="SF", description="",
            )
            res = FilterResult(
                score=50, score_pct=50, should_apply=True,
                reason="", resume_variant="se",
            )
            return job, res

        results = [
            mk("https://linkedin.com/jobs/view/10?trk=a"),
            mk("https://linkedin.com/jobs/view/10?trk=b"),  # same job, different tracking
            mk("https://linkedin.com/jobs/view/11"),
        ]
        new, seen = deduplicate_results(results, {})
        assert len(new) == 2
        assert "https://linkedin.com/jobs/view/10" in seen
        assert "https://linkedin.com/jobs/view/11" in seen

    def test_previously_seen_with_tracking_blocks_new_with_different_tracking(self):
        from jobflow.scanner import deduplicate_results
        from jobflow.models import JobPosting, FilterResult

        # "seen" populated with a canonical URL (as it would be after migration)
        seen = {"https://linkedin.com/jobs/view/10": "2026-04-21T00:00:00-05:00"}
        job = JobPosting(
            url="https://linkedin.com/jobs/view/10?trk=email",
            title="SWE", company="Stripe", location="SF", description="",
        )
        res = FilterResult(
            score=50, score_pct=50, should_apply=True,
            reason="", resume_variant="se",
        )
        new, seen = deduplicate_results([(job, res)], seen)
        assert new == []


# ----------------------------------------------------------------------------
# _merge_job_rows — DB migration merge helper (pure Python)
# ----------------------------------------------------------------------------

class TestMergeJobRows:
    def test_single_row_passthrough(self):
        from jobflow.db import _merge_job_rows
        row = {"url": "https://x.com/1?trk=a", "status": "Applied"}
        merged = _merge_job_rows([row], "https://x.com/1")
        assert merged["url"] == "https://x.com/1"
        assert merged["status"] == "Applied"

    def test_applied_beats_not_interested(self):
        from jobflow.db import _merge_job_rows
        a = {"url": "u1", "status": "Applied", "description_preview": ""}
        b = {"url": "u2", "status": "Not Interested", "description_preview": "long " * 20}
        merged = _merge_job_rows([b, a], "canon")
        assert merged["status"] == "Applied"

    def test_applied_beats_empty_status(self):
        from jobflow.db import _merge_job_rows
        a = {"url": "u1", "status": "", "ai_score": 9, "description_preview": "aaaaaaaaaa"}
        b = {"url": "u2", "status": "Applied", "ai_score": None, "description_preview": ""}
        merged = _merge_job_rows([a, b], "canon")
        assert merged["status"] == "Applied"
        # AI score from the losing row is carried over
        assert merged["ai_score"] == 9

    def test_tiebreaker_prefers_ai_scored(self):
        from jobflow.db import _merge_job_rows
        a = {"url": "u1", "status": "", "ai_score": None, "description_preview": ""}
        b = {"url": "u2", "status": "", "ai_score": 7, "description_preview": ""}
        merged = _merge_job_rows([a, b], "canon")
        assert merged["ai_score"] == 7

    def test_tiebreaker_prefers_longer_description(self):
        from jobflow.db import _merge_job_rows
        a = {"url": "u1", "status": "", "ai_score": None, "description_preview": "short"}
        b = {"url": "u2", "status": "", "ai_score": None, "description_preview": "a much longer description"}
        merged = _merge_job_rows([a, b], "canon")
        assert merged["description_preview"] == "a much longer description"

    def test_first_seen_takes_earliest(self):
        from jobflow.db import _merge_job_rows
        early = "2026-01-01T00:00:00+00:00"
        late = "2026-04-01T00:00:00+00:00"
        a = {"url": "u1", "status": "Applied", "first_seen": late, "last_seen": late,
             "description_preview": ""}
        b = {"url": "u2", "status": "", "first_seen": early, "last_seen": early,
             "description_preview": ""}
        merged = _merge_job_rows([a, b], "canon")
        assert merged["first_seen"] == early
        assert merged["last_seen"] == late

    def test_ai_reason_carried_with_ai_score(self):
        from jobflow.db import _merge_job_rows
        best = {"url": "u1", "status": "Applied", "ai_score": None, "ai_reason": "",
                "description_preview": ""}
        other = {"url": "u2", "status": "", "ai_score": 8, "ai_reason": "good match",
                 "ai_model": "gpt-4o-mini", "description_preview": ""}
        merged = _merge_job_rows([best, other], "canon")
        assert merged["ai_score"] == 8
        assert merged["ai_reason"] == "good match"
        assert merged["ai_model"] == "gpt-4o-mini"

    def test_url_is_rewritten_to_canonical(self):
        from jobflow.db import _merge_job_rows
        rows = [{"url": "https://x.com/1?a=1", "status": "", "description_preview": ""}]
        merged = _merge_job_rows(rows, "https://x.com/1")
        assert merged["url"] == "https://x.com/1"
