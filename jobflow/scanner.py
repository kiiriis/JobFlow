"""Scan job boards from JobBoards_Links.json and return matching jobs."""

import json
import random
import re
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .filter import evaluate_job
from .models import JobPosting

console = Console()

# Build a default SSL context and URL opener with browser-like headers
_SSL_CTX = ssl.create_default_context()
_OPENER = urllib.request.build_opener()
_OPENER.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"),
    ("Accept", "application/json"),
]


def _fetch_json(url: str, retries: int = 3) -> dict | list | None:
    """Fetch JSON from a URL using stdlib urllib with retry/backoff."""
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


# Role must be software/engineering related
SWE_ROLE_KEYWORDS = [
    "software", "engineer", "developer", "sde", "swe", "backend",
    "frontend", "full stack", "fullstack", "full-stack", "platform",
    "infrastructure", "devops", "systems engineer", "data engineer",
    "machine learning", "ml engineer", "ai engineer", "applied scientist",
    "research engineer", "security engineer", "site reliability",
    "cloud engineer", "distributed systems",
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
    """Scan a Lever company's job board via API."""
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
    """Scan a Greenhouse company's job board via API."""
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
    """Scan an Ashby company's job board via API."""
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
    """
    Scan all API-based job boards (Lever, Greenhouse, Ashby).
    Returns list of (JobPosting, FilterResult) tuples for relevant jobs.
    max_age_hours: only include jobs posted within this many hours (0 = no limit).
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

    # Scan LinkedIn guest API
    if not platforms or "linkedin" in platforms:
        aggregators = boards.get("job_aggregators", {})
        linkedin = aggregators.get("linkedin", {})
        if linkedin:
            console.print(f"\n[bold cyan]Scanning LinkedIn (guest API)...[/bold cyan]")
            jobs = scan_linkedin_guest(linkedin, keywords, max_age_hours)
            console.print(f"  [green]{len(jobs)} matches[/green]")
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
# LinkedIn guest API scanner
# ---------------------------------------------------------------------------

def scan_linkedin_guest(linkedin_config: dict, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan LinkedIn using the guest API (HTML fragments, no auth)."""
    base = linkedin_config.get("guest_api_base", "")
    if not base:
        return []

    queries = [
        "new%20grad%20software%20engineer%202026",
        "entry%20level%20software%20engineer",
        "SDE%20I%20new%20grad",
        "junior%20software%20engineer",
    ]

    all_jobs = []
    seen_titles = set()

    for query in queries:
        # Fetch first 2 pages (25 results each)
        for offset in [0, 25]:
            url = f"{base}?keywords={query}&location=United%20States&f_E=2&f_JT=F&sortBy=DD&start={offset}"
            html = _fetch_text(url)
            if not html:
                continue

            jobs = _parse_linkedin_html(html, include_kw, seen_titles)
            all_jobs.extend(jobs)

            # Small delay between requests to avoid rate limiting
            time.sleep(random.uniform(0.5, 1.5))

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


def _parse_linkedin_html(html: str, include_kw: list[str], seen: set) -> list[JobPosting]:
    """Parse LinkedIn guest API HTML fragments into JobPosting objects."""
    jobs = []

    # LinkedIn guest API returns <li> cards with job data
    # Extract job cards using regex patterns on the HTML
    cards = re.findall(
        r'<div class="base-card.*?</div>\s*</div>\s*</div>',
        html, re.DOTALL
    )
    if not cards:
        # Try alternate pattern
        cards = re.findall(r'<li>.*?</li>', html, re.DOTALL)

    for card in cards:
        # Extract title
        title_m = re.search(r'class="base-search-card__title"[^>]*>(.*?)</(?:h3|span|a)', card, re.DOTALL)
        if not title_m:
            title_m = re.search(r'<h3[^>]*>(.*?)</h3>', card, re.DOTALL)
        if not title_m:
            continue
        title = _strip_html(title_m.group(1)).strip()

        # Dedup
        if title.lower() in seen:
            continue
        seen.add(title.lower())

        # Must be SWE role
        if not _is_swe_role(title):
            continue

        # Extract company
        company_m = re.search(r'class="base-search-card__subtitle"[^>]*>(.*?)</(?:h4|span|a)', card, re.DOTALL)
        if not company_m:
            company_m = re.search(r'<h4[^>]*>(.*?)</h4>', card, re.DOTALL)
        company = _strip_html(company_m.group(1)).strip() if company_m else ""

        # Extract location
        loc_m = re.search(r'class="job-search-card__location"[^>]*>(.*?)</', card, re.DOTALL)
        location = _strip_html(loc_m.group(1)).strip() if loc_m else ""

        # Extract URL
        url_m = re.search(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)', card)
        url = url_m.group(1) if url_m else ""

        if title and company:
            jobs.append(JobPosting(
                url=url,
                title=title,
                company=company,
                location=location,
                description=title,  # Guest API doesn't give full JD
            ))

    return jobs


# ---------------------------------------------------------------------------
# GitHub new-grad repo scanner
# ---------------------------------------------------------------------------

def scan_github_repos(repos_config: dict, include_kw: list[str]) -> list[JobPosting]:
    """Scan GitHub new-grad repos by parsing README markdown tables."""
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
    """Parse job tables from SimplifyJobs (HTML <tr>/<td>) and Jobright (markdown |) READMEs."""
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

    # Extract application URL
    url = ""
    full_row = " ".join(cols)
    href_matches = re.findall(r'href="(https?://[^"]+)"', full_row)
    for href in href_matches:
        if "simplify.jobs/c/" not in href and "simplify.jobs/p/" not in href:
            url = href.split("?utm_source")[0]
            break
    if not url:
        # Markdown link fallback
        url_m = re.search(r'\[.*?\]\((https?://[^)]+)\)', full_row)
        if url_m and "simplify.jobs/c/" not in url_m.group(1):
            url = url_m.group(1).split("?utm_source")[0]

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

def load_seen_jobs(config: dict) -> set:
    """Load previously seen job URLs from seen_jobs.json."""
    path = config["output_dir"] / "seen_jobs.json"
    if path.exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(config: dict, seen: set) -> None:
    """Save seen job URLs to seen_jobs.json."""
    path = config["output_dir"] / "seen_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def deduplicate_results(
    results: list[tuple[JobPosting, 'FilterResult']],
    seen: set,
) -> tuple[list[tuple[JobPosting, 'FilterResult']], set]:
    """Remove already-seen jobs. Returns (new_results, updated_seen_set)."""
    new_results = []
    for job, filt in results:
        # Use URL + title as dedup key
        key = job.url if job.url else f"{job.company}_{job.title}"
        if key not in seen:
            seen.add(key)
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
