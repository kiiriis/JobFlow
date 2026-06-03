"""Tests for Groq-backed AI scoring helpers."""

from jobflow.ai_scorer import AI_BLOCK_REASON, _is_blocked_ai_source, score_single_job


class _UnexpectedClient:
    @property
    def chat(self):
        raise AssertionError("blocked jobs should not call the model")


def test_blocked_ai_source_matches_jack_and_jill_variants():
    assert _is_blocked_ai_source({"company": "Jack & Jill"})
    assert _is_blocked_ai_source({"company": "Jack and Jill"})
    assert _is_blocked_ai_source({"url": "https://example.com/jack-and-jill/swe"})


def test_blocked_ai_source_scores_zero_without_model_call():
    result = score_single_job(
        _UnexpectedClient(),
        "profile",
        {"company": "Jack and Jill", "title": "Software Engineer"},
    )

    assert result == {"ai_score": 0, "ai_reason": AI_BLOCK_REASON}
