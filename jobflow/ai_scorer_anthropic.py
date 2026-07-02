"""Server-side AI scoring via the Anthropic Messages API (bring-your-own key).

Mirrors jobflow.ai_local.score_batch_* (same batch shape, same return contract:
list[dict]|None) but calls the Anthropic API with a user-supplied key instead of
a signed-in CLI. Reuses the shared prompt/parse from jobflow.ai_prompt so a
friend's API-key scores are identical to the operator's local-CLI scores.
"""

from .ai_prompt import BATCH_PROMPT, build_jobs_block, parse_scores

# User-selectable in Settings. Default Haiku 4.5 — cheapest, well-suited to batch
# relevance scoring and the right default for cost-conscious bring-your-own-key
# users. (Model ids verified via the claude-api skill.)
MODELS = {
    "haiku": ("claude-haiku-4-5", "Haiku 4.5 (cheapest)"),
    "sonnet": ("claude-sonnet-5", "Sonnet 5 (balanced)"),
    "opus": ("claude-opus-4-8", "Opus 4.8 (most capable)"),
}
ALLOWED_MODELS = {mid for mid, _ in MODELS.values()}
DEFAULT_MODEL = "claude-haiku-4-5"

# Small output (a JSON array of ≤BATCH_SIZE short objects); non-streaming is safe.
MAX_TOKENS = 4096


def normalize_model(model: str | None) -> str:
    """Clamp to a known model id, defaulting to Haiku."""
    return model if model in ALLOWED_MODELS else DEFAULT_MODEL


def score_batch(batch, profile: str, api_key: str, model: str | None = None):
    """Score a batch of jobs via the Anthropic API. Returns list[dict] or None.

    ``batch`` rows are (url, company, title, location, desc, ...) tuples — the
    same shape build_jobs_block expects. Sampling params are intentionally omitted
    (rejected by current Claude models); only model/max_tokens/messages are sent.
    """
    if not api_key:
        print("  Batch error: no Anthropic API key configured")
        return None
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(batch))
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=normalize_model(model),
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        return parse_scores(text)
    except Exception as e:
        print(f"  Batch error: anthropic API — {e}")
        return None
