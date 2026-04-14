"""AI-powered job relevance scoring using OpenAI GPT-4o-mini."""

import json
import os
from pathlib import Path


SCORE_PROMPT = """You are a job relevance scorer. Given a candidate profile and a job posting, rate how relevant this job is to the candidate on a scale of 0-10.

## Candidate Profile
{profile}

## Hard Reject — score MUST be 0
Give a score of 0 if ANY of these apply:
- The job does NOT sponsor visas (e.g., "no sponsorship", "must be authorized to work without sponsorship", "US citizen only", "clearance required")
- The job requires more than 2 years of experience (e.g., "3+ years", "4-6 years", "senior level")

## Scoring Guide (only if no hard reject)
- 10: Perfect match — role, skills, level, and visa all align
- 7-9: Strong match — most criteria met, minor gaps
- 4-6: Partial match — some relevant skills but role/level mismatch
- 1-3: Poor match — wrong field, wrong level, or missing key requirements

## Job Posting
Title: {title}
Company: {company}
Location: {location}
Description: {description}

## Instructions
Return ONLY valid JSON, nothing else:
{{"score": <0-10>, "reason": "<one sentence explaining why>"}}"""


def _load_profile(config_root: Path | None = None) -> str:
    """Load the user profile from config/profile.txt."""
    if config_root:
        profile_path = config_root / "config" / "profile.txt"
    else:
        profile_path = Path(__file__).parent.parent / "config" / "profile.txt"
    if profile_path.exists():
        return profile_path.read_text().strip()
    return ""


def _get_client():
    """Create OpenAI client. Returns None if unavailable."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=api_key)
    except ImportError:
        return None


def score_single_job(client, profile: str, job: dict) -> dict | None:
    """Score a single job with GPT-4o-mini. Returns {"ai_score": int, "ai_reason": str} or None."""
    prompt = SCORE_PROMPT.format(
        profile=profile,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        description=job.get("description_preview", "")[:1500],
    )
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100,
        )
        text = response.choices[0].message.content.strip()
        # Parse JSON — handle markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
        score = max(0, min(10, int(result.get("score", 5))))
        reason = str(result.get("reason", ""))[:200]
        return {"ai_score": score, "ai_reason": reason}
    except Exception:
        return None


def ai_score_jobs(jobs: list[dict], config_root: Path | None = None) -> list[dict]:
    """Score a list of job dicts with GPT-4o-mini. Modifies jobs in-place.

    Adds ai_score (1-10) and ai_reason to each job.
    Silently skips if OPENAI_API_KEY is not set or openai is not installed.
    """
    client = _get_client()
    if not client:
        return jobs

    profile = _load_profile(config_root)
    if not profile:
        return jobs

    scored = 0
    for job in jobs:
        # Skip if already scored
        if job.get("ai_score"):
            continue
        result = score_single_job(client, profile, job)
        if result:
            job["ai_score"] = result["ai_score"]
            job["ai_reason"] = result["ai_reason"]
            scored += 1

    return jobs
