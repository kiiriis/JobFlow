"""Shared test fixtures for JobFlow test suite."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from jobflow.models import JobPosting, FilterResult


# ── Job Posting Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def new_grad_ml_job():
    """Perfect new grad ML job — should score very high."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/123",
        title="Machine Learning Engineer – New Grad",
        company="Stripe",
        location="San Francisco, CA",
        description=(
            "We're hiring new graduate Machine Learning Engineers! "
            "Requirements: Python, PyTorch, AWS, distributed systems, Docker, Kubernetes, FastAPI. "
            "0-2 years experience. Will sponsor H1B visa. "
            "Work on deep learning models, NLP, and LLM applications."
        ),
    )


@pytest.fixture
def entry_backend_job():
    """Entry-level backend job — should score well."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/456",
        title="Junior Software Engineer",
        company="Notion",
        location="New York, NY",
        description=(
            "Entry-level software engineering position. Python, PostgreSQL, Redis, Docker. "
            "REST API development. 0-1 years experience preferred. "
            "Great opportunity for recent graduates."
        ),
    )


@pytest.fixture
def senior_job():
    """Senior role — should be hard rejected by title."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/789",
        title="Senior Software Engineer",
        company="Google",
        location="Seattle, WA",
        description="8+ years experience. Lead a team of engineers. Python, AWS, Kubernetes.",
    )


@pytest.fixture
def overqualified_job():
    """Requires 4+ years — should be hard rejected by experience."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/101",
        title="ML Engineer",
        company="10a Labs",
        location="Remote, US",
        description=(
            "At least 3–8+ years of professional working experience as a Machine Learning engineer. "
            "Salary Range: $150K–$250K. Build and deploy ML systems. "
            "Python, PyTorch, LLMs, fine-tuning, AWS."
        ),
    )


@pytest.fixture
def no_sponsorship_job():
    """No visa sponsorship — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/202",
        title="Software Developer",
        company="Acme Corp",
        location="Austin, TX",
        description=(
            "Software developer position. Python, React. "
            "Must be authorized to work in the United States without sponsorship."
        ),
    )


@pytest.fixture
def clearance_job():
    """Requires security clearance — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/303",
        title="Software Engineer",
        company="Northrop Grumman",
        location="Virginia, US",
        description=(
            "Active Top Secret/SCI clearance required. "
            "Python developer for defense systems."
        ),
    )


@pytest.fixture
def spam_company_job():
    """Job aggregator spam — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/404",
        title="Software Developer",
        company="Jobs via Dice",
        location="Remote",
        description="Python developer needed. Great opportunity.",
    )


@pytest.fixture
def qa_job():
    """QA role — should be hard rejected by title."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/505",
        title="QA Engineer",
        company="TestCo",
        location="Chicago, IL",
        description="Selenium, test automation, quality assurance.",
    )


@pytest.fixture
def non_us_job():
    """Non-US location — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/606",
        title="Software Engineer",
        company="Spotify",
        location="London, UK",
        description="Python, microservices, Kubernetes. Great team.",
    )


@pytest.fixture
def mid_level_job():
    """Mid-level role — should pass but score lower."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/707",
        title="Software Engineer II",
        company="Microsoft",
        location="Redmond, WA",
        description=(
            "SDE II position. 2-3 years experience preferred. Python, distributed systems, AWS. "
            "Build scalable backend services."
        ),
    )


@pytest.fixture
def ambiguous_job():
    """No clear level signals — tests Unknown handling."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/808",
        title="Software Engineer",
        company="StartupCo",
        location="Remote, US",
        description="Python, Docker, PostgreSQL. Build features for our platform.",
    )


@pytest.fixture
def high_salary_no_entry_signals():
    """High salary ($180K) with no entry signals — should be rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/909",
        title="Software Engineer",
        company="FinTechCo",
        location="New York, NY",
        description=(
            "Compensation: $180,000 - $220,000 base salary. "
            "Python, Kafka, distributed systems. Build trading platform."
        ),
    )


# ── Time Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def recent_timestamp():
    """Timestamp from 2 hours ago."""
    return (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()


@pytest.fixture
def old_timestamp():
    """Timestamp from 3 days ago."""
    return (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()


# ── Store Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def empty_store():
    return {"jobs": {}, "last_updated": ""}


@pytest.fixture
def sample_store():
    """Store with a mix of jobs for testing filters."""
    now = datetime.now(tz=timezone.utc)
    return {
        "last_updated": now.isoformat(),
        "jobs": {
            "https://example.com/1": {
                "company": "Stripe", "title": "SWE New Grad",
                "location": "SF, CA", "url": "https://example.com/1",
                "score": 45, "score_pct": 35, "level": "New Grad",
                "min_exp": 0, "max_exp": 2, "competition": 5,
                "recommended": True, "status": "",
                "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                "search_term": "new grad software engineer",
                "variant": "se", "reason": "", "description_preview": "",
            },
            "https://example.com/2": {
                "company": "Google", "title": "Junior Backend Engineer",
                "location": "Seattle, WA", "url": "https://example.com/2",
                "score": 35, "score_pct": 27, "level": "Entry",
                "min_exp": None, "max_exp": None, "competition": 7,
                "recommended": True, "status": "Tracking",
                "first_seen": (now - timedelta(hours=6)).isoformat(),
                "last_seen": now.isoformat(),
                "search_term": "entry level software engineer",
                "variant": "se", "reason": "", "description_preview": "",
            },
            "https://example.com/3": {
                "company": "StartupCo", "title": "Python Developer",
                "location": "Remote", "url": "https://example.com/3",
                "score": 20, "score_pct": 15, "level": "Unknown",
                "min_exp": None, "max_exp": None, "competition": 0,
                "recommended": False, "status": "Not Interested",
                "first_seen": (now - timedelta(days=5)).isoformat(),
                "last_seen": (now - timedelta(days=2)).isoformat(),
                "search_term": "python developer",
                "variant": "se", "reason": "", "description_preview": "",
            },
            "https://example.com/4": {
                "company": "Meta", "title": "ML Engineer",
                "location": "Menlo Park, CA", "url": "https://example.com/4",
                "score": 40, "score_pct": 31, "level": "Entry",
                "min_exp": 0, "max_exp": 2, "competition": 5,
                "recommended": True, "status": "Applied",
                "first_seen": (now - timedelta(days=10)).isoformat(),
                "last_seen": (now - timedelta(days=8)).isoformat(),
                "search_term": "ml engineer",
                "variant": "ml", "reason": "", "description_preview": "",
            },
        },
    }


# ── Flask App Fixture ───────────────────────────────────────────────────────

@pytest.fixture
def app():
    """Flask test app."""
    from jobflow.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()
