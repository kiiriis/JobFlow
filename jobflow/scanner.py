"""Multi-platform job scanner — aggregates listings from LinkedIn, Lever,
Greenhouse, Ashby, and GitHub new-grad repos.

Data flow:
    1. scan_all_api_boards() orchestrates all platform scanners
    2. Each scanner (scan_lever, scan_linkedin, etc.) returns [JobPosting]
    3. Every JobPosting is scored by evaluate_job() → (JobPosting, FilterResult)
    4. deduplicate_results() removes already-seen jobs via seen_jobs.json
    5. Results saved to scan_results.json, then merged into linkedin_jobs.json

Platform differences:
    - Lever:       REST API, JSON, epoch ms timestamps, no auth needed
    - Greenhouse:  REST API, JSON, ISO timestamps, no auth needed
    - Ashby:       REST API, JSON, ISO timestamps, no auth needed
    - LinkedIn:    guest API scraper (jobflow.linkedin_scraper), two-phase
    - GitHub:      Raw README markdown parsing (SimplifyJobs, Jobright repos)

Deduplication (seen_jobs.json):
    Tracks previously seen job URLs with timestamps. Entries expire after
    48 hours (SEEN_TTL_HOURS) so reposted/updated jobs can resurface.
    Format: {"url": "ISO_timestamp_EST", ...}
    Backward-compatible: auto-migrates old array format on first load.
"""

import json
import os
import random
import re
import ssl
import time
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import unescape
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .filter import evaluate_job
from .linkedin_store import is_db_enabled
from .models import JobPosting, FilterResult

console = Console()

# Build a default SSL context and URL opener with browser-like headers.
# Using browser User-Agent prevents some ATS platforms from blocking requests.
_SSL_CTX = ssl.create_default_context()
_OPENER = urllib.request.build_opener()
_OPENER.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"),
    ("Accept", "application/json"),
]


def _fetch_json(url: str, retries: int = 3) -> dict | list | None:
    """Fetch JSON from a URL using stdlib urllib with retry/backoff.

    Uses exponential backoff (3^attempt seconds) on failure. Rate limit
    responses (429) respect the Retry-After header if present. We use
    stdlib urllib instead of requests to avoid the extra dependency for
    simple GET requests.
    """
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 3 ** (attempt + 1)))
                console.print(f"  [yellow]Rate limited, waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"  [red]Failed to fetch {url}: {e}[/red]")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3 ** (attempt + 1))
                continue
            console.print(f"  [red]Failed to fetch {url}: {e}[/red]")
            return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """Check if text contains any of the filter keywords."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


# Role must be software/engineering related.
# This is a pre-filter applied BEFORE evaluate_job() to reduce noise. It's
# intentionally broad — we'd rather let a borderline role through to the
# scoring engine than miss a valid job. The scoring engine handles the
# fine-grained filtering (senior, sponsorship, etc.).
SWE_ROLE_KEYWORDS = [
    "software", "engineer", "developer", "sde", "swe", "backend",
    "frontend", "full stack", "fullstack", "full-stack", "platform",
    "infrastructure", "devops", "systems engineer", "data engineer",
    "machine learning", "ml engineer", "ai engineer", "applied scientist",
    "research engineer", "security engineer", "site reliability",
    "cloud engineer", "distributed systems",
    "member of technical staff", "mts", "data scientist", "data analyst",
]


def _is_swe_role(title: str) -> bool:
    """Check if the job title is a software engineering role."""
    lower = title.lower()
    return any(kw in lower for kw in SWE_ROLE_KEYWORDS)


def _is_recent(posted_at: str | int | None, max_age_hours: int) -> bool:
    """Check if a job was posted within max_age_hours."""
    if not posted_at or max_age_hours <= 0:
        return True  # no filter if no timestamp or disabled

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    try:
        if isinstance(posted_at, (int, float)):
            # Lever uses epoch milliseconds
            ts = datetime.fromtimestamp(posted_at / 1000, tz=timezone.utc)
        elif isinstance(posted_at, str):
            # ISO format from Greenhouse/Ashby
            posted_at = posted_at.replace("Z", "+00:00")
            ts = datetime.fromisoformat(posted_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        else:
            return True
        return ts >= cutoff
    except (ValueError, OSError):
        return True  # can't parse → include it


# ---------------------------------------------------------------------------
# Platform-specific scanners
# ---------------------------------------------------------------------------

def scan_lever(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan a Lever company's job board via API.

    Lever's public API (api.lever.co/v0/postings/{company}) returns JSON with
    no auth required. Uses createdAt (epoch milliseconds) for recency filtering.
    include_kw is a list of entry-level keywords from job_boards.json that
    must appear in title/commitment/team to pass the pre-filter.
    """
    data = _fetch_json(api_url)
    if not data or not isinstance(data, list):
        return []

    jobs = []
    for post in data:
        title = post.get("text", "")
        loc = post.get("categories", {}).get("location", "")
        commitment = post.get("categories", {}).get("commitment", "")
        team = post.get("categories", {}).get("team", "")
        desc_html = post.get("descriptionPlain") or post.get("description", "")
        desc = _strip_html(desc_html) if "<" in desc_html else desc_html
        url = post.get("hostedUrl", "")

        # Recency check (Lever uses createdAt in epoch ms)
        if not _is_recent(post.get("createdAt"), max_age_hours):
            continue

        # Must be a software engineering role
        if not _is_swe_role(title):
            continue

        # Keyword pre-filter on title for new-grad/entry-level signals
        combined = f"{title} {commitment} {team}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc,
            description=desc[:6000],
        ))
    return jobs


def scan_greenhouse(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan a Greenhouse company's job board via API.

    Greenhouse API (boards-api.greenhouse.io/v1/boards/{company}/jobs)
    returns JSON with job objects nested under a "jobs" key. Uses updated_at
    (ISO format) for recency. Description is in HTML (content field).
    """
    data = _fetch_json(api_url)
    if not data or "jobs" not in data:
        return []

    jobs = []
    for post in data["jobs"]:
        # Recency check (Greenhouse uses updated_at in ISO format)
        if not _is_recent(post.get("updated_at"), max_age_hours):
            continue

        title = post.get("title", "")
        loc_name = post.get("location", {}).get("name", "")
        desc_html = post.get("content", "")
        desc = _strip_html(desc_html) if desc_html else ""
        url = post.get("absolute_url", "")
        departments = ", ".join(d.get("name", "") for d in post.get("departments", []))

        if not _is_swe_role(title):
            continue

        combined = f"{title} {departments}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc_name,
            description=desc[:6000],
        ))
    return jobs


def scan_ashby(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan an Ashby company's job board via API.

    Ashby API returns jobs under a "jobs" key with publishedAt (ISO format)
    for recency. Location can be a string or dict with a "name" key.
    """
    data = _fetch_json(api_url)
    if not data:
        return []

    job_list = data.get("jobs", [])
    if not job_list:
        return []

    jobs = []
    for post in job_list:
        # Recency check (Ashby uses publishedAt in ISO format)
        if not _is_recent(post.get("publishedAt"), max_age_hours):
            continue

        title = post.get("title", "")
        loc = post.get("location", "")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        desc = post.get("descriptionPlain") or _strip_html(post.get("descriptionHtml", ""))
        url = post.get("jobUrl") or post.get("applyUrl", "")
        department = post.get("department", "")
        team = post.get("team", "")

        if not _is_swe_role(title):
            continue

        combined = f"{title} {department} {team}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc if isinstance(loc, str) else "",
            description=desc[:6000],
        ))
    return jobs


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------

def load_job_boards(config: dict) -> dict:
    """Load JobBoards_Links.json."""
    path = config.get("job_boards")
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"JobBoards_Links.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def scan_all_api_boards(
    config: dict,
    platforms: list[str] | None = None,
    max_age_hours: int = 0,
) -> list[tuple[JobPosting, 'FilterResult']]:
    """Main scan orchestrator — scans all platforms and scores every job.

    This is the entry point called by both the CLI (`jobflow scan`) and the
    web dashboard's "Scan Now" button. It iterates through all configured
    platforms, collects JobPostings, and scores each one with evaluate_job().

    Args:
        config: Loaded config dict with job_boards path
        platforms: Filter to specific platforms (e.g., ["linkedin"]), or None for all
        max_age_hours: Only include jobs posted within this window (0 = default;
                       LinkedIn then uses a 4h window). Passed to LinkedIn's
                       f_TPR time filter and used for timestamp filtering on
                       ATS platforms.

    Returns:
        List of (JobPosting, FilterResult) tuples — includes both passing and
        rejected jobs so callers can show skip counts.
    """
    boards = load_job_boards(config)
    keywords = boards.get("scraping_tips", {}).get("keyword_filters_for_new_grad", [])

    if max_age_hours > 0:
        console.print(f"[dim]Filtering to jobs posted in the last {max_age_hours} hours[/dim]")

    all_results = []

    # Scan LinkedIn via the guest API (jobflow.linkedin_scraper)
    if not platforms or "linkedin" in platforms:
        console.print(f"\n[bold cyan]Scanning LinkedIn ({len(LINKEDIN_SEARCH_TERMS)} search terms)...[/bold cyan]")
        known = _known_job_urls(config)
        if known:
            console.print(f"  [dim]{len(known)} stored jobs will be skipped if re-listed[/dim]")
        jobs = scan_linkedin(max_age_hours, known_urls=known)
        console.print(f"  [green]{len(jobs)} new jobs[/green]")
        for job in jobs:
            result = evaluate_job(job)
            all_results.append((job, result))

    # Scan GitHub new-grad repos
    if not platforms or "github" in platforms:
        ng = boards.get("new_grad_aggregators", {})
        gh_repos = ng.get("github_repos", {})
        if gh_repos:
            console.print("\n[bold cyan]Scanning GitHub new-grad repos...[/bold cyan]")
            jobs = scan_github_repos(gh_repos, keywords)
            console.print(f"  [green]{len(jobs)} matches[/green]")
            for job in jobs:
                result = evaluate_job(job)
                all_results.append((job, result))

    return all_results


# ---------------------------------------------------------------------------
# LinkedIn scanner (guest API via jobflow.linkedin_scraper)
# ---------------------------------------------------------------------------
# Two-phase scan: (1) cheap listing pages for every search term, deduped by
# job id across terms; (2) full descriptions fetched ONLY for jobs the store
# doesn't already have. Descriptions are ~10x the request volume of listings,
# and re-fetching JDs for already-stored jobs is what used to trip LinkedIn's
# rate limiter and silently truncate every scan to a small sample.

LINKEDIN_SEARCH_TERMS = [
    "New Grad Software Engineer",
    "Junior Software Engineer",
    "Associate Software Engineer",
    "Entry Level Jobs 2026",
    "New Grad Machine Learning Engineer",
]

RESULTS_PER_TERM = 200
# Cap on description fetches per scan so a cold store can't blow past the CI
# job's 15-minute timeout. Anything past the cap is reported, kept with a
# title-only description, and re-fetched by the next scan.
MAX_DESC_FETCHES = 300
DESC_DELAY_S = (0.7, 1.5)
# Hard wall-clock budget for one LinkedIn scan. CI kills the whole job at 15
# minutes and scan results are only written at the end — running past this
# would lose everything, so wind down early and save what we have.
SCAN_TIME_BUDGET_S = 720
# A stored description this short is a placeholder (title fallback / failed
# fetch), not a real JD — treat the job as unknown so its JD is re-fetched.
MIN_STORED_DESC_LEN = 200


def _known_job_urls(config: dict) -> set[str]:
    """Normalized URLs of stored jobs that already have a real description.

    Prefers the DB, falls back to the JSON store; returns an empty set on any
    failure (the scan then degrades to fetching every description — the old
    behavior, minus jobspy's bugs).
    """
    from .linkedin_store import normalize_url

    if os.environ.get("DATABASE_URL"):
        try:
            from . import db
            db.init_db()
            conn = db.get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT url FROM jobs WHERE LENGTH(description_preview) >= %s",
                        (MIN_STORED_DESC_LEN,),
                    )
                    return {normalize_url(r[0]) for r in cur.fetchall()}
            finally:
                db.put_conn(conn)
        except Exception as e:
            console.print(
                f"  [yellow]Could not load known jobs from DB ({e}); "
                f"trying JSON store[/yellow]"
            )

    try:
        from .linkedin_store import load_store
        store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
        jobs = load_store(store_path).get("jobs", {})
        return {
            normalize_url(job.get("url", "") or key)
            for key, job in jobs.items()
            if len(job.get("description_preview") or "") >= MIN_STORED_DESC_LEN
        }
    except Exception:
        return set()


def scan_linkedin(max_age_hours: int = 0, known_urls: set[str] | None = None) -> list[JobPosting]:
    """Scan LinkedIn's guest API. Returns NEW jobs only (not in known_urls).

    Known jobs are skipped entirely rather than re-emitted: merging a job
    without its description would let the merge's per-user re-score overwrite
    a good algo score with one computed from an empty JD, and new users get
    the existing posting pool via db.backfill_user at signup anyway.
    """
    from . import linkedin_scraper as li
    from .linkedin_store import normalize_url

    known_urls = known_urls or set()
    # Default to a 4h window when no limit is specified — matches the CI cron
    # (every 30 min with --hours 4); anything older is already in the store,
    # and a wider window just burns request budget re-listing known jobs.
    hours = max_age_hours if max_age_hours > 0 else 4

    session = li.new_session()
    seen_ids: set[str] = set()
    listings: list[dict] = []
    blocked = False
    deadline = time.monotonic() + SCAN_TIME_BUDGET_S

    # Phase 1 — listings (cheap: ~10 jobs per request).
    for i, term in enumerate(LINKEDIN_SEARCH_TERMS):
        if time.monotonic() > deadline:
            console.print("  [yellow]Scan time budget reached — skipping remaining search terms.[/yellow]")
            break
        console.print(f"  [dim]Search: \"{term}\"...[/dim]", end=" ")
        try:
            found = li.search_listings(session, term, hours, RESULTS_PER_TERM, seen_ids)
        except li.LinkedInBlocked as e:
            console.print(f"[red]blocked: {e}[/red]")
            blocked = True
            break
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
            continue
        kept = [
            j for j in found
            if j["title"] and j["company"] and _is_swe_role(j["title"])
        ]
        console.print(f"[green]{len(kept)} kept[/green] [dim]({len(found)} listed)[/dim]")
        listings.extend(kept)
        if i < len(LINKEDIN_SEARCH_TERMS) - 1:
            time.sleep(random.uniform(2, 4))

    if blocked:
        console.print("  [yellow]Rate-limited — continuing with listings collected so far.[/yellow]")

    new_listings = [j for j in listings if normalize_url(j["url"]) not in known_urls]
    skipped_known = len(listings) - len(new_listings)
    to_fetch = new_listings[:MAX_DESC_FETCHES]
    deferred = len(new_listings) - len(to_fetch)
    console.print(
        f"  [dim]{len(listings)} listings: {skipped_known} already stored, "
        f"{len(new_listings)} new; fetching {len(to_fetch)} descriptions"
        + (f", {deferred} deferred to next scan" if deferred else "")
        + "[/dim]"
    )

    # Phase 2 — descriptions, only for jobs the store doesn't have.
    jobs: list[JobPosting] = []
    budget_reported = False
    for i, listing in enumerate(to_fetch):
        if not blocked and not budget_reported and time.monotonic() > deadline:
            budget_reported = True
            blocked = True  # stop fetching; keep title-only fallbacks below
            console.print(
                f"  [yellow]Scan time budget reached ({i}/{len(to_fetch)} "
                f"descriptions fetched) — remaining jobs kept with title-only "
                f"descriptions, re-fetched next scan.[/yellow]"
            )
        description = ""
        if not blocked:
            try:
                description = li.fetch_description(session, listing["job_id"])
            except li.LinkedInBlocked:
                blocked = True
                console.print(
                    f"  [yellow]Rate-limited during description fetch "
                    f"({i}/{len(to_fetch)} done) — remaining jobs kept with "
                    f"title-only descriptions, re-fetched next scan.[/yellow]"
                )
            if not blocked and i < len(to_fetch) - 1:
                time.sleep(random.uniform(*DESC_DELAY_S))

        date_posted = listing["date_posted"]
        if date_posted and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted):
            date_posted += "T00:00:00+00:00"

        jobs.append(JobPosting(
            url=listing["url"],
            title=listing["title"],
            company=listing["company"],
            location=listing["location"],
            description=description[:6000] if description else listing["title"],
            date_posted=date_posted,
            source="linkedin",
        ))
    return jobs


def _fetch_text(url: str, retries: int = 3) -> str | None:
    """Fetch text/HTML from a URL with retry/backoff."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 3 ** (attempt + 1)))
                console.print(f"  [yellow]Rate limited, waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"  [red]Failed: {e}[/red]")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3 ** (attempt + 1))
                continue
            console.print(f"  [red]Failed: {e}[/red]")
            return None


# ---------------------------------------------------------------------------
# GitHub new-grad repo scanner
# ---------------------------------------------------------------------------

def scan_github_repos(repos_config: dict, include_kw: list[str]) -> list[JobPosting]:
    """Scan GitHub new-grad repos by parsing README markdown tables.

    These repos (SimplifyJobs, Jobright) maintain community-curated lists of
    new grad job postings in README tables. We fetch the raw README and parse
    the HTML or markdown tables to extract job entries. Closed positions
    (marked with a lock emoji) are skipped.
    """
    repos = repos_config.get("repos", {})
    all_jobs = []
    seen = set()

    for name, info in repos.items():
        raw_url = info.get("raw_readme", "")
        if not raw_url:
            continue

        console.print(f"  [dim]{name}...[/dim]", end=" ")
        md = _fetch_text(raw_url)
        if not md:
            console.print("[red]failed[/red]")
            continue

        jobs = _parse_github_readme(md, include_kw, seen)
        console.print(f"[green]{len(jobs)} entries[/green]")
        all_jobs.extend(jobs)

    return all_jobs


def _parse_github_readme(md: str, include_kw: list[str], seen: set) -> list[JobPosting]:
    """Parse job tables from SimplifyJobs (HTML <tr>/<td>) and Jobright (markdown |) READMEs.

    Two formats are supported because different repos use different table styles:
    - SimplifyJobs: HTML tables with <tr>/<td> tags
    - Jobright: Standard markdown pipe-delimited tables
    """
    jobs = []

    # Try HTML table format first (SimplifyJobs uses <tr>/<td>)
    rows = re.findall(r'<tr>(.*?)</tr>', md, re.DOTALL)
    if rows:
        for row in rows:
            cells = re.findall(r'<td>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 3:
                continue
            job = _parse_table_row(cells, seen)
            if job:
                jobs.append(job)
        return jobs

    # Fallback: markdown pipe-delimited tables (Jobright format)
    for line in md.split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| :---"):
            continue
        if "| company" in line.lower() or "| role" in line.lower():
            continue

        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 3:
            continue

        job = _parse_table_row(cols, seen)
        if job:
            jobs.append(job)

    return jobs


def _extract_apply_url(cols: list[str]) -> str:
    """Extract the apply/job-posting URL from table columns.

    Each GitHub repo puts the apply link in a different place:
    - SimplifyJobs (HTML <td>): column 4 has the apply <a href>, skip simplify.jobs/c/ company profiles
    - Jobright (markdown |): column 2 has [Title](jobright.ai/...) — the apply link is IN the title
    - SpeedyApply (markdown | with HTML): last column with an <a href> img is the apply button

    Strategy: look for hrefs in columns 3+ first (apply columns), fall back to column 2 (title link).
    Skip simplify.jobs/c/ (company profiles) and bare homepages (no path).
    """
    # First pass: search columns 3+ for apply links (where apply buttons live)
    for col in cols[3:] if len(cols) > 3 else []:
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', col)
        for href in hrefs:
            if "simplify.jobs/c/" in href:
                continue
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    # Second pass: column 2 (role/title) — Jobright puts the apply link here
    if len(cols) > 1:
        # Check for markdown link [Title](url)
        url_m = re.search(r'\[.*?\]\((https?://[^)]+)\)', cols[1])
        if url_m:
            cleaned = url_m.group(1).split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned
        # Check for HTML href in title column
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', cols[1])
        for href in hrefs:
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    # Third pass: column 3 (sometimes apply is here)
    if len(cols) > 2:
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', cols[2])
        for href in hrefs:
            if "simplify.jobs/c/" in href:
                continue
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    return ""


def _parse_table_row(cols: list[str], seen: set) -> JobPosting | None:
    """Parse a single table row (HTML or markdown) into a JobPosting."""
    # Extract company name from links
    company_raw = cols[0]
    company_m = re.search(r'[>\]]([\w\s&.\'-]+)[<\[]', company_raw) or re.search(r'\[([^\]]+)\]', company_raw)
    if company_m:
        company = company_m.group(1).strip()
    else:
        company = _strip_html(company_raw).strip("* ")
    company = company.strip("🔥 ").strip()

    # Extract role
    role_raw = cols[1]
    role_m = re.search(r'\[([^\]]+)\]', role_raw)
    role = role_m.group(1) if role_m else _strip_html(role_raw).strip()
    role = role.strip("* ")

    # Extract location
    location_raw = cols[2] if len(cols) > 2 else ""
    location = _strip_html(location_raw.replace("</br>", ", ")).strip()

    # Extract application URL from the correct column
    url = _extract_apply_url(cols)

    # Skip closed
    if any("🔒" in c for c in cols):
        return None

    # Dedup
    key = f"{company}_{role}".lower()
    if key in seen:
        return None
    seen.add(key)

    # Must be SWE-related
    if not _is_swe_role(role):
        return None

    if not company or not role:
        return None

    return JobPosting(
        url=url,
        title=role,
        company=company,
        location=location,
        description=f"{role} at {company}. Location: {location}",
        source="github",
    )


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------
# seen_jobs.json tracks which jobs we've already processed to avoid showing
# the same job twice across scans. Format: {"url_or_key": "EST_timestamp"}.
#
# The 48-hour TTL ensures that:
# 1. Jobs that get reposted/updated resurface after 2 days
# 2. The file doesn't grow unboundedly (was 5,573 entries before TTL was added)
# 3. If a job appears in multiple search terms, it's still deduped within a scan
#
# All timestamps use US/Eastern timezone for consistency with the user.

SEEN_TTL_HOURS = 48
EST = ZoneInfo("US/Eastern")

def load_seen_jobs(config: dict) -> dict[str, str]:
    """Load previously seen job URLs, pruning entries older than 48h."""
    if is_db_enabled():
        try:
            from .db import load_seen_jobs as db_load
            return db_load()
        except Exception:
            pass
    path = config["output_dir"] / "seen_jobs.json"
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    # Backward compat: convert old list format to dict with current timestamp
    if isinstance(data, list):
        now = datetime.now(EST).isoformat()
        data = {url: now for url in data}
    # Prune expired entries
    cutoff = datetime.now(EST) - timedelta(hours=SEEN_TTL_HOURS)
    return {
        url: ts for url, ts in data.items()
        if datetime.fromisoformat(ts) > cutoff
    }


def save_seen_jobs(config: dict, seen: dict[str, str]) -> None:
    """Save seen job URLs with timestamps."""
    if is_db_enabled():
        try:
            from .db import save_seen_jobs_bulk as db_save
            db_save(seen)
            return
        except Exception:
            pass
    path = config["output_dir"] / "seen_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(dict(sorted(seen.items())), f, indent=2)


def deduplicate_results(
    results: list[tuple[JobPosting, 'FilterResult']],
    seen: dict[str, str],
) -> tuple[list[tuple[JobPosting, 'FilterResult']], dict[str, str]]:
    """Remove already-seen jobs. Returns (new_results, updated_seen_dict)."""
    from .linkedin_store import normalize_url
    new_results = []
    now = datetime.now(EST).isoformat()
    for job, filt in results:
        key = normalize_url(job.url) if job.url else f"{job.company}_{job.title}"
        if key not in seen:
            seen[key] = now
            new_results.append((job, filt))
    return new_results, seen


def print_scan_results(results: list[tuple[JobPosting, 'FilterResult']]) -> None:
    """Pretty-print scan results in a table."""
    if not results:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    # Split for display only: jobs flagged by a hard-reject rule carry a
    # reject_reason. They are still kept downstream (the AI scorer can rescue
    # them), but showing them apart from clean matches keeps the table readable.
    relevant = [(j, r) for j, r in results if not r.reject_reason]
    flagged = [(j, r) for j, r in results if r.reject_reason]

    if relevant:
        table = Table(title=f"Relevant Jobs ({len(relevant)})", border_style="green")
        table.add_column("#", style="dim", width=4)
        table.add_column("Company", style="cyan", max_width=15)
        table.add_column("Role", max_width=40)
        table.add_column("Location", max_width=20)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Variant", width=8)
        table.add_column("URL", max_width=50)

        for i, (job, filt) in enumerate(sorted(relevant, key=lambda x: x[1].score, reverse=True), 1):
            table.add_row(
                str(i), job.company, job.title, job.location,
                str(filt.score), filt.resume_variant,
                job.url[:50] + "..." if len(job.url) > 50 else job.url,
            )
        console.print(table)

    if flagged:
        console.print(f"\n[dim]Flagged by filter rules: {len(flagged)} jobs (sponsorship, senior-level, non-US, etc.) — kept for AI re-scoring[/dim]")

    console.print(f"\n[bold]Total: {len(results)} scanned, {len(relevant)} relevant, {len(flagged)} flagged[/bold]")
