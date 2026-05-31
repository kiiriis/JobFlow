"""Tests for local Codex AI scoring helpers."""

from scripts.ai_score_local import is_blocked_staffing_source


def _row(url="", company="", title="Software Engineer", location="US", desc="Python role"):
    return (url, company, title, location, desc, None)


def test_blocked_staffing_sources_match_company_and_url():
    assert is_blocked_staffing_source(_row(url="https://jobright.ai/jobs/123"))
    assert is_blocked_staffing_source(_row(company="RemoteHunter"))
    assert is_blocked_staffing_source(_row(company="Quik Hire Staffing"))
    assert is_blocked_staffing_source(_row(company="Beacon FIre"))
    assert is_blocked_staffing_source(_row(company="BeaconFire Inc."))
    assert is_blocked_staffing_source(_row(company="Helic & Co."))
    assert is_blocked_staffing_source(_row(company="Helic and Co"))
    assert is_blocked_staffing_source(_row(company="Jobs Via DIce"))


def test_legitimate_company_is_not_blocked():
    assert not is_blocked_staffing_source(_row(company="Stripe", url="https://stripe.com/jobs/123"))
