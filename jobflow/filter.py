import re
from datetime import datetime, timezone

from .models import FilterResult, JobPosting

# ── Disqualifying phrases (case-insensitive) ────────────────────────────────
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
    r"must\s+be\s+authorized\s+to\s+work",
    r"work\s+authorization\s+without\s+sponsorship",
    r"eligible\s+to\s+work.*without\s+sponsorship",
]

# ── Senior / high-experience phrases ────────────────────────────────────────
SENIOR_PHRASES = [
    r"\b[4-9]\+?\s*(?:years?|yrs?)\b",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)\b",
    r"\bsenior\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bmanager\b",
    r"\bdirector\b",
]

# ── Entry-level signals ─────────────────────────────────────────────────────
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
    r"\bassociate\b",
    r"\bfirst\s+opportunity\b",
]

# ── ML/AI keywords for variant selection ────────────────────────────────────
ML_KEYWORDS = [
    r"\bmachine\s+learning\b", r"\bdeep\s+learning\b", r"\bml\s+engineer\b",
    r"\bdata\s+scien", r"\bcomputer\s+vision\b", r"\bnlp\b", r"\bllm\b",
    r"\bpytorch\b", r"\btensorflow\b", r"\bmodel\s+training\b",
]

# ── Full-stack / AppDev keywords ────────────────────────────────────────────
APPDEV_KEYWORDS = [
    r"\bfull[\s-]*stack\b", r"\bfrontend\b", r"\bfront[\s-]*end\b",
    r"\breact\b", r"\bangular\b", r"\bvue\b", r"\bnext\.?js\b",
    r"\bweb\s+developer\b", r"\bui/ux\b",
]

# ── Personal tech stack (categorized, Atriveo-style) ────────────────────────
STACK_CATEGORIES = {
    "core": {
        "python": 10, "c++": 6, "java": 4, "sql": 5, "go": 4,
    },
    "ml_ai": {
        "machine learning": 10, "deep learning": 8, "pytorch": 8, "tensorflow": 7,
        "llm": 8, "rag": 7, "langchain": 6, "hugging face": 5,
        "computer vision": 5, "nlp": 6, "transformers": 6,
    },
    "backend": {
        "distributed systems": 8, "rest": 4, "api": 4, "fastapi": 7,
        "flask": 5, "microservices": 5, "grpc": 5,
    },
    "cloud": {
        "aws": 7, "gcp": 4, "azure": 3, "lambda": 3, "ec2": 3,
    },
    "devops": {
        "docker": 5, "kubernetes": 6, "ci/cd": 4, "terraform": 4, "linux": 4,
    },
    "data": {
        "postgresql": 5, "mongodb": 4, "redis": 5, "kafka": 5,
        "spark": 4, "airflow": 4, "elasticsearch": 4,
    },
}

# Bonus when full tech combos appear together
SYNERGY_COMBOS = [
    ({"python", "fastapi", "aws"}, 10),
    ({"python", "pytorch", "aws"}, 10),
    ({"machine learning", "python", "docker"}, 8),
    ({"llm", "python", "aws"}, 10),
    ({"python", "docker", "kubernetes"}, 8),
    ({"python", "kafka", "distributed systems"}, 8),
    ({"postgresql", "redis", "api"}, 6),
]

# Practical maximum raw score for normalization
SCORE_MAX_RAW = 130

# Big tech companies (higher competition)
BIG_TECH = {
    "google", "amazon", "meta", "apple", "microsoft",
    "netflix", "uber", "airbnb", "stripe", "openai",
}

# H1B sponsorship positive signals
H1B_PREFER = [
    "h1b", "h-1b", "visa sponsorship", "will sponsor",
    "sponsorship available", "sponsorship provided", "open to sponsorship",
]


# ── Helper functions ────────────────────────────────────────────────────────

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


# ── Scoring components (adapted from Atriveo) ──────────────────────────────

def keyword_score(text: str) -> tuple[int, int]:
    """Binary presence match across all stack categories.

    Returns (raw_score, hit_count).
    """
    lower = text.lower()
    score = 0
    hits = 0
    for category in STACK_CATEGORIES.values():
        for keyword, weight in category.items():
            if keyword in lower:
                score += weight
                hits += 1
    return score, hits


def synergy_bonus(text: str) -> int:
    """Extra points when a full tech combo appears together."""
    lower = text.lower()
    bonus = 0
    for keywords, points in SYNERGY_COMBOS:
        if all(k in lower for k in keywords):
            bonus += points
    return bonus


def level_tag(title: str, description: str = "") -> str:
    """Categorize job as New Grad / Entry / Mid / Unknown."""
    text = f"{title} {description}".lower()

    # Check for explicit new grad signals first
    new_grad_patterns = [r"\bnew\s*grad", r"\buniversity\s+grad", r"\brecent\s+graduate"]
    for p in new_grad_patterns:
        if re.search(p, text):
            return "New Grad"

    # Entry-level signals
    entry_patterns = [
        r"\bentry[\s-]*level\b", r"\bjunior\b", r"\bassociate\b",
        r"\bsde[\s-]*[i1]\b", r"\bswe[\s-]*[i1]\b",
        r"\bearly\s+career\b", r"\bfirst\s+opportunity\b",
    ]
    for p in entry_patterns:
        if re.search(p, text):
            return "Entry"

    # Mid-level signals
    mid_patterns = [
        r"\bsde[\s-]*(?:ii|2)\b", r"\bswe[\s-]*(?:ii|2)\b",
        r"\bsoftware\s+engineer\s*(?:ii|2)\b", r"\bmid[\s-]*level\b",
    ]
    for p in mid_patterns:
        if re.search(p, text):
            return "Mid"

    return "Unknown"


def extract_experience(text: str) -> tuple[int | None, int | None]:
    """Parse experience requirements from JD text.

    Looks for patterns like "2-4 years", "2+ years", "minimum 3 years".
    Returns (min_exp, max_exp).
    """
    text = text.lower()

    # "X-Y years" or "X to Y years"
    m = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:\+\s*)?(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "X+ years"
    m = re.search(r"(\d+)\+\s*(?:years?|yrs?)", text)
    if m:
        val = int(m.group(1))
        return val, None

    # "minimum X years" or "at least X years"
    m = re.search(r"(?:minimum|at\s+least|min)\s*(?:of\s+)?(\d+)\s*(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), None

    # "X years of experience" (standalone)
    m = re.search(r"(\d+)\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)", text)
    if m:
        val = int(m.group(1))
        return val, val

    return None, None


def experience_score(min_exp: int | None, max_exp: int | None) -> int:
    """Score based on how well the experience requirement fits ~0-2 years.

    Sweet spot (range includes 0-2) → 10
    1-year max role              →  8
    Exactly 3-year min           →  6
    Min > 3 (too senior)         →  0
    No exp data                  →  0
    """
    if min_exp is None and max_exp is None:
        return 0
    if (min_exp is None or min_exp <= 2) and (max_exp is None or max_exp >= 2):
        return 10
    if max_exp is not None and max_exp <= 1:
        return 8
    if min_exp is not None and min_exp == 3:
        return 6
    if min_exp is not None and min_exp > 3:
        return 0
    return 4


def recency_score(iso_timestamp: str | None) -> int:
    """Freshness bonus — decays sharply after 24 hours."""
    if not iso_timestamp:
        return 0
    try:
        posted = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        hours = max((datetime.now(tz=timezone.utc) - posted).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        return 0
    if hours < 6:
        return 10
    if hours < 12:
        return 8
    if hours < 24:
        return 5
    if hours < 48:
        return 2
    return -5


def competition_estimate(company: str, hours_old: float = 0) -> int:
    """Estimate competition level (0-10). Higher = more competitive."""
    company_lower = company.lower()
    score = 0
    if any(bt in company_lower for bt in BIG_TECH):
        score += 5
    if hours_old > 48:
        score += 5
    elif hours_old > 24:
        score += 2
    return min(10, score)


def _level_points(level: str) -> int:
    """Points based on level tag."""
    return {
        "New Grad": 20,
        "Entry": 15,
        "Mid": 5,
        "Unknown": 4,
    }.get(level, 4)


# ── Main evaluation ─────────────────────────────────────────────────────────

def evaluate_job(job: JobPosting, first_seen: str | None = None) -> FilterResult:
    """Evaluate a job posting with multi-signal scoring."""
    text = f"{job.title} {job.description}"
    text_lower = text.lower()
    variant = select_variant(text_lower)

    # Hard disqualifiers: visa/citizenship/clearance
    if has_match(text_lower, DISQUALIFYING_PHRASES):
        return FilterResult(
            score=0, score_pct=0, should_apply=False,
            reason="No visa sponsorship or requires citizenship/clearance",
            resume_variant=variant, level=level_tag(job.title, job.description),
        )

    # Non-US location check
    non_us_patterns = [
        r"\bindia\b", r"\bgermany\b", r"\bfrance\b", r"\bbrazil\b",
        r"\bcanada\b", r"\bjapan\b", r"\bkorea\b", r"\bsingapore\b",
        r"\baustralia\b", r"\buk\b", r"\bunited\s+kingdom\b", r"\blondon\b",
        r"\bberlin\b", r"\bdublin\b", r"\bparis\b", r"\bamsterdam\b",
        r"\bdenmark\b", r"\bserbia\b", r"\bbelgrade\b",
        r"\btoronto\b", r"\bvancouver\b", r"\bbangalore\b",
        r"\bmumbai\b", r"\bluxembourg\b", r"\bsweden\b",
        r"\bstockholm\b", r"\bnetherlands\b", r"\bmelbourne\b",
        r"\bsydney\b", r"\bseoul\b", r"\btel\s+aviv\b", r"\bisrael\b",
    ]
    if has_match(job.location.lower(), non_us_patterns):
        return FilterResult(
            score=10, score_pct=0, should_apply=False,
            reason=f"Non-US location: {job.location}",
            resume_variant=variant, level=level_tag(job.title, job.description),
        )

    # ── Compute all score signals ──
    reasons = []

    # Keyword matching
    ks, hits = keyword_score(text)
    if ks > 0:
        reasons.append(f"Stack +{ks}")

    # Synergy bonus
    sb = synergy_bonus(text)
    if sb > 0:
        reasons.append(f"Synergy +{sb}")

    # Level
    level = level_tag(job.title, job.description)
    lp = _level_points(level)
    if level != "Unknown":
        reasons.append(f"{level} +{lp}")

    # Experience extraction and scoring
    min_exp, max_exp = extract_experience(text_lower)
    es = experience_score(min_exp, max_exp)
    if es > 0:
        reasons.append(f"Exp fit +{es}")

    # Recency
    rs = recency_score(first_seen)
    if rs != 0:
        reasons.append(f"Recency {'+' if rs > 0 else ''}{rs}")

    # Location bonus
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
    loc_score = 0
    if has_match(job.location.lower(), us_patterns):
        loc_score = 10
        reasons.append("US +10")
    else:
        loc_score = -10

    # H1B bonus
    h1b_bonus = 0
    if any(p in text_lower for p in H1B_PREFER):
        h1b_bonus = 8
        reasons.append("H1B +8")

    # Competition
    hours_old = 0.0
    if first_seen:
        try:
            posted = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            hours_old = max((datetime.now(tz=timezone.utc) - posted).total_seconds() / 3600, 0)
        except (ValueError, TypeError):
            pass
    comp = competition_estimate(job.company, hours_old)

    # Senior penalty (only if no positive level signals)
    senior_count = count_matches(text_lower, SENIOR_PHRASES)
    entry_count = count_matches(text_lower, ENTRY_LEVEL_SIGNALS)
    senior_penalty = 0
    if senior_count >= 3 and entry_count == 0:
        senior_penalty = -30
        reasons.append("Senior -30")

    # ── Aggregate raw score ──
    raw = ks + sb + lp + es + rs + loc_score + h1b_bonus + senior_penalty
    score_pct = min(100, max(0, round(raw / SCORE_MAX_RAW * 100)))

    # Clamp display score to 0-100
    score = max(0, min(100, raw))
    should_apply = score_pct >= 30
    reason = "; ".join(reasons) if reasons else "Meets basic criteria"
    if not should_apply:
        reason = f"Low match ({score_pct}%): {reason}"

    return FilterResult(
        score=score,
        score_pct=score_pct,
        should_apply=should_apply,
        reason=reason,
        resume_variant=variant,
        level=level,
        min_exp=min_exp,
        max_exp=max_exp,
        competition=comp,
        keyword_hits=hits,
    )
