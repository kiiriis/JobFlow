"""LinkedIn jobs local store — merges CI scan results with user status tracking."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINKEDIN_STATUSES = ["Tracking", "Applied", "Not Interested"]
RECOMMENDED_THRESHOLD = 15  # score_pct >= this marks job as "recommended"
RETENTION_DAYS = 7
# Statuses that survive the 7-day prune
KEEP_STATUSES = {"Tracking", "Applied"}


def load_store(path: Path) -> dict:
    """Load linkedin_jobs.json. Returns empty store if missing."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {"jobs": {}, "last_updated": ""}


def save_store(path: Path, store: dict) -> None:
    """Write linkedin_jobs.json."""
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2))


def merge_scan_results(store: dict, scan_results: list[dict]) -> dict:
    """Merge new jobs from scan_results.json into the store.

    - New jobs get status 'New' or 'Should Apply' (score_pct >= threshold)
    - Existing jobs keep their user-assigned status but update last_seen
    """
    now = datetime.now(timezone.utc).isoformat()
    jobs = store.get("jobs", {})

    for entry in scan_results:
        url = entry.get("url", "")
        key = url if url else f"{entry.get('company', '')}_{entry.get('title', '')}".lower()
        if not key:
            continue

        if key in jobs:
            # Update last_seen and scores
            jobs[key]["last_seen"] = now
            jobs[key]["score"] = entry.get("score", jobs[key].get("score", 0))
            score_pct = int(entry.get("score_pct", jobs[key].get("score_pct", 0)) or 0)
            jobs[key]["score_pct"] = score_pct
            jobs[key]["recommended"] = score_pct >= RECOMMENDED_THRESHOLD
            # Backfill new fields if missing
            for field in ("level", "min_exp", "max_exp", "competition", "search_term"):
                if field in entry and entry[field] is not None:
                    jobs[key][field] = entry[field]
            # Migrate old statuses
            if jobs[key].get("status") in ("Should Apply", "New"):
                jobs[key]["status"] = ""
        else:
            # New job
            score_pct = int(entry.get("score_pct", 0) or 0)
            jobs[key] = {
                "company": entry.get("company", ""),
                "title": entry.get("title", ""),
                "location": entry.get("location", ""),
                "url": url,
                "score": int(entry.get("score", 0) or 0),
                "score_pct": score_pct,
                "recommended": score_pct >= RECOMMENDED_THRESHOLD,
                "level": entry.get("level", "Unknown"),
                "min_exp": entry.get("min_exp"),
                "max_exp": entry.get("max_exp"),
                "competition": int(entry.get("competition", 0) or 0),
                "variant": entry.get("variant", "se"),
                "reason": entry.get("reason", ""),
                "description_preview": entry.get("description_preview", ""),
                "status": "",
                "first_seen": now,
                "last_seen": now,
                "search_term": entry.get("search_term", ""),
            }

    store["jobs"] = jobs
    return store


def prune_old_jobs(store: dict, days: int = RETENTION_DAYS) -> dict:
    """Remove jobs older than `days` unless they have a keep-status."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    jobs = store.get("jobs", {})
    pruned = {}
    for key, job in jobs.items():
        if job.get("status") in KEEP_STATUSES:
            pruned[key] = job
        elif job.get("last_seen", "") >= cutoff:
            pruned[key] = job
    store["jobs"] = pruned
    return store


def update_job_status(store: dict, key: str, status: str) -> bool:
    """Update a job's status. Returns True if found."""
    jobs = store.get("jobs", {})
    if key in jobs and (status in LINKEDIN_STATUSES or status == ""):
        jobs[key]["status"] = status
        return True
    return False


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_filtered_jobs(
    store: dict,
    status: str = "",
    level: str = "",
    query: str = "",
    search_term: str = "",
    time_range: str = "",
    hour_filter: str = "",
    sort_col: str = "last_seen",
    sort_dir: str = "desc",
) -> list[dict]:
    """Return jobs as a sorted list with filtering.

    time_range: "hour" (last 1h), "today" (since midnight UTC), "yesterday"
    hour_filter: specific hour like "14" to show jobs first seen in that hour today
    """
    jobs = store.get("jobs", {})
    result = []
    q_lower = query.lower().strip() if query else ""
    now = datetime.now(tz=timezone.utc)

    # Compute time boundaries
    time_cutoff = None
    time_end = None
    if time_range == "hour":
        time_cutoff = now - timedelta(hours=1)
    elif time_range == "today":
        time_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == "yesterday":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = today_start - timedelta(days=1)
        time_end = today_start

    # Hour filter (e.g. "14" means show jobs from 2 PM today)
    hour_start = None
    hour_end = None
    if hour_filter:
        try:
            h = int(hour_filter)
            hour_start = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if h > now.hour:
                hour_start -= timedelta(days=1)
            hour_end = hour_start + timedelta(hours=1)
        except (ValueError, TypeError):
            pass

    for key, job in jobs.items():
        entry = {"_key": key, **job}

        # Time range filter
        if time_cutoff or hour_start:
            fs = _parse_iso(entry.get("first_seen", ""))
            if not fs:
                continue
            if time_cutoff and fs < time_cutoff:
                continue
            if time_end and fs >= time_end:
                continue
            if hour_start and (fs < hour_start or fs >= hour_end):
                continue

        # Status filter (special case: "Recommended" is not a real status)
        if status == "Recommended":
            if not entry.get("recommended"):
                continue
        elif status and entry.get("status") != status:
            continue

        # Level filter
        if level and level != "All" and entry.get("level", "Unknown") != level:
            continue

        # Text search (company, title, location)
        if q_lower:
            searchable = f"{entry.get('company', '')} {entry.get('title', '')} {entry.get('location', '')}".lower()
            if q_lower not in searchable:
                continue

        # Search term filter
        if search_term and entry.get("search_term", "") != search_term:
            continue

        # Add computed recency display
        entry["_recency"] = format_recency(entry.get("first_seen", ""))

        result.append(entry)

    # Sort
    def sort_key(j):
        val = j.get(sort_col, "")
        if sort_col in ("score", "score_pct", "competition", "min_exp"):
            return int(val) if val is not None and val != "" else -1
        return str(val or "")

    reverse = sort_dir == "desc"

    # Not Interested always at bottom, then custom sort
    ni = [j for j in result if j.get("status") == "Not Interested"]
    rest = [j for j in result if j.get("status") != "Not Interested"]
    rest.sort(key=sort_key, reverse=reverse)
    ni.sort(key=sort_key, reverse=reverse)
    return rest + ni


def get_status_counts(store: dict) -> dict[str, int]:
    """Return count of jobs per status + recommended count."""
    counts = {s: 0 for s in LINKEDIN_STATUSES}
    counts["Recommended"] = 0
    total = 0
    for job in store.get("jobs", {}).values():
        total += 1
        s = job.get("status", "")
        if s in counts:
            counts[s] += 1
        if job.get("recommended"):
            counts["Recommended"] += 1
    counts["All"] = total
    return counts


def get_level_counts(store: dict) -> dict[str, int]:
    """Return count of jobs per level tag."""
    counts = {"All": 0, "New Grad": 0, "Entry": 0, "Mid": 0, "Unknown": 0}
    for job in store.get("jobs", {}).values():
        level = job.get("level", "Unknown")
        if level in counts:
            counts[level] += 1
        else:
            counts["Unknown"] += 1
    counts["All"] = sum(v for k, v in counts.items() if k != "All")
    return counts


def get_search_terms(store: dict) -> list[str]:
    """Return sorted list of distinct search_term values."""
    terms = set()
    for job in store.get("jobs", {}).values():
        t = job.get("search_term", "")
        if t:
            terms.add(t)
    return sorted(terms)


def format_recency(iso_timestamp: str) -> str:
    """Convert ISO timestamp to human-friendly relative time."""
    if not iso_timestamp:
        return "--"
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(tz=timezone.utc) - dt
        hours = diff.total_seconds() / 3600
        if hours < 1:
            return "now"
        if hours < 24:
            return f"{int(hours)}h ago"
        days = diff.days
        if days == 1:
            return "1d ago"
        if days < 7:
            return f"{days}d ago"
        return dt.strftime("%b %d")
    except (ValueError, TypeError):
        return "--"


def get_time_counts(store: dict) -> dict:
    """Return counts for time range tabs and hourly breakdown."""
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    one_hour_ago = now - timedelta(hours=1)

    hour_count = 0
    today_count = 0
    yesterday_count = 0

    # Hourly breakdown: last 24 hours, keyed by hour number
    hourly = {}
    for h in range(24):
        dt = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h)
        hourly[dt.strftime("%I %p").lstrip("0")] = {"hour": dt.hour, "count": 0, "label": dt.strftime("%I %p").lstrip("0")}

    for job in store.get("jobs", {}).values():
        fs = _parse_iso(job.get("first_seen", ""))
        if not fs:
            continue
        if fs >= one_hour_ago:
            hour_count += 1
        if fs >= today_start:
            today_count += 1
        if yesterday_start <= fs < today_start:
            yesterday_count += 1

        # Hourly bucketing
        diff_hours = (now - fs).total_seconds() / 3600
        if diff_hours < 24:
            bucket_dt = fs.replace(minute=0, second=0, microsecond=0)
            label = bucket_dt.strftime("%I %p").lstrip("0")
            if label in hourly:
                hourly[label]["count"] += 1

    # Convert to sorted list (most recent first)
    hourly_list = []
    for h in range(24):
        dt = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h)
        label = dt.strftime("%I %p").lstrip("0")
        if label in hourly:
            hourly_list.append(hourly[label])

    return {
        "this_hour": hour_count,
        "today": today_count,
        "yesterday": yesterday_count,
        "hourly": hourly_list,
    }


def get_sidebar_stats(store: dict) -> dict:
    """Compute sidebar statistics for the dashboard."""
    jobs = list(store.get("jobs", {}).values())
    total = len(jobs)

    # Match score distribution
    match_dist = {"80-100%": 0, "60-79%": 0, "40-59%": 0, "< 40%": 0}
    for j in jobs:
        pct = int(j.get("score_pct", 0) or 0)
        if pct >= 80:
            match_dist["80-100%"] += 1
        elif pct >= 60:
            match_dist["60-79%"] += 1
        elif pct >= 40:
            match_dist["40-59%"] += 1
        else:
            match_dist["< 40%"] += 1

    # Top companies
    from collections import Counter
    company_counts = Counter(j.get("company", "Unknown") for j in jobs)
    top_companies = company_counts.most_common(8)

    # Experience distribution
    exp_dist = {"0-1 yr": 0, "1-2 yrs": 0, "2-3 yrs": 0, "Not listed": 0}
    for j in jobs:
        mn = j.get("min_exp")
        mx = j.get("max_exp")
        if mn is None and mx is None:
            exp_dist["Not listed"] += 1
        elif mn is not None and mn <= 1:
            exp_dist["0-1 yr"] += 1
        elif mn is not None and mn <= 2:
            exp_dist["1-2 yrs"] += 1
        else:
            exp_dist["2-3 yrs"] += 1

    return {
        "total": total,
        "match_dist": match_dist,
        "top_companies": top_companies,
        "exp_dist": exp_dist,
    }


def backfill_job(job: dict) -> dict:
    """Add missing new fields to a job entry using available data."""
    from .filter import (
        keyword_score, synergy_bonus, level_tag, extract_experience,
        competition_estimate, experience_score, recency_score, SCORE_MAX_RAW,
    )

    if "score_pct" in job and job.get("level") and job["level"] != "":
        return job  # Already has new fields

    text = f"{job.get('title', '')} {job.get('description_preview', '')}"

    # Level
    if not job.get("level") or job.get("level") == "":
        job["level"] = level_tag(job.get("title", ""), job.get("description_preview", ""))

    # Experience
    if "min_exp" not in job:
        min_exp, max_exp = extract_experience(text)
        job["min_exp"] = min_exp
        job["max_exp"] = max_exp

    # Competition
    if "competition" not in job:
        job["competition"] = competition_estimate(job.get("company", ""), 24)

    # Score PCT
    if "score_pct" not in job or not job["score_pct"]:
        ks, hits = keyword_score(text)
        sb = synergy_bonus(text)
        es = experience_score(job.get("min_exp"), job.get("max_exp"))
        rs = recency_score(job.get("first_seen"))
        raw = ks + sb + es + rs + 10  # assume US location
        job["score_pct"] = min(100, max(0, round(raw / SCORE_MAX_RAW * 100)))
        job["keyword_hits"] = hits

    if "search_term" not in job:
        job["search_term"] = ""

    # Migrate old statuses
    if job.get("status") in ("Should Apply", "New"):
        job["status"] = ""

    # Set recommended flag
    job["recommended"] = int(job.get("score_pct", 0) or 0) >= RECOMMENDED_THRESHOLD

    return job
