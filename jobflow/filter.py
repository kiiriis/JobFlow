"""Multi-signal job scoring engine with a hard-reject pipeline.

This is the core evaluation logic that decides whether a job is worth applying
to. Every scanned job passes through evaluate_job(), which works in two phases:

Phase 1 — Hard Reject (instant score=0, should_apply=False):
    Six ordered checks that disqualify a job immediately:
    1. Company blocklist    — spam aggregators (Dice, Turing, Jobot, etc.)
    2. Title patterns       — senior/staff/principal/lead/QA/architect/VP
    3. Sponsorship phrases  — 47 patterns catching "no sponsorship", citizenship, clearance
    4. Non-US location      — regex matching international cities/countries
    5. Overqualified exp    — min_exp >= 4 years or "5-8 years" patterns
    6. Senior salary        — $130K+ with zero entry-level signals in the JD

    Hard rejects are ordered by cheapest-to-check first. Sponsorship phrases
    use plain string matching (_has_phrase) instead of regex for speed since
    this runs on every job.

Phase 2 — Scoring (if all hard filters pass):
    Additive scoring across multiple signals, normalized to 0-100%:
    - keyword_score:    Tech stack matches (Python=10, PyTorch=8, AWS=7, etc.)
    - synergy_bonus:    Extra points for common stack combos (Python+FastAPI+AWS = +10)
    - level_points:     New Grad=+20, Entry=+15, Mid=+5, Unknown=+4
    - experience_score: Sweet spot is 0-2 years (+10), penalty for 3+ years
    - recency_score:    Freshly posted jobs get +10, >48h old gets -5
    - loc_score:        US location = +10, non-US = -10
    - h1b_bonus:        Mentions visa sponsorship = +8
    - senior_penalty:   3+ senior signals with 0 entry signals = -30

    Raw score (max ~130) is normalized: score_pct = raw / 130 * 100
    should_apply = True (all jobs passing hard rejects are kept for AI scoring)

Variant Selection:
    select_variant() picks which base resume to use:
    - "ml"     if 2+ ML keywords (pytorch, tensorflow, llm, etc.)
    - "appdev" if 2+ frontend keywords (react, angular, full-stack, etc.)
    - "se"     default (backend/infra/general SWE)
"""

import re
from datetime import datetime, timezone

from .models import FilterResult, JobPosting

# ── Sponsorship / Citizenship / Clearance — hard reject (case-insensitive) ──
# These are plain strings (not regex) checked via `phrase in text` for speed.
# Covers three categories: sponsorship denial, citizenship requirements, and
# security clearance requirements. Any match = immediate disqualification
# because the user needs visa sponsorship (F1/OPT).
DISQUALIFYING_PHRASES = [
    # Sponsorship rejection
    "no sponsorship", "no visa sponsorship", "no need for visa sponsorship",
    "will not sponsor", "cannot sponsor", "does not sponsor",
    "not sponsor", "unable to sponsor", "not able to sponsor",
    "not offer sponsorship", "not offering sponsorship",
    "sponsorship not available", "sponsorship is not available",
    "sponsorship: not available", "not available for this role",
    "sponsorship not provided", "sponsorship is not provided",
    "no visa", "visa: not available",
    "without sponsorship", "without the need for sponsorship",
    "without requiring sponsorship",
    "work authorization without sponsorship",
    "work in the us without sponsorship",
    # Citizenship / permanent residency
    "us citizen only", "u.s. citizen only", "citizens only",
    "must be a us citizen", "must be a u.s. citizen",
    "must be a united states citizen",
    "us citizenship required", "u.s. citizenship required",
    "united states citizenship required", "citizenship is required",
    "gc holder", "green card holder", "green card only",
    "permanent resident only", "lawful permanent resident",
    "must be authorized to work", "must have authorization to work",
    "authorization to work in the us",
    "authorization to work in the united states",
    "authorized to work in the us without sponsorship",
    "authorized to work in the united states without sponsorship",
    "eligible to work in the us without sponsorship",
    "legally authorized to work",
    # Security clearance
    "security clearance required", "clearance required",
    "active clearance", "active secret clearance", "active top secret",
    "top secret clearance", "top-secret clearance",
    "ts/sci", "ts sci", "top secret/sci",
    "sci eligibility", "sci access",
    "dod clearance", "dod secret", "government clearance",
    "public trust clearance", "secret clearance",
    "must have clearance", "must hold clearance",
    "ability to obtain a clearance", "ability to obtain and maintain",
    "obtain a government clearance", "obtain a security clearance",
]

# ── Overqualified experience patterns — hard reject ─────────────────────────
# Regex patterns that catch "4+ years", "5-8 years", "minimum 4 years", etc.
# These fire BEFORE the numeric extract_experience() check as a safety net
# for unusual phrasing that the parser might miss.
OVERQUALIFIED_PATTERNS = [
    r"\b[4-9]\+?\s*(?:years?|yrs?)\s+(?:\w+\s+){0,5}experience",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)",
    r"(?:minimum|at\s+least|requires?)\s+(?:of\s+)?[4-9]\+?\s*(?:years?|yrs?)",
    r"\b[4-9]\s*(?:or\s+more)\s*(?:years?|yrs?)",
    r"\b[4-9]\s*[-–to]+\s*[5-9]\d?\s*\+?\s*(?:years?|yrs?)",
    r"\b[3-9]\s*[-–]\s*[8-9]\+?\s*(?:years?|yrs?)",
]

# ── Title-level hard reject patterns ────────────────────────────────────────
# Matched against the job TITLE only (not description) to avoid false positives
# from phrases like "work with senior engineers" in JD body text.
TITLE_REJECT_PATTERNS = [
    r"\bsenior\b", r"\bsr\.?\s", r"\bstaff\b", r"\bprincipal\b",
    r"\blead\s+(?:engineer|developer|software)\b",
    r"\bmanager\b", r"\bdirector\b",
    r"\barchitect\b", r"\bvp\b", r"\bvice\s+president\b", r"\bhead\s+of\b",
    # QA / Testing roles
    r"\bsdet\b", r"\bqa\s+engineer\b", r"\bquality\s+assurance\b",
    r"\bquality\s+engineer\b",
    r"\btest\s+(?:automation|engineer)\b", r"\btesting\s+engineer\b",
]

# ── Company blocklist (job aggregators / spam) ──────────────────────────────
# These companies are staffing agencies or aggregators that repost jobs
# from other companies. Applying through them adds no value.
COMPANY_BLOCKLIST = {
    "dice", "remotehunter", "jobs via dice", "jobot", "cybercoders",
    "lancesoft", "haystack", "turing", "micro1", "hackajob",
    "crossover", "toptal", "andela", "revelo", "tira",
}

# ── Senior salary pattern (>= $130K suggests non-entry) ────────────────────
# A salary floor of $130K+ without any entry-level signals strongly indicates
# a mid/senior role, even if the title doesn't say "Senior". This catches
# roles like "Software Engineer" that pay $150K-$200K (clearly not new grad).
SENIOR_SALARY_PATTERN = r"\$1[3-9]\d[,.]?\d{3}|\$[2-9]\d\d[,.]?\d{3}"

# ── Entry-level signals ─────────────────────────────────────────────────────
# Used as a counterbalance: if a job has senior salary BUT also has these
# entry signals, it's not hard-rejected (could be a well-paying entry role).
# Also used in the soft senior_penalty calculation.
ENTRY_LEVEL_SIGNALS = [
    r"\bnew[\s-]*grad", r"\bnew[\s-]*graduate\b", r"\bcampus\s+hire\b",
    r"\bentry[\s-]*level\b",
    r"\bjunior\b", r"\bjr\.?\s", r"\bjr\s+",
    r"\b0[\s-]*[12]\s*(?:years?|yrs?)\b",
    r"\b0\s+to\s+2\s*(?:years?|yrs?)\b",
    r"\b1\+?\s*(?:years?|yrs?)\b",
    r"\bearly\s+career\b",
    r"\brecent\s+graduate?\b", r"\brecent\s+grad\b",
    r"\buniversity\s+grad",
    r"\bsde[\s-]*[i1]\b", r"\bswe[\s-]*[i1]\b", r"\beng[\s-]*[i1]\b",
    r"\blevel[\s-]*[i1]\b", r"\blevel[\s-]*1\b",
    r"\bassociate\b",
    r"\bfirst\s+opportunity\b",
    r"\bintern\b",
]

# ── Description-level senior signals (soft penalty) ─────────────────────────
# Unlike TITLE_REJECT_PATTERNS (hard reject), these are checked in the JD body
# and only apply a -30 penalty if 3+ match with zero entry signals. This
# handles JDs that mention "3+ years" once casually vs. those that repeatedly
# emphasize senior requirements.
SENIOR_DESC_SIGNALS = [
    r"\b[3-9]\+?\s*(?:years?|yrs?)\b",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)\b",
    r"\bsenior\b", r"\bstaff\b", r"\bprincipal\b",
    r"\blead\b", r"\bmanager\b", r"\bdirector\b",
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

# ── Personal tech stack (categorized) ───────────────────────────────────────
# Weights reflect how central each technology is to the user's skill set.
# Higher weight = stronger match signal. Categories are used for organization
# only — scoring sums across all categories.
#
# Scoring is binary presence: if "python" appears anywhere in title+description,
# add 10 points. No frequency weighting — mentioning Python 5 times doesn't
# score higher than mentioning it once.
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

# Bonus when full tech combos appear together.
# Synergy rewards jobs that match the user's actual project experience
# (e.g., "Python + FastAPI + AWS" = a real stack the user has built with).
# All keywords in the combo must be present to earn the bonus.
SYNERGY_COMBOS = [
    ({"python", "fastapi", "aws"}, 10),
    ({"python", "pytorch", "aws"}, 10),
    ({"machine learning", "python", "docker"}, 8),
    ({"llm", "python", "aws"}, 10),
    ({"python", "docker", "kubernetes"}, 8),
    ({"python", "kafka", "distributed systems"}, 8),
    ({"postgresql", "redis", "api"}, 6),
]

# Maximum theoretical raw score. Used to normalize to 0-100%.
# Approximate breakdown: stack ~50 + synergy ~10 + level 20 + exp 10 + recency 10
# + location 10 + h1b 8 + misc ≈ 130
SCORE_MAX_RAW = 130

# Big Tech companies get a +5 competition score — more applicants means
# the user should prioritize these applications (higher urgency, not penalty).
BIG_TECH = {
    "google", "amazon", "meta", "apple", "microsoft",
    "netflix", "uber", "airbnb", "stripe", "openai",
}

# Positive sponsorship signals — if the JD explicitly mentions visa sponsorship,
# the job gets a +8 bonus since the user needs F1/OPT sponsorship.
H1B_PREFER = [
    "h1b", "h-1b", "visa sponsorship", "will sponsor",
    "sponsorship available", "sponsorship provided", "open to sponsorship",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def count_matches(text: str, patterns: list[str]) -> int:
    count = 0
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            count += 1
    return count


def has_match(text: str, patterns: list[str]) -> bool:
    return count_matches(text, patterns) > 0


def _has_phrase(text_lower: str, phrases: list[str]) -> bool:
    """Fast plain-string match for disqualifying phrases."""
    for phrase in phrases:
        if phrase in text_lower:
            return True
    return False


def select_variant(description: str) -> str:
    ml_score = count_matches(description, ML_KEYWORDS)
    appdev_score = count_matches(description, APPDEV_KEYWORDS)
    if ml_score > appdev_score and ml_score >= 2:
        return "ml"
    if appdev_score > ml_score and appdev_score >= 2:
        return "appdev"
    return "se"


# ── Scoring components ──────────────────────────────────────────────────────

def keyword_score(text: str) -> tuple[int, int]:
    """Binary presence match across all stack categories. Returns (raw_score, hit_count).

    Scans title+description for each keyword in STACK_CATEGORIES. Each keyword
    is checked once (binary: present or not). The score is the sum of weights
    for all matched keywords. hit_count tracks how many distinct keywords matched.
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
    lower = text.lower()
    bonus = 0
    for keywords, points in SYNERGY_COMBOS:
        if all(k in lower for k in keywords):
            bonus += points
    return bonus


def level_tag(title: str, description: str = "") -> str:
    """Categorize job as New Grad / Entry / Mid / Unknown."""
    text = f"{title} {description}".lower()

    # New Grad
    new_grad = [r"\bnew[\s-]*grad", r"\bnew[\s-]*graduate\b", r"\bcampus\s+hire\b",
                r"\brecent\s+graduate?\b", r"\brecent\s+grad\b", r"\buniversity\s+grad"]
    for p in new_grad:
        if re.search(p, text):
            return "New Grad"

    # Entry
    entry = [
        r"\bentry[\s-]*level\b", r"\bjunior\b", r"\bjr\.?\s",
        r"\bassociate\b", r"\bearly\s+career\b", r"\bfirst\s+opportunity\b",
        r"\bsde[\s-]*[i1]\b", r"\bswe[\s-]*[i1]\b", r"\beng[\s-]*[i1]\b",
        r"\blevel[\s-]*[i1]\b", r"\blevel[\s-]*1\b",
        r"\b0[\s-]*(?:to[\s-]*)?2\s*(?:years?|yrs?)\b",
        r"\bintern\b",
    ]
    for p in entry:
        if re.search(p, text):
            return "Entry"

    # Mid
    mid = [
        r"\bsde[\s-]*(?:ii|2)\b", r"\bswe[\s-]*(?:ii|2)\b", r"\beng[\s-]*(?:ii|2)\b",
        r"\bsoftware\s+engineer\s*(?:ii|2)\b", r"\bmid[\s-]*level\b",
        r"\blevel[\s-]*(?:ii|2)\b",
        r"\b2[\s-]*(?:to[\s-]*)?5\s*(?:years?|yrs?)\b",
        r"\b3[\s-]*(?:to[\s-]*)?5\s*(?:years?|yrs?)\b",
    ]
    for p in mid:
        if re.search(p, text):
            return "Mid"

    return "Unknown"


def extract_experience(text: str) -> tuple[int | None, int | None]:
    """Parse experience requirements from JD text. Returns (min_exp, max_exp)."""
    text = text.lower()
    m = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:\+\s*)?(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\+\s*(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:minimum|at\s+least|min)\s*(?:of\s+)?(\d+)\s*(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d+)\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)", text)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None, None


def experience_score(min_exp: int | None, max_exp: int | None) -> int:
    """Score based on parsed experience requirements.

    Sweet spot for a new grad: 0-2 years (+10). The further from this
    range, the lower the score. Jobs requiring 3+ years get reduced scores
    since they're borderline for a new grad applicant.
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
    """Time-decay scoring — freshly posted jobs are more valuable.

    Applying early increases chances. Jobs older than 48h get a penalty (-5)
    because they likely already have many applicants.
    """
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
    """Estimate applicant competition (0-10) for prioritization.

    Big Tech companies (+5) and older postings (+2-5) tend to have more
    applicants. This score is informational — it doesn't affect should_apply,
    but helps the user prioritize which jobs to apply to first.
    """
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
    return {"New Grad": 20, "Entry": 15, "Mid": 5, "Unknown": 4}.get(level, 4)


# ── Main evaluation ─────────────────────────────────────────────────────────

def evaluate_job(job: JobPosting, first_seen: str | None = None) -> FilterResult:
    """Evaluate a job posting with multi-signal scoring and hard rejection."""
    title_lower = job.title.lower()
    text = f"{job.title} {job.description}"
    text_lower = text.lower()
    variant = select_variant(text_lower)
    level = level_tag(job.title, job.description)

    def _reject(reason: str) -> FilterResult:
        return FilterResult(
            score=0, score_pct=0, should_apply=False, reason=reason,
            resume_variant=variant, level=level,
        )

    # ── 1. Company blocklist ──
    if job.company.lower().strip() in COMPANY_BLOCKLIST:
        return _reject(f"Blocked company: {job.company}")

    # ── 2. Title-level hard reject (senior, QA, architect, VP) ──
    for pattern in TITLE_REJECT_PATTERNS:
        if re.search(pattern, title_lower):
            return _reject(f"Title disqualified: {job.title}")

    # ── 3. Sponsorship / citizenship / clearance ──
    if _has_phrase(text_lower, DISQUALIFYING_PHRASES):
        return _reject("No visa sponsorship or requires citizenship/clearance")

    # ── 4. Non-US location ──
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
        return _reject(f"Non-US location: {job.location}")

    # ── 5. Overqualified experience (hard reject) ──
    min_exp, max_exp = extract_experience(text_lower)
    if min_exp is not None and min_exp >= 4:
        return _reject(f"Requires {min_exp}+ years experience")
    for pattern in OVERQUALIFIED_PATTERNS:
        if re.search(pattern, text_lower):
            return _reject("Overqualified: high experience requirement")

    # ── 6. Senior salary signal (hard reject if no entry signals) ──
    has_senior_salary = bool(re.search(SENIOR_SALARY_PATTERN, text))
    has_entry_signals = has_match(text_lower, ENTRY_LEVEL_SIGNALS)
    if has_senior_salary and not has_entry_signals:
        return _reject("Senior-level salary ($130K+) with no entry-level signals")

    # ══ Passed all hard filters — compute additive score ══
    # Each component adds to a raw score (max ~130), then normalized to 0-100%.
    # The reasons list tracks which components contributed for the UI display.
    reasons = []

    # Keyword matching — how well does this JD match the user's tech stack?
    ks, hits = keyword_score(text)
    if ks > 0:
        reasons.append(f"Stack +{ks}")

    # Synergy bonus
    sb = synergy_bonus(text)
    if sb > 0:
        reasons.append(f"Synergy +{sb}")

    # Level
    lp = _level_points(level)
    if level != "Unknown":
        reasons.append(f"{level} +{lp}")

    # Experience scoring
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
    loc_score = 10 if has_match(job.location.lower(), us_patterns) else -10
    if loc_score > 0:
        reasons.append("US +10")

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

    # Description-level senior penalty (soft — JD may mention "work with senior engineers")
    senior_count = count_matches(text_lower, SENIOR_DESC_SIGNALS)
    entry_count = count_matches(text_lower, ENTRY_LEVEL_SIGNALS)
    senior_penalty = 0
    if senior_count >= 3 and entry_count == 0:
        senior_penalty = -30
        reasons.append("Senior desc -30")

    # ── Aggregate: sum all components, normalize to 0-100% ──
    raw = ks + sb + lp + es + rs + loc_score + h1b_bonus + senior_penalty
    score_pct = min(100, max(0, round(raw / SCORE_MAX_RAW * 100)))
    score = max(0, min(100, raw))
    # All jobs that pass hard rejects are kept — AI scoring is the real quality gate
    should_apply = True
    reason = "; ".join(reasons) if reasons else "Meets basic criteria"

    return FilterResult(
        score=score, score_pct=score_pct, should_apply=should_apply,
        reason=reason, resume_variant=variant, level=level,
        min_exp=min_exp, max_exp=max_exp, competition=comp, keyword_hits=hits,
    )
