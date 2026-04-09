"""LinkedIn jobs local store — merges CI scan results with user status tracking."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINKEDIN_STATUSES = ["Tracking", "Applied", "Not Interested"]
RECOMMENDED_THRESHOLD = 25  # score_pct >= this marks job as "recommended"
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


def _dedup_key(entry: dict) -> str:
    """Generate a dedup key: prefer URL, fall back to company+title normalized."""
    url = entry.get("url", "")
    if url:
        return url
    co = entry.get("company", "").lower().strip()
    title = entry.get("title", "").lower().strip()
    return f"{co}_{title}"


def _rescore_entry(entry: dict) -> dict:
    """Re-score a job entry using the current filter logic."""
    from .filter import (
        keyword_score, synergy_bonus, level_tag, extract_experience,
        competition_estimate, experience_score, recency_score,
        has_match, DISQUALIFYING_PHRASES, H1B_PREFER, SCORE_MAX_RAW,
    )
    text = f"{entry.get('title', '')} {entry.get('description_preview', '')}"
    text_lower = text.lower()

    ks, hits = keyword_score(text)
    sb = synergy_bonus(text)
    level = level_tag(entry.get("title", ""), entry.get("description_preview", ""))
    min_exp, max_exp = extract_experience(text_lower)
    es = experience_score(min_exp, max_exp)
    rs = recency_score(entry.get("first_seen"))
    loc_score = 10  # assume US (LinkedIn US search)
    h1b = 8 if any(p in text_lower for p in H1B_PREFER) else 0
    comp = competition_estimate(entry.get("company", ""), 24)

    # Level points
    lp = {"New Grad": 20, "Entry": 15, "Mid": 5}.get(level, 4)

    raw = ks + sb + lp + es + rs + loc_score + h1b
    score_pct = min(100, max(0, round(raw / SCORE_MAX_RAW * 100)))

    entry["score"] = max(0, min(100, raw))
    entry["score_pct"] = score_pct
    entry["level"] = level
    entry["min_exp"] = min_exp
    entry["max_exp"] = max_exp
    entry["competition"] = comp
    entry["keyword_hits"] = hits
    entry["recommended"] = score_pct >= RECOMMENDED_THRESHOLD
    return entry


def merge_scan_results(store: dict, scan_results: list[dict]) -> dict:
    """Merge new jobs into the store with deduplication and re-scoring.

    Dedup: same company+title across different locations → keep one (prefer the one with URL).
    All jobs are re-scored using the current filter logic.
    """
    now = datetime.now(timezone.utc).isoformat()
    jobs = store.get("jobs", {})

    # Pre-dedup scan results: group by company+title, keep best per group
    seen_combos: dict[str, dict] = {}
    for entry in scan_results:
        co = entry.get("company", "").lower().strip()
        title = entry.get("title", "").lower().strip()
        combo = f"{co}|{title}"
        if combo in seen_combos:
            # Keep the one with a URL, or the first one
            if not seen_combos[combo].get("url") and entry.get("url"):
                seen_combos[combo] = entry
        else:
            seen_combos[combo] = entry
    deduped_results = list(seen_combos.values())

    for entry in deduped_results:
        key = _dedup_key(entry)
        if not key:
            continue

        if key in jobs:
            jobs[key]["last_seen"] = now
            # Keep user status, update description if better
            if entry.get("description_preview") and len(entry.get("description_preview", "")) > len(jobs[key].get("description_preview", "")):
                jobs[key]["description_preview"] = entry["description_preview"]
            # Update date_posted if we now have one and didn't before
            if entry.get("date_posted") and not jobs[key].get("date_posted"):
                jobs[key]["date_posted"] = entry["date_posted"]
                jobs[key]["first_seen"] = entry["date_posted"]
            # Migrate old statuses
            if jobs[key].get("status") in ("Should Apply", "New"):
                jobs[key]["status"] = ""
            # Re-score with latest logic
            jobs[key] = _rescore_entry(jobs[key])
        else:
            # Use date_posted from source if available, else merge time
            posted = entry.get("date_posted", "") or now
            jobs[key] = {
                "company": entry.get("company", ""),
                "title": entry.get("title", ""),
                "location": entry.get("location", ""),
                "url": entry.get("url", ""),
                "description_preview": entry.get("description_preview", ""),
                "variant": entry.get("variant", "se"),
                "reason": entry.get("reason", ""),
                "status": "",
                "first_seen": posted,
                "last_seen": now,
                "date_posted": entry.get("date_posted", ""),
                "search_term": entry.get("search_term", ""),
                # Placeholders — _rescore_entry fills these
                "score": 0, "score_pct": 0, "level": "Unknown",
                "min_exp": None, "max_exp": None, "competition": 0,
                "keyword_hits": 0, "recommended": False,
            }
            jobs[key] = _rescore_entry(jobs[key])

    # Also deduplicate existing store: remove jobs with same company+title (keep the one with user status or URL)
    by_combo: dict[str, list[str]] = {}
    for key, job in jobs.items():
        combo = f"{job.get('company','').lower().strip()}|{job.get('title','').lower().strip()}"
        by_combo.setdefault(combo, []).append(key)
    for combo, keys in by_combo.items():
        if len(keys) <= 1:
            continue
        # Keep the best: prefer one with user status, then URL, then newest
        def rank(k):
            j = jobs[k]
            has_status = 1 if j.get("status") else 0
            has_url = 1 if j.get("url") else 0
            return (has_status, has_url, j.get("first_seen", ""))
        keys.sort(key=rank, reverse=True)
        for k in keys[1:]:  # remove all but the best
            del jobs[k]

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
    tz_offset: int = 0,
) -> list[dict]:
    """Return jobs as a sorted list with filtering.

    time_range: "hour" (last 1h), "today" (since user's midnight), "yesterday"
    hour_filter: specific local hour like "22" to show jobs from that hour
    tz_offset: user's timezone offset in minutes from UTC (e.g. 240 = UTC-4 EDT)
    """
    jobs = store.get("jobs", {})
    result = []
    q_lower = query.lower().strip() if query else ""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    # Compute time boundaries in UTC based on user's local time
    time_cutoff = None
    time_end = None
    if time_range == "hour":
        time_cutoff = now_utc - timedelta(hours=1)
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = local_midnight.astimezone(timezone.utc)
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
        time_end = local_midnight.astimezone(timezone.utc)

    # Hour filter: specific local hour
    hour_start = None
    hour_end = None
    if hour_filter:
        try:
            h = int(hour_filter)
            local_hour = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
            if h > now_local.hour:
                local_hour -= timedelta(days=1)
            hour_start = local_hour.astimezone(timezone.utc)
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

    # Sort with secondary key: when primary is equal, sort by score_pct desc
    def sort_key(j):
        val = j.get(sort_col, "")
        if sort_col in ("score", "score_pct", "competition", "min_exp"):
            primary = int(val) if val is not None and val != "" else -1
        else:
            primary = str(val or "")
        secondary = int(j.get("score_pct", 0) or 0)
        return (primary, secondary)

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
    """Server-side fallback: 'Xh ago (HH:MM UTC)'. JS overrides with local time."""
    if not iso_timestamp:
        return "--"
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(tz=timezone.utc) - dt
        hours = diff.total_seconds() / 3600
        clock = dt.strftime("%I:%M %p").lstrip("0")
        if hours < 1:
            rel = "just now"
        elif hours < 24:
            rel = f"{int(hours)}h ago"
        elif diff.days == 1:
            rel = "1d ago"
        elif diff.days < 7:
            rel = f"{diff.days}d ago"
        else:
            return dt.strftime("%b %d")
        return f"{rel} ({clock})"
    except (ValueError, TypeError):
        return "--"


def get_time_counts(store: dict, tz_offset: int = 0) -> dict:
    """Return counts for time range tabs and hourly breakdown.

    tz_offset: user's timezone offset in minutes from UTC (e.g. 240 = UTC-4).
    All "today"/"yesterday" boundaries use the user's local midnight.
    Hourly labels use UTC hours (converted to local in the browser).
    """
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = local_midnight.astimezone(timezone.utc)
    yesterday_start_utc = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)

    hour_count = 0
    today_count = 0
    yesterday_count = 0

    # Hourly breakdown: last 24 hours in UTC (browser converts to local)
    hourly_buckets = {}
    for h in range(24):
        dt = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h)
        hourly_buckets[dt.hour] = {"hour": dt.hour, "count": 0, "label": dt.strftime("%I %p").lstrip("0")}

    for job in store.get("jobs", {}).values():
        fs = _parse_iso(job.get("first_seen", ""))
        if not fs:
            continue
        if fs >= one_hour_ago:
            hour_count += 1
        if fs >= today_start_utc:
            today_count += 1
        if yesterday_start_utc <= fs < today_start_utc:
            yesterday_count += 1

        diff_hours = (now_utc - fs).total_seconds() / 3600
        if diff_hours < 24:
            bucket_hour = fs.replace(minute=0, second=0, microsecond=0).hour
            if bucket_hour in hourly_buckets:
                hourly_buckets[bucket_hour]["count"] += 1

    # Sorted list, most recent first
    hourly_list = []
    for h in range(24):
        dt = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h)
        if dt.hour in hourly_buckets:
            hourly_list.append(hourly_buckets[dt.hour])

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
    """Re-score and fill all fields on a job entry."""
    # Migrate old statuses
    if job.get("status") in ("Should Apply", "New"):
        job["status"] = ""

    if "search_term" not in job:
        job["search_term"] = ""

    # Always re-score to ensure consistency
    return _rescore_entry(job)
