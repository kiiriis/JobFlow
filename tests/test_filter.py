"""Tests for the Milan hardware-focused scoring engine."""

from datetime import datetime, timedelta, timezone

import pytest

from jobflow.filter import (
    DISQUALIFYING_PHRASES,
    H1B_PREFER,
    STACK_CATEGORIES,
    SYNERGY_COMBOS,
    TITLE_REJECT_PATTERNS,
    competition_estimate,
    count_matches,
    evaluate_job,
    experience_score,
    extract_experience,
    has_match,
    is_target_hardware_role,
    keyword_score,
    level_tag,
    recency_score,
    select_variant,
    synergy_bonus,
)
from jobflow.models import JobPosting


class TestHardRejectTitle:
    """Senior, management, and software QA titles should be rejected."""

    @pytest.mark.parametrize("title", [
        "Senior ASIC Design Engineer",
        "Staff SoC Verification Engineer",
        "Principal FPGA Engineer",
        "Lead RTL Engineer",
        "Engineering Manager",
        "Director of Silicon Engineering",
        "Solutions Architect",
        "VP of Engineering",
        "QA Engineer",
        "SDET",
        "Quality Assurance Engineer",
        "Test Automation Engineer",
    ])
    def test_title_reject(self, title):
        job = JobPosting(url="x", title=title, company="Acme", location="US", description="SystemVerilog UVM RTL")
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False
        assert "Title disqualified" in result.reason

    @pytest.mark.parametrize("title", [
        "ASIC Design Engineer - New Grad",
        "Entry Level ASIC Verification Engineer",
        "SoC Physical Design Engineer I",
        "FPGA Design Engineer",
        "GPU ASIC Engineer",
        "RTL Design Engineer",
        "VLSI Engineer",
        "Design Verification Engineer",
    ])
    def test_target_titles_pass_title_gate(self, title):
        assert is_target_hardware_role(title) is True
        job = JobPosting(url="x", title=title, company="Acme", location="Remote, US", description="SystemVerilog RTL UVM")
        result = evaluate_job(job)
        assert "Title disqualified" not in result.reason
        assert result.score > 0

    @pytest.mark.parametrize("title", [
        "Software Engineer",
        "Entry Level Backend Engineer",
        "Machine Learning Engineer",
        "Embedded Firmware Engineer",
        "Frontend Developer",
        "Data Engineer",
    ])
    def test_non_target_titles_reject(self, title):
        job = JobPosting(url="x", title=title, company="Acme", location="Remote, US", description="Python")
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False
        assert "Not a target" in result.reason


class TestHardRejectSponsorship:
    """Jobs that explicitly deny sponsorship should be rejected."""

    @pytest.mark.parametrize("phrase", [
        "We will not sponsor visas for this role.",
        "No sponsorship available.",
        "Must be authorized to work in the United States without sponsorship.",
        "US citizenship required for this position.",
        "Requires active Top Secret/SCI clearance.",
        "DoD Secret clearance required.",
        "Must be a U.S. citizen.",
        "Green card holder only.",
        "Eligible to work in the US without sponsorship.",
    ])
    def test_sponsorship_reject(self, phrase):
        job = JobPosting(
            url="x",
            title="ASIC Design Engineer",
            company="Acme",
            location="US",
            description=f"RTL SystemVerilog role. {phrase}",
        )
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False

    def test_generic_authorized_to_work_passes(self):
        job = JobPosting(
            url="x",
            title="ASIC Verification Engineer",
            company="Acme",
            location="San Jose, CA",
            description="SystemVerilog UVM role. Must be authorized to work in the United States.",
        )
        result = evaluate_job(job)
        assert result.score > 0
        assert result.should_apply is True

    def test_sponsorship_positive_passes(self):
        job = JobPosting(
            url="x",
            title="SoC Design Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="RTL Verilog synthesis role. Will sponsor H1B visa.",
        )
        result = evaluate_job(job)
        assert result.score > 0


class TestHardRejectExperienceAndLocation:
    @pytest.mark.parametrize("desc", [
        "Requires at least 5 years of experience in ASIC design.",
        "Minimum 4 years of professional experience.",
        "7+ years of hands-on RTL experience.",
        "5-8 years of experience in physical design.",
    ])
    def test_overqualified_reject(self, desc):
        job = JobPosting(url="x", title="ASIC Design Engineer", company="Acme", location="Remote, US", description=desc)
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False

    @pytest.mark.parametrize("location", [
        "London, UK", "Bangalore, India", "Berlin, Germany", "Toronto, Canada",
    ])
    def test_non_us_reject(self, location):
        job = JobPosting(url="x", title="FPGA Design Engineer", company="Acme", location=location, description="Verilog RTL")
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False
        assert "Non-US" in result.reason

    def test_high_salary_with_entry_signals_passes(self):
        job = JobPosting(
            url="x",
            title="ASIC Design Engineer - New Grad",
            company="Apple",
            location="Cupertino, CA",
            description="Compensation: $140,000. New grad position. RTL, Verilog, synthesis.",
        )
        result = evaluate_job(job)
        assert result.score > 0


class TestKeywordScoring:
    """Test keyword matching against Milan's hardware stack."""

    def test_systemverilog_uvm_match(self):
        score, hits = keyword_score("SystemVerilog UVM coverage verification")
        assert score >= 25
        assert hits >= 3

    def test_full_hardware_stack(self):
        text = "ASIC SoC RTL Verilog SystemVerilog UVM physical design STA timing closure Cadence Synopsys"
        score, hits = keyword_score(text)
        assert score > 70
        assert hits >= 9

    def test_no_match(self):
        score, hits = keyword_score("Marketing manager for sales team")
        assert score == 0
        assert hits == 0

    def test_case_insensitive(self):
        s1, _ = keyword_score("SYSTEMVERILOG verification")
        s2, _ = keyword_score("systemverilog verification")
        assert s1 == s2


class TestSynergyBonus:
    @pytest.mark.parametrize("text", [
        "SystemVerilog UVM coverage",
        "RTL Verilog synthesis",
        "physical design STA timing closure",
        "FPGA Verilog Vivado",
        "FPGA Verilog Quartus",
        "SoC RTL verification",
        "GPU ASIC RTL",
    ])
    def test_hardware_synergy(self, text):
        assert synergy_bonus(text) > 0

    def test_partial_combo_no_bonus(self):
        assert synergy_bonus("SystemVerilog UVM") == 0


class TestLevelTag:
    @pytest.mark.parametrize("title,expected", [
        ("ASIC Design Engineer - New Grad", "New Grad"),
        ("University Grad SoC Verification Engineer", "New Grad"),
        ("Recent Grad FPGA Design Engineer", "New Grad"),
        ("Entry-Level ASIC Verification Engineer", "Entry"),
        ("Junior RTL Design Engineer", "Entry"),
        ("Associate Physical Design Engineer", "Entry"),
        ("SoC Design Engineer I", "Entry"),
        ("ASIC Design Engineer II", "Mid"),
    ])
    def test_level_detection(self, title, expected):
        assert level_tag(title) == expected

    def test_unknown(self):
        assert level_tag("ASIC Design Engineer") == "Unknown"


class TestExtractExperience:
    @pytest.mark.parametrize("text,expected_min,expected_max", [
        ("2-4 years of experience", 2, 4),
        ("3+ years", 3, None),
        ("minimum 5 years", 5, None),
        ("at least 2 years of experience", 2, None),
        ("3 years of experience required", 3, 3),
        ("0-2 years preferred", 0, 2),
        ("1 to 3 years", 1, 3),
    ])
    def test_extraction(self, text, expected_min, expected_max):
        min_exp, max_exp = extract_experience(text)
        assert min_exp == expected_min
        assert max_exp == expected_max

    def test_no_experience_mentioned(self):
        assert extract_experience("Great team. SystemVerilog skills needed.") == (None, None)


class TestExperienceScore:
    def test_sweet_spot(self):
        assert experience_score(0, 2) == 10

    def test_one_year_max(self):
        assert experience_score(0, 1) == 8

    def test_three_year_min(self):
        assert experience_score(3, None) == 6

    def test_overqualified(self):
        assert experience_score(5, None) == 0
        assert experience_score(4, 8) == 0


class TestRecencyScore:
    def test_very_fresh(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        assert recency_score(ts) == 10

    def test_same_day(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(hours=8)).isoformat()
        assert recency_score(ts) == 8

    def test_old_posting(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()
        assert recency_score(ts) == -5

    def test_none_and_invalid(self):
        assert recency_score(None) == 0
        assert recency_score("not-a-date") == 0


class TestMiscScoring:
    def test_big_tech_competition(self):
        assert competition_estimate("Google", 0) >= 5

    def test_old_posting_competition(self):
        assert competition_estimate("StartupCo", 72) >= 5

    def test_variant_defaults_to_se(self):
        assert select_variant("SystemVerilog UVM RTL ASIC") == "se"

    def test_exported_constants_are_populated(self):
        assert DISQUALIFYING_PHRASES
        assert TITLE_REJECT_PATTERNS
        assert STACK_CATEGORIES
        assert SYNERGY_COMBOS
        assert H1B_PREFER
        assert count_matches("new grad ASIC", [r"\bnew[\s-]*grad"]) == 1
        assert has_match("ASIC Design Engineer", [r"\basic\b"])


class TestEvaluateJobIntegration:
    def test_new_grad_asic_design(self):
        job = JobPosting(
            url="x",
            title="ASIC Design Engineer - New Grad",
            company="NVIDIA",
            location="Santa Clara, CA",
            description=(
                "New graduate ASIC design role. RTL, Verilog, SystemVerilog, synthesis, "
                "static timing analysis, Python, Tcl. 0-2 years experience. Will sponsor H1B."
            ),
        )
        result = evaluate_job(job, first_seen=(datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat())
        assert result.should_apply is True
        assert result.score_pct >= 50
        assert result.level == "New Grad"
        assert result.resume_variant == "se"

    def test_verification_and_physical_design_pass(self):
        verification = JobPosting(
            url="x",
            title="Entry Level ASIC Verification Engineer",
            company="AMD",
            location="Austin, TX",
            description="SystemVerilog UVM coverage assertions simulation regressions. 0-2 years.",
        )
        physical = JobPosting(
            url="y",
            title="SoC Physical Design Engineer I",
            company="Apple",
            location="Cupertino, CA",
            description="Physical design STA timing closure floorplanning Innovus PrimeTime.",
        )
        assert evaluate_job(verification).score > 0
        assert evaluate_job(physical).score > 0

    def test_generic_software_and_embedded_rejected(self):
        software = JobPosting(url="x", title="Software Engineer", company="Meta", location="Menlo Park, CA", description="Python backend")
        embedded = JobPosting(url="y", title="Embedded Firmware Engineer", company="Intel", location="Hillsboro, OR", description="C firmware")
        assert evaluate_job(software).should_apply is False
        assert evaluate_job(embedded).should_apply is False

    def test_mid_level_hardware_rejected_by_experience(self):
        job = JobPosting(
            url="x",
            title="ASIC Design Engineer",
            company="Broadcom",
            location="San Jose, CA",
            description="Requires 4+ years of RTL design experience with SystemVerilog and synthesis.",
        )
        result = evaluate_job(job)
        assert result.score == 0
        assert result.should_apply is False
