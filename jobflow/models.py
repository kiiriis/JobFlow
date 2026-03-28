from dataclasses import dataclass


@dataclass
class JobPosting:
    url: str
    title: str
    company: str
    location: str
    description: str


@dataclass
class FilterResult:
    score: int
    should_apply: bool
    reason: str
    resume_variant: str  # "se", "ml", or "appdev"
