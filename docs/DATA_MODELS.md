# Data Models & Storage

## Python Dataclasses (`jobflow/models.py`)

### JobPosting

```python
@dataclass
class JobPosting:
    url: str            # Job posting URL
    title: str          # Job title
    company: str        # Company name
    location: str       # Job location
    description: str    # Full JD text (up to 5000 chars)
    date_posted: str    # ISO timestamp from source (often empty for LinkedIn)
```

### FilterResult

```python
@dataclass
class FilterResult:
    score: int              # Raw score (0-100)
    score_pct: int          # Normalized percentage (0-100)
    should_apply: bool      # True if score_pct >= 30
    reason: str             # Human-readable scoring breakdown
    resume_variant: str     # "se", "ml", or "appdev"
    level: str              # "New Grad" / "Entry" / "Mid" / "Unknown"
    min_exp: int | None     # Minimum years parsed from JD
    max_exp: int | None     # Maximum years parsed from JD
    competition: int        # 0-10 competition estimate
    keyword_hits: int       # Count of matched stack keywords
```

## JSON Store: `data/ci/linkedin_jobs.json`

Persistent storage for LinkedIn jobs with user status tracking.

```json
{
  "last_updated": "2026-04-09T04:00:00+00:00",
  "jobs": {
    "https://linkedin.com/jobs/view/12345": {
      "company": "Stripe",
      "title": "Software Engineer, New Grad",
      "location": "San Francisco, CA",
      "url": "https://linkedin.com/jobs/view/12345",
      "score": 45,
      "score_pct": 35,
      "recommended": true,
      "level": "New Grad",
      "min_exp": 0,
      "max_exp": 2,
      "competition": 5,
      "keyword_hits": 7,
      "variant": "se",
      "reason": "Stack +24; Synergy +10; New Grad +20; ...",
      "description_preview": "First 200 chars of JD...",
      "status": "",
      "first_seen": "2026-04-09T03:00:00+00:00",
      "last_seen": "2026-04-09T04:00:00+00:00",
      "date_posted": "",
      "search_term": "new grad software engineer"
    }
  }
}
```

### Job Fields

| Field | Type | Description |
|-------|------|-------------|
| `company` | string | Company name |
| `title` | string | Job title |
| `location` | string | Job location |
| `url` | string | LinkedIn job URL (also used as dedup key) |
| `score` | int | Raw score from filter engine (0-100) |
| `score_pct` | int | Normalized match percentage (0-100) |
| `recommended` | bool | True if score_pct >= 25 |
| `level` | string | Detected level: "New Grad", "Entry", "Mid", "Unknown" |
| `min_exp` | int/null | Minimum years from JD (null if not found) |
| `max_exp` | int/null | Maximum years from JD |
| `competition` | int | 0-10 competition estimate |
| `keyword_hits` | int | Number of matched keywords |
| `variant` | string | Resume variant: "se", "ml", "appdev" |
| `reason` | string | Scoring breakdown |
| `description_preview` | string | First 200 chars of JD |
| `status` | string | User status: "", "Tracking", "Applied", "Not Interested" |
| `first_seen` | ISO string | When job was first discovered (used for sorting/filtering) |
| `last_seen` | ISO string | Last time job appeared in a scan |
| `date_posted` | ISO string | LinkedIn post date (usually empty — LinkedIn doesn't expose this) |
| `search_term` | string | The search query that found this job |

### Status Values

| Status | Meaning |
|--------|---------|
| `""` (empty) | Default — no user action taken |
| `"Tracking"` | User is watching this job (survives 7-day prune) |
| `"Applied"` | User has applied (survives 7-day prune) |
| `"Not Interested"` | User dismissed (sinks to bottom of list) |

### Deduplication

Jobs are deduplicated by **company+title** (case-insensitive). Same role posted in multiple locations is collapsed to one entry. The best entry is kept (prefers: user status > has URL > newest).

### Pruning

Jobs older than 7 days (based on `last_seen`) are automatically removed, **unless** their status is "Tracking" or "Applied".

## JSON: `data/ci/scan_results.json`

Raw output from the scanner. Array of job entries (max 500, sorted by score desc).

```json
[
  {
    "index": 1,
    "company": "Stripe",
    "title": "Software Engineer, New Grad",
    "location": "San Francisco, CA",
    "url": "https://...",
    "score": 45,
    "score_pct": 35,
    "level": "New Grad",
    "min_exp": 0,
    "max_exp": 2,
    "competition": 5,
    "variant": "se",
    "reason": "Stack +24; ...",
    "description_preview": "...",
    "date_posted": ""
  }
]
```

## CSV: `data/applications.csv`

Application tracking with status management.

```
company,role,link,score,variant,status,source,resume_path,date_found,date_applied,notes
Stripe,Software Engineer,https://...,85,se,Applied,linkedin,,2026-04-08,2026-04-09,Great match
```

### CSV Headers

| Column | Description |
|--------|-------------|
| `company` | Company name |
| `role` | Job title |
| `link` | Job URL |
| `score` | Filter score |
| `variant` | Resume variant used |
| `status` | Pending/Applied/Interview/OA/Offer/Rejected/Skipped/Withdrawn |
| `source` | How found: "linkedin", "manual", "scan" |
| `resume_path` | Path to generated resume |
| `date_found` | Date added (YYYY-MM-DD) |
| `date_applied` | Date status changed to Applied |
| `notes` | Free-text notes |

## JSON: `data/ci/seen_jobs.json`

Set of previously seen job URLs for deduplication across scans.

```json
["https://linkedin.com/jobs/view/123", "https://linkedin.com/jobs/view/456"]
```
