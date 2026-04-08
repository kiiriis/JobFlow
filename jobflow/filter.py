import re

from .models import FilterResult, JobPosting

# Disqualifying phrases (case-insensitive)
DISQUALIFYING_PHRASES = [
    r"no\s+visa\s+sponsorship",
    r"not\s+sponsor",
    r"does\s+not\s+sponsor",
    r"will\s+not\s+sponsor",
    r"cannot\s+sponsor",
    r"unable\s+to\s+sponsor",
    r"without\s+sponsorship",
    r"u\.?s\.?\s+citizen",
    r"us\s+citizen",
    r"united\s+states\s+citizen",
    r"security\s+clearance",
    r"clearance\s+required",
    r"permanent\s+resident\s+only",
    r"green\s+card\s+required",
    r"authorized\s+to\s+work.*without.*sponsorship",
]

# Phrases suggesting too much experience required
SENIOR_PHRASES = [
    r"\b[3-9]\+?\s*(?:years?|yrs?)\b",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)\b",
    r"\bsenior\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bmanager\b",
    r"\bdirector\b",
]

# Positive signals for new grad / entry level
ENTRY_LEVEL_SIGNALS = [
    r"\bnew\s*grad",
    r"\bentry[\s-]*level\b",
    r"\bjunior\b",
    r"\b0[\s-]*[12]\s*(?:years?|yrs?)\b",
    r"\b1\+?\s*(?:years?|yrs?)\b",
    r"\bearly\s+career\b",
    r"\brecent\s+graduate\b",
    r"\buniversity\s+grad",
    r"\bsde[\s-]*[i1]\b",
    r"\bswe[\s-]*[i1]\b",
    r"\bsoftware\s+engineer\s*(?:ii|2)\b",
    r"\bsde[\s-]*(?:ii|2)\b",
    r"\bassociate\b",
    r"\bfirst\s+opportunity\b",
]

# ML/AI keywords for variant selection
ML_KEYWORDS = [
    r"\bmachine\s+learning\b", r"\bdeep\s+learning\b", r"\bml\s+engineer\b",
    r"\bdata\s+scien", r"\bcomputer\s+vision\b", r"\bnlp\b", r"\bllm\b",
    r"\bpytorch\b", r"\btensorflow\b", r"\bmodel\s+training\b",
]

# Full-stack / AppDev keywords
APPDEV_KEYWORDS = [
    r"\bfull[\s-]*stack\b", r"\bfrontend\b", r"\bfront[\s-]*end\b",
    r"\breact\b", r"\bangular\b", r"\bvue\b", r"\bnext\.?js\b",
    r"\bweb\s+developer\b", r"\bui/ux\b",
]


def count_matches(text: str, patterns: list[str]) -> int:
    count = 0
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            count += 1
    return count


def has_match(text: str, patterns: list[str]) -> bool:
    return count_matches(text, patterns) > 0


def select_variant(description: str) -> str:
    ml_score = count_matches(description, ML_KEYWORDS)
    appdev_score = count_matches(description, APPDEV_KEYWORDS)

    if ml_score > appdev_score and ml_score >= 2:
        return "ml"
    if appdev_score > ml_score and appdev_score >= 2:
        return "appdev"
    return "se"


# Krish's tech stack — keyword weights for JD matching
PERSONAL_STACK = {
    # Core languages
    "python": 7, "c++": 5, "java": 4, "sql": 3,
    # ML/AI
    "machine learning": 8, "deep learning": 7, "pytorch": 7, "tensorflow": 6,
    "llm": 7, "rag": 6, "langchain": 6, "hugging face": 5,
    "computer vision": 5, "nlp": 5, "transformers": 5,
    # Backend/Systems
    "distributed systems": 7, "rest": 5, "api": 5, "fastapi": 6,
    "microservices": 5, "docker": 5, "kubernetes": 6,
    # Cloud
    "aws": 6, "gcp": 4, "azure": 3,
    # Data
    "postgresql": 4, "mongodb": 4, "redis": 4, "kafka": 4,
}

KEYWORD_SCORE_MAX = 40  # max points from keyword matching


def keyword_score(text: str) -> int:
    """Score a job description based on keyword overlap with personal stack."""
    lower = text.lower()
    raw = 0
    for keyword, weight in PERSONAL_STACK.items():
        if keyword in lower:
            raw += weight
    # Normalize to 0-KEYWORD_SCORE_MAX range
    max_possible = sum(PERSONAL_STACK.values())
    return min(KEYWORD_SCORE_MAX, round(raw / max_possible * KEYWORD_SCORE_MAX * 2))


def evaluate_job(job: JobPosting) -> FilterResult:
    """Evaluate a job posting for relevance."""
    text = f"{job.title} {job.description}".lower()
    score = 50  # baseline
    reasons = []

    # Check disqualifying visa/citizenship phrases
    if has_match(text, DISQUALIFYING_PHRASES):
        return FilterResult(
            score=0,
            should_apply=False,
            reason="Job explicitly does not offer visa sponsorship or requires citizenship/clearance.",
            resume_variant=select_variant(text),
        )

    # Check experience level
    if has_match(text, SENIOR_PHRASES):
        score -= 30
        reasons.append("Mentions senior-level experience requirements")

    if has_match(text, ENTRY_LEVEL_SIGNALS):
        score += 30
        reasons.append("Entry-level / new grad signals found")

    # Keyword scoring — boost jobs that match personal tech stack
    has_real_description = len(job.description) > 100 and job.description != job.title
    if has_real_description:
        kw_score = keyword_score(text)
        if kw_score > 0:
            score += kw_score
            reasons.append(f"Stack match +{kw_score}")

    # Non-US location disqualifier (check location field specifically)
    non_us_patterns = [
        r"\bindia\b", r"\bgermany\b", r"\bfrance\b", r"\bbrazil\b",
        r"\bcanada\b", r"\bjapan\b", r"\bkorea\b", r"\bsingapore\b",
        r"\baustralia\b", r"\buk\b", r"\bunited\s+kingdom\b", r"\blondon\b",
        r"\bberlin\b", r"\bdublin\b", r"\bparis\b", r"\bamsterdam\b",
        r"\bdenmark\b", r"\bserbia\b", r"\bbelgrade\b", r"\bsão\s+paulo\b",
        r"\btoronto\b", r"\bvancouver\b", r"\bbangalore\b", r"\bhyperabad\b",
        r"\bgurgaon\b", r"\bmumbai\b", r"\bluxembourg\b", r"\bsweden\b",
        r"\bstockholm\b", r"\baarhus\b", r"\bnetherlands\b", r"\bmelbourne\b",
        r"\bsydney\b", r"\bseoul\b", r"\btel\s+aviv\b", r"\bisrael\b",
    ]
    loc_lower = job.location.lower()
    if has_match(loc_lower, non_us_patterns):
        return FilterResult(
            score=10,
            should_apply=False,
            reason=f"Non-US location: {job.location}",
            resume_variant=select_variant(text),
        )

    # USA location check
    us_patterns = [
        r"\bunited\s+states\b", r"\busa\b", r"\bu\.s\.\b",
        r"\bremote\b", r"\bnew\s+york\b", r"\bsan\s+francisco\b",
        r"\bseattle\b", r"\baustin\b", r"\bboston\b", r"\bchicago\b",
        r"\blos\s+angeles\b", r"\bdenver\b", r"\batlanta\b",
        r"\bsunnyvale\b", r"\bmountain\s+view\b", r"\bpalo\s+alto\b",
        r"\bsan\s+jose\b", r"\bportland\b", r"\bphiladelphia\b",
        r"\bwashington\b", r"\bdc\b", r"\braleigh\b", r"\bcharlotte\b",
        r"\bmiami\b", r"\bdallas\b", r"\bhouston\b", r"\bphoenix\b",
        r"\b[A-Z]{2}\b",
    ]
    location_text = f"{job.location}".lower()
    if has_match(location_text, us_patterns):
        score += 10
        reasons.append("US-based or remote position")
    else:
        score -= 20
        reasons.append("Location may not be in the US")

    # Clamp score
    score = max(0, min(100, score))
    should_apply = score >= 40
    variant = select_variant(text)

    reason = "; ".join(reasons) if reasons else "Meets basic criteria"
    if not should_apply:
        reason = f"Score too low ({score}/100): {reason}"

    return FilterResult(
        score=score,
        should_apply=should_apply,
        reason=reason,
        resume_variant=variant,
    )
