"""Multi-platform job scanner — aggregates listings from LinkedIn, Lever,
Greenhouse, Ashby, and GitHub new-grad repos.

Data flow:
    1. scan_all_api_boards() orchestrates all platform scanners
    2. Each scanner (scan_lever, scan_linkedin_jobspy, etc.) returns [JobPosting]
    3. Every JobPosting is scored by evaluate_job() → (JobPosting, FilterResult)
    4. deduplicate_results() removes already-seen jobs via seen_jobs.json
    5. Results saved to scan_results.json, then merged into linkedin_jobs.json

Platform differences:
    - Lever:       REST API, JSON, epoch ms timestamps, no auth needed
    - Greenhouse:  REST API, JSON, ISO timestamps, no auth needed
    - Ashby:       REST API, JSON, ISO timestamps, no auth needed
    - LinkedIn:    python-jobspy library (scrapes LinkedIn), returns DataFrame
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
from .models import JobPosting

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
            description=desc[:3000],
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
            description=desc[:3000],
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
            description=desc[:3000],
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
        max_age_hours: Only include jobs posted within this window (0 = all time).
                       Passed directly to jobspy's hours_old parameter for LinkedIn,
                       and used for timestamp filtering on ATS platforms.

    Returns:
        List of (JobPosting, FilterResult) tuples — includes both passing and
        rejected jobs so callers can show skip counts.
    """
    from .filter import evaluate_job

    boards = load_job_boards(config)
    ats = boards.get("ats_platforms", {})
    keywords = boards.get("scraping_tips", {}).get("keyword_filters_for_new_grad", [])

    if max_age_hours > 0:
        console.print(f"[dim]Filtering to jobs posted in the last {max_age_hours} hours[/dim]")

    all_results = []

    # Scan Lever companies
    if not platforms or "lever" in platforms:
        lever = ats.get("lever", {})
        companies = lever.get("example_companies", {})
        if companies:
            console.print(f"\n[bold cyan]Scanning Lever ({len(companies)} companies)...[/bold cyan]")
            for name, urls in companies.items():
                api_url = urls.get("api", "")
                if not api_url:
                    continue
                console.print(f"  [dim]{name}...[/dim]", end=" ")
                jobs = scan_lever(name, api_url, keywords, max_age_hours)
                console.print(f"[green]{len(jobs)} matches[/green]")
                for job in jobs:
                    result = evaluate_job(job)
                    all_results.append((job, result))

    # Scan Greenhouse companies
    if not platforms or "greenhouse" in platforms:
        gh = ats.get("greenhouse", {})
        companies = gh.get("example_companies", {})
        if companies:
            console.print(f"\n[bold cyan]Scanning Greenhouse ({len(companies)} companies)...[/bold cyan]")
            for name, urls in companies.items():
                api_url = urls.get("api", "")
                if not api_url:
                    continue
                console.print(f"  [dim]{name}...[/dim]", end=" ")
                jobs = scan_greenhouse(name, api_url, keywords, max_age_hours)
                console.print(f"[green]{len(jobs)} matches[/green]")
                for job in jobs:
                    result = evaluate_job(job)
                    all_results.append((job, result))

    # Scan Ashby companies
    if not platforms or "ashby" in platforms:
        ashby = ats.get("ashby", {})
        companies = ashby.get("example_companies", {})
        if companies:
            console.print(f"\n[bold cyan]Scanning Ashby ({len(companies)} companies)...[/bold cyan]")
            for name, urls in companies.items():
                api_url = urls.get("api", "")
                if not api_url:
                    continue
                console.print(f"  [dim]{name}...[/dim]", end=" ")
                jobs = scan_ashby(name, api_url, keywords, max_age_hours)
                console.print(f"[green]{len(jobs)} matches[/green]")
                for job in jobs:
                    result = evaluate_job(job)
                    all_results.append((job, result))

    # Scan LinkedIn via python-jobspy
    if not platforms or "linkedin" in platforms:
        console.print(f"\n[bold cyan]Scanning LinkedIn ({len(LINKEDIN_SEARCH_TERMS)} search terms)...[/bold cyan]")
        jobs = scan_linkedin_jobspy(max_age_hours)
        console.print(f"  [green]{len(jobs)} total matches[/green]")
        for job in jobs:
            result = evaluate_job(job)
            all_results.append((job, result))

    # Scan GitHub new-grad repos
    if not platforms or "github" in platforms:
        ng = boards.get("new_grad_aggregators", {})
        gh_repos = ng.get("github_repos", {})
        if gh_repos:
            console.print(f"\n[bold cyan]Scanning GitHub new-grad repos...[/bold cyan]")
            jobs = scan_github_repos(gh_repos, keywords)
            console.print(f"  [green]{len(jobs)} matches[/green]")
            for job in jobs:
                result = evaluate_job(job)
                all_results.append((job, result))

    return all_results


# ---------------------------------------------------------------------------
# LinkedIn scanner (python-jobspy)
# ---------------------------------------------------------------------------
# Uses the python-jobspy library to scrape LinkedIn job listings. Each search
# term is run as a separate query, results are deduped by URL across terms.
# linkedin_fetch_description=True fetches the full JD for each job (slow but
# needed for accurate scoring). Descriptions are truncated to 5K chars.

LINKEDIN_SEARCH_TERMS = [
    "Software Engineer New Grad",
    "Entry Level Software Engineer",
    "Software Engineer 1",
    "New Grad Machine Learning Engineer",
    "Entry Level AI Engineer",
]


def scan_linkedin_jobspy(max_age_hours: int = 0) -> list[JobPosting]:
    """Scan LinkedIn using python-jobspy. Returns deduplicated JobPosting list."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        console.print("  [red]python-jobspy not installed. Run: pip install python-jobspy[/red]")
        return []

    all_jobs = []
    seen_urls = set()
    # Default to 72h window if no limit specified — LinkedIn's timestamps are
    # imprecise (often just a date), so a wider window catches more jobs.
    hours = max_age_hours if max_age_hours > 0 else 72

    for i, term in enumerate(LINKEDIN_SEARCH_TERMS):
        console.print(f"  [dim]Search: \"{term}\"...[/dim]", end=" ")
        try:
            df = scrape_jobs(
                site_name=["linkedin"],
                search_term=term,
                location="United States",
                hours_old=hours,
                results_wanted=500,
                linkedin_fetch_description=True,
            )
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
            continue

        if df is None or df.empty:
            console.print("[yellow]0 results[/yellow]")
        else:
            count = 0
            for _, row in df.iterrows():
                url = str(row.get("job_url", "") or "")
                title = str(row.get("title", "") or "")
                company = str(row.get("company", "") or "")
                location = str(row.get("location", "") or "")
                description = str(row.get("description", "") or "")

                if not title or not company:
                    continue
                if not _is_swe_role(title):
                    continue

                # Dedup by URL across search terms
                dedup_key = url if url else f"{company}_{title}".lower()
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)

                date_posted = str(row.get("date_posted", "") or "")
                # Normalize date_posted to ISO string
                if date_posted and date_posted != "NaT":
                    try:
                        import pandas as pd
                        dp = pd.to_datetime(date_posted, utc=True)
                        date_posted = dp.isoformat() if not pd.isna(dp) else ""
                    except Exception:
                        pass
                else:
                    date_posted = ""

                all_jobs.append(JobPosting(
                    url=url,
                    title=title,
                    company=company,
                    location=location,
                    description=description[:5000] if description else title,
                    date_posted=date_posted,
                ))
                count += 1
            console.print(f"[green]{count} new[/green]")

        # Random delay (2-4s) between search terms to avoid LinkedIn rate limiting
        if i < len(LINKEDIN_SEARCH_TERMS) - 1:
            time.sleep(random.uniform(2, 4))

    return all_jobs


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

    # Extract application URL (take last href — first is often the company homepage)
    url = ""
    full_row = " ".join(cols)
    href_matches = re.findall(r'href=["\']?(https?://[^"\'> ]+)', full_row)
    for href in reversed(href_matches):
        cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
        # Skip bare homepages (no path or just /)
        path = urlparse(cleaned).path.rstrip("/")
        if not path or path == "":
            continue
        url = cleaned
        break
    if not url:
        # Markdown link fallback
        url_m = re.search(r'\[.*?\]\((https?://[^)]+)\)', full_row)
        if url_m:
            candidate = url_m.group(1).split("?utm_source")[0]
            if urlparse(candidate).path.rstrip("/"):
                url = candidate

    # Skip closed
    if "🔒" in full_row:
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

_USE_DB = bool(os.environ.get("DATABASE_URL"))


def load_seen_jobs(config: dict) -> dict[str, str]:
    """Load previously seen job URLs, pruning entries older than 48h."""
    if _USE_DB:
        from .db import load_seen_jobs as db_load
        return db_load()
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
    if _USE_DB:
        from .db import save_seen_jobs_bulk as db_save
        db_save(seen)
        return
    path = config["output_dir"] / "seen_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(dict(sorted(seen.items())), f, indent=2)


def deduplicate_results(
    results: list[tuple[JobPosting, 'FilterResult']],
    seen: dict[str, str],
) -> tuple[list[tuple[JobPosting, 'FilterResult']], dict[str, str]]:
    """Remove already-seen jobs. Returns (new_results, updated_seen_dict)."""
    new_results = []
    now = datetime.now(EST).isoformat()
    for job, filt in results:
        key = job.url if job.url else f"{job.company}_{job.title}"
        if key not in seen:
            seen[key] = now
            new_results.append((job, filt))
    return new_results, seen


def print_scan_results(results: list[tuple[JobPosting, 'FilterResult']]) -> None:
    """Pretty-print scan results in a table."""
    if not results:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    # Separate into apply / skip
    apply_jobs = [(j, r) for j, r in results if r.should_apply]
    skip_jobs = [(j, r) for j, r in results if not r.should_apply]

    if apply_jobs:
        table = Table(title=f"Relevant Jobs ({len(apply_jobs)})", border_style="green")
        table.add_column("#", style="dim", width=4)
        table.add_column("Company", style="cyan", max_width=15)
        table.add_column("Role", max_width=40)
        table.add_column("Location", max_width=20)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Variant", width=8)
        table.add_column("URL", max_width=50)

        for i, (job, filt) in enumerate(sorted(apply_jobs, key=lambda x: x[1].score, reverse=True), 1):
            table.add_row(
                str(i), job.company, job.title, job.location,
                str(filt.score), filt.resume_variant,
                job.url[:50] + "..." if len(job.url) > 50 else job.url,
            )
        console.print(table)

    if skip_jobs:
        console.print(f"\n[dim]Filtered out: {len(skip_jobs)} jobs (no sponsorship, senior-level, etc.)[/dim]")

    console.print(f"\n[bold]Total: {len(results)} scanned, {len(apply_jobs)} relevant, {len(skip_jobs)} skipped[/bold]")
