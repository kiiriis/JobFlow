"""Self-contained LinkedIn guest-API scraper.

Replaces python-jobspy's LinkedIn scraper, which had three defects that made
every scan return a small random sample instead of the full result set:

1. **Silent truncation on 429** — on the first rate-limit response it returned
   whatever partial list it had collected, with no retry and no signal that it
   was cut off.
2. **Pagination skip** — it advanced the search offset by the *cumulative*
   number of jobs collected across all pages instead of the page size, so it
   leapfrogged ever-larger ranges of results.
3. **Inline description fetching** — with linkedin_fetch_description=True it
   made one extra request per job, burning ~10x the request budget on JDs for
   jobs the store already had, which is what triggered the 429s to begin with.

This module fixes 1 and 2 and enables the fix for 3 by exposing the cheap
list phase (search_listings) separately from the expensive description phase
(fetch_description), so the caller can fetch descriptions only for jobs it
doesn't already have. See scanner.scan_linkedin for the orchestration.

Endpoints (public guest API, no auth):
    List: https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
    Job:  https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}
"""

import random
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.linkedin.com"
LIST_URL = f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = f"{BASE}/jobs-guest/jobs/api/jobPosting/{{job_id}}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# LinkedIn's guest search stops serving results past offset 1000.
MAX_START = 1000
# 429/999 backoff: honor Retry-After when present, else exponential.
MAX_RATE_LIMIT_RETRIES = 4
BACKOFF_START_S = 20
BACKOFF_CAP_S = 180
# Jitter between successive list pages within one search term.
PAGE_DELAY_S = (1.0, 2.2)


class LinkedInBlocked(Exception):
    """LinkedIn kept rate-limiting after all backoff retries — further
    requests in this scan are pointless; the caller should keep what it has."""


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _get(session: requests.Session, url: str, params: dict | None = None):
    """GET with rate-limit backoff.

    Retries 429 (and LinkedIn's 999 bot-block) with Retry-After/exponential
    waits; raises LinkedInBlocked when retries are exhausted. Transient
    network errors get a couple of short retries. Any other response is
    returned as-is for the caller to judge by status code.
    """
    backoff = BACKOFF_START_S
    network_errors = 0
    attempt = 0
    while True:
        try:
            resp = session.get(url, params=params, timeout=15)
        except requests.RequestException:
            network_errors += 1
            if network_errors > 2:
                return None
            time.sleep(3)
            continue
        if resp.status_code in (429, 999):
            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise LinkedInBlocked(
                    f"HTTP {resp.status_code} persisted after "
                    f"{MAX_RATE_LIMIT_RETRIES} backoff retries"
                )
            retry_after = 0
            try:
                retry_after = int(resp.headers.get("Retry-After", "") or 0)
            except ValueError:
                pass
            wait = min(retry_after or backoff, BACKOFF_CAP_S)
            time.sleep(wait + random.uniform(0, 3))
            backoff = min(backoff * 2, BACKOFF_CAP_S)
            attempt += 1
            continue
        return resp


def _parse_card(card) -> dict | None:
    """Parse one base-search-card div into a listing dict."""
    link = card.find("a", class_="base-card__full-link")
    if not (link and link.has_attr("href")):
        return None
    href = link["href"].split("?")[0]
    job_id = href.rstrip("/").split("-")[-1]
    if not job_id.isdigit():
        return None

    title_tag = card.find("span", class_="sr-only")
    title = title_tag.get_text(strip=True) if title_tag else ""

    company_tag = card.find("h4", class_="base-search-card__subtitle")
    company = company_tag.get_text(strip=True) if company_tag else ""

    loc_tag = card.find("span", class_="job-search-card__location")
    location = loc_tag.get_text(strip=True) if loc_tag else ""

    time_tag = card.find("time")
    date_posted = time_tag.get("datetime", "") if time_tag else ""

    return {
        "job_id": job_id,
        "url": f"{BASE}/jobs/view/{job_id}",
        "title": title,
        "company": company,
        "location": location,
        "date_posted": date_posted,  # YYYY-MM-DD from <time datetime=...>
    }


def search_listings(
    session: requests.Session,
    term: str,
    hours_old: int,
    results_wanted: int,
    seen_ids: set[str],
) -> list[dict]:
    """Collect listing dicts for one search term (no descriptions).

    Paginates by the number of cards actually served per page, deduping by
    job id via the shared ``seen_ids`` set so terms don't re-report each
    other's jobs. Stops on: enough results, an empty page (end of results),
    a non-200 page, or LinkedIn's offset cap. Raises LinkedInBlocked if the
    rate limiter never relents.
    """
    out: list[dict] = []
    start = 0
    while len(out) < results_wanted and start < MAX_START:
        params = {
            "keywords": term,
            "location": "United States",
            "pageNum": 0,
            "start": start,
        }
        if hours_old > 0:
            params["f_TPR"] = f"r{hours_old * 3600}"

        resp = _get(session, LIST_URL, params=params)
        if resp is None or resp.status_code != 200:
            break

        cards = BeautifulSoup(resp.text, "html.parser").find_all(
            "div", class_="base-search-card"
        )
        if not cards:
            break

        for card in cards:
            job = _parse_card(card)
            if not job or job["job_id"] in seen_ids:
                continue
            seen_ids.add(job["job_id"])
            out.append(job)

        # Advance by the page size actually served — NOT the cumulative
        # collected count (jobspy's bug), which skips ranges of results.
        start += len(cards)
        if len(out) < results_wanted and start < MAX_START:
            time.sleep(random.uniform(*PAGE_DELAY_S))

    return out[:results_wanted]


def fetch_description(session: requests.Session, job_id: str) -> str:
    """Fetch one job's full description as plain text ('' when unavailable)."""
    resp = _get(session, JOB_URL.format(job_id=job_id))
    if resp is None or resp.status_code != 200 or "linkedin.com/signup" in resp.url:
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    div = soup.find("div", class_=lambda c: c and "show-more-less-html__markup" in c)
    if div is None:
        return ""
    text = div.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)
