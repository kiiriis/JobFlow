from dataclasses import dataclass, field


@dataclass
class JobPosting:
    url: str
    title: str
    company: str
    location: str
    description: str
    date_posted: str = ""  # ISO date string from the source


@dataclass
class FilterResult:
    score: int
    score_pct: int  # 0-100 normalized percentage
    should_apply: bool
    reason: str
    resume_variant: str  # "se", "ml", or "appdev"
    level: str = "Unknown"  # "New Grad" / "Entry" / "Mid" / "Unknown"
    min_exp: int | None = None
    max_exp: int | None = None
    competition: int = 0  # 0-10 estimated competition
    keyword_hits: int = 0
