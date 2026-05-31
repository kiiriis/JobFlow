"""Tests for Milan's hardware-focused scanner pre-filter."""

from jobflow.scanner import LINKEDIN_SEARCH_TERMS, _is_swe_role


def test_linkedin_search_terms_are_hardware_focused():
    joined = " ".join(LINKEDIN_SEARCH_TERMS).lower()
    assert "asic" in joined
    assert "soc" in joined
    assert "fpga" in joined
    assert "physical design" in joined
    assert "software engineer" not in joined
    assert "machine learning" not in joined


def test_hardware_titles_pass_prefilter():
    passing = [
        "New Grad ASIC Design Engineer",
        "Entry Level ASIC Verification Engineer",
        "SoC Physical Design Engineer I",
        "FPGA Design Engineer",
        "GPU ASIC Engineer",
        "RTL Design Engineer",
        "VLSI Engineer",
        "Design Verification Engineer",
    ]
    assert all(_is_swe_role(title) for title in passing)


def test_non_target_titles_fail_prefilter():
    failing = [
        "Software Engineer",
        "Entry Level Backend Engineer",
        "Machine Learning Engineer",
        "Embedded Firmware Engineer",
        "Frontend Developer",
        "Data Engineer",
        "Product Manager",
    ]
    assert not any(_is_swe_role(title) for title in failing)
