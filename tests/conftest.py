"""Shared test fixtures for JobFlow test suite."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from jobflow.models import JobPosting, FilterResult


# ── Job Posting Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def new_grad_ml_job():
    """Legacy fixture name: perfect new grad ASIC job."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/123",
        title="ASIC Design Engineer - New Grad",
        company="NVIDIA",
        location="Santa Clara, CA",
        description=(
            "We're hiring new graduate ASIC Design Engineers! "
            "Requirements: RTL, Verilog, SystemVerilog, synthesis, STA, Cadence, Synopsys. "
            "0-2 years experience. Will sponsor H1B visa. "
            "Work on SoC and GPU ASIC design."
        ),
    )


@pytest.fixture
def entry_backend_job():
    """Legacy fixture name: entry-level verification job."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/456",
        title="Junior ASIC Verification Engineer",
        company="AMD",
        location="New York, NY",
        description=(
            "Entry-level design verification position. SystemVerilog, UVM, coverage, assertions. "
            "0-1 years experience preferred. "
            "Great opportunity for recent graduates."
        ),
    )


@pytest.fixture
def senior_job():
    """Senior role — should be hard rejected by title."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/789",
        title="Senior ASIC Design Engineer",
        company="Google",
        location="Seattle, WA",
        description="8+ years experience. Lead a team of engineers. RTL, SystemVerilog, synthesis.",
    )


@pytest.fixture
def overqualified_job():
    """Requires 4+ years — should be hard rejected by experience."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/101",
        title="ASIC Design Engineer",
        company="10a Labs",
        location="Remote, US",
        description=(
            "At least 3-8+ years of professional working experience as an ASIC engineer. "
            "Salary Range: $150K-$250K. RTL, SystemVerilog, synthesis, STA."
        ),
    )


@pytest.fixture
def no_sponsorship_job():
    """No visa sponsorship — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/202",
        title="ASIC Design Engineer",
        company="Acme Corp",
        location="Austin, TX",
        description=(
            "RTL design position. Verilog, SystemVerilog. "
            "Must be authorized to work in the United States without sponsorship."
        ),
    )


@pytest.fixture
def clearance_job():
    """Requires security clearance — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/303",
        title="ASIC Design Engineer",
        company="Northrop Grumman",
        location="Virginia, US",
        description=(
            "Active Top Secret/SCI clearance required. "
            "RTL design for defense systems."
        ),
    )


@pytest.fixture
def spam_company_job():
    """Job aggregator spam — should be hard rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/404",
        title="ASIC Design Engineer",
        company="Jobs via Dice",
        location="Remote",
        description="RTL design role. Great opportunity.",
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
        title="FPGA Design Engineer",
        company="Spotify",
        location="London, UK",
        description="Verilog, Vivado, FPGA. Great team.",
    )


@pytest.fixture
def mid_level_job():
    """Mid-level role — should pass but score lower."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/707",
        title="ASIC Design Engineer II",
        company="Microsoft",
        location="Redmond, WA",
        description=(
            "Engineer II position. 2-3 years experience preferred. RTL, SystemVerilog, synthesis."
        ),
    )


@pytest.fixture
def ambiguous_job():
    """No clear level signals — tests Unknown handling."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/808",
        title="ASIC Design Engineer",
        company="StartupCo",
        location="Remote, US",
        description="RTL, Verilog, SystemVerilog. Build chip features.",
    )


@pytest.fixture
def high_salary_no_entry_signals():
    """High salary ($180K) with no entry signals — should be rejected."""
    return JobPosting(
        url="https://linkedin.com/jobs/view/909",
        title="ASIC Design Engineer",
        company="FinTechCo",
        location="New York, NY",
        description=(
            "Compensation: $180,000 - $220,000 base salary. "
            "RTL, SystemVerilog, synthesis. Build trading hardware."
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
                "company": "Stripe", "title": "ASIC Design Engineer New Grad",
                "location": "SF, CA", "url": "https://example.com/1",
                "score": 45, "score_pct": 35, "level": "New Grad",
                "min_exp": 0, "max_exp": 2, "competition": 5,
                "recommended": True, "status": "",
                "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                "search_term": "New Grad ASIC Design Engineer",
                "variant": "se", "reason": "", "description_preview": "RTL Verilog",
            },
            "https://example.com/2": {
                "company": "Google", "title": "Junior ASIC Verification Engineer",
                "location": "Seattle, WA", "url": "https://example.com/2",
                "score": 35, "score_pct": 27, "level": "Entry",
                "min_exp": None, "max_exp": None, "competition": 7,
                "recommended": True, "status": "Tracking",
                "first_seen": (now - timedelta(hours=6)).isoformat(),
                "last_seen": now.isoformat(),
                "search_term": "Entry Level ASIC Verification Engineer",
                "variant": "se", "reason": "", "description_preview": "SystemVerilog UVM",
            },
            "https://example.com/3": {
                "company": "StartupCo", "title": "FPGA Design Engineer",
                "location": "Remote", "url": "https://example.com/3",
                "score": 20, "score_pct": 15, "level": "Unknown",
                "min_exp": None, "max_exp": None, "competition": 0,
                "recommended": False, "status": "Not Interested",
                "first_seen": (now - timedelta(days=5)).isoformat(),
                "last_seen": (now - timedelta(days=2)).isoformat(),
                "search_term": "Entry Level FPGA Design Engineer",
                "variant": "se", "reason": "", "description_preview": "Verilog Vivado",
            },
            "https://example.com/4": {
                "company": "Meta", "title": "SoC Physical Design Engineer",
                "location": "Menlo Park, CA", "url": "https://example.com/4",
                "score": 40, "score_pct": 31, "level": "Entry",
                "min_exp": 0, "max_exp": 2, "competition": 5,
                "recommended": True, "status": "Applied",
                "first_seen": (now - timedelta(days=10)).isoformat(),
                "last_seen": (now - timedelta(days=8)).isoformat(),
                "search_term": "New Grad Physical Design Engineer",
                "variant": "se", "reason": "", "description_preview": "STA timing closure",
            },
        },
    }


# ── Flask App Fixture ───────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask test app backed by an isolated temp JSON store."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data" / "ci"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (data_dir / "linkedin_jobs.json").write_text('{"jobs": {}, "last_updated": ""}')
    (data_dir / "scan_results.json").write_text("[]")
    (config_dir / "config.yaml").write_text(
        "\n".join([
            "resumes:",
            "  se: resumes/base/SE.tex",
            "  ml: resumes/base/ML.tex",
            "  appdev: resumes/base/AppDev.tex",
            "output_dir: data/ci",
            "csv_path: data/ci/applications.csv",
            "job_boards: config/job_boards.json",
            "resume_prompt: resumes/prompt.md",
            "",
        ])
    )
    monkeypatch.setenv("JOBFLOW_CONFIG", str(config_dir / "config.yaml"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from jobflow.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()
