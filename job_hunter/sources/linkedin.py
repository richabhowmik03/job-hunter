from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from ..core.models import Profile, RawJob
from ..core.raw_job_io import load_raw_jobs

logger = logging.getLogger(__name__)

GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 10


def _ingest_path() -> Path | None:
    p = os.getenv("LINKEDIN_INGEST_PATH", "").strip()
    return Path(p) if p else None


def fetch(profile: Profile) -> list[RawJob]:
    """Use on-disk ingest and/or live guest API; one path can back up the other.

    - ``LINKEDIN_INGEST_PATH``: optional JSON from :func:`fetch_guest_for_scrape`.
    - ``LINKEDIN_PRIMARY`` (default ``ingest`` when this path is set, else behaves
      as live-only): ``ingest`` = prefer non-empty ingest file, then live; ``live``
      = try live first, then ingest if live raises or returns empty.

    If ingest is unset, only the live guest API runs.
    """
    path = _ingest_path()
    ingest_data: list[RawJob] | None = None
    if path:
        if path.is_file():
            try:
                ingest_data = load_raw_jobs(path)
                logger.info(
                    "LinkedIn: loaded %d job(s) from ingest %s",
                    len(ingest_data),
                    path,
                )
            except Exception as exc:
                logger.warning(
                    "LinkedIn: ingest load failed (%s); will try live guest API",
                    exc,
                )
                ingest_data = None
        else:
            logger.warning(
                "LinkedIn: LINKEDIN_INGEST_PATH set but file missing (%s); "
                "will try live guest API",
                path,
            )

    if not path:
        return fetch_guest_live(profile)

    primary = os.getenv("LINKEDIN_PRIMARY", "ingest").strip().lower()
    if primary not in ("ingest", "live"):
        primary = "ingest"

    if primary == "ingest":
        if ingest_data:
            return ingest_data
        return fetch_guest_live(profile)

    # primary == "live": try fresh guest listings first, fall back to ingest file.
    try:
        live = fetch_guest_live(profile)
        if live:
            return live
    except Exception as exc:
        if ingest_data:
            logger.warning(
                "LinkedIn: live guest API failed (%s); using ingest (%d job(s))",
                exc,
                len(ingest_data),
            )
            return ingest_data
        raise
    if ingest_data:
        logger.warning(
            "LinkedIn: live returned no jobs; using ingest (%d job(s))",
            len(ingest_data),
        )
        return ingest_data
    return []


def fetch_guest_live(profile: Profile) -> list[RawJob]:
    """Guest API only (no login). Paginates until empty or max pages."""
    return _fetch_guest_pages(profile, fetch_descriptions=True)


def fetch_guest_for_scrape(
    profile: Profile, *, fetch_descriptions: bool = True
) -> list[RawJob]:
    """Same as live fetch; used by scrape CLI to write ingest JSON."""
    return _fetch_guest_pages(profile, fetch_descriptions=fetch_descriptions)


def _fetch_guest_pages(
    profile: Profile, fetch_descriptions: bool
) -> list[RawJob]:
    max_pages = int(os.getenv("LINKEDIN_MAX_PAGES", str(DEFAULT_MAX_PAGES)))
    if max_pages < 1:
        max_pages = DEFAULT_MAX_PAGES

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    all_jobs: list[RawJob] = []
    seen_ids: set[str] = set()

    search_delay = float(os.getenv("LINKEDIN_SEARCH_DELAY", "3.0"))

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        first_query = True
        for title in profile.target_titles:
            for loc in profile.locations:
                if not first_query:
                    time.sleep(search_delay)
                first_query = False
                for page in range(max_pages):
                    if page > 0:
                        time.sleep(search_delay)
                    start = page * PAGE_SIZE
                    params = {
                        "keywords": title,
                        "location": loc,
                        "f_TPR": "r86400",
                        "sortBy": "DD",
                        "start": str(start),
                    }
                    try:
                        r = client.get(
                            GUEST_SEARCH_URL, params=params, headers=headers
                        )
                        r.raise_for_status()
                    except httpx.HTTPError as e:
                        logger.warning(
                            "LinkedIn guest search failed title=%r loc=%r start=%s: %s",
                            title,
                            loc,
                            start,
                            e,
                        )
                        break

                    cards = _parse_job_cards_html(r.text)
                    if not cards:
                        break

                    new_in_page = 0
                    for card in cards:
                        jid = card["job_id"]
                        if jid in seen_ids:
                            continue
                        seen_ids.add(jid)
                        new_in_page += 1
                        desc = ""
                        if fetch_descriptions:
                            time.sleep(0.35)
                            desc = _fetch_description(client, headers, jid)
                        all_jobs.append(
                            RawJob(
                                source="linkedin",
                                source_job_id=jid,
                                url=card["url"],
                                title=card["title"],
                                company=card["company"],
                                location=card.get("location") or loc,
                                remote=_infer_remote(
                                    card.get("location") or loc
                                ),
                                description=desc,
                                posted_at=_parse_posted(card.get("listed_at")),
                                raw={
                                    "search_title": title,
                                    "search_location": loc,
                                    "page": page,
                                    "start": start,
                                },
                            )
                        )

                    if new_in_page == 0 and cards:
                        break
                    if len(cards) < PAGE_SIZE:
                        break

    return all_jobs


def _parse_job_cards_html(html: str) -> list[dict[str, Any]]:
    """Extract job cards from guest search HTML.

    LinkedIn changed their markup — cards are now <div data-entity-urn=...>
    instead of <li class="jobs-search-results__list-item">.
    """
    # Split on each job card div
    blocks = re.split(r'(?=<div[^>]+data-entity-urn="urn:li:jobPosting:)', html)
    out: list[dict[str, Any]] = []
    for b in blocks:
        m = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', b)
        if not m:
            continue
        job_id = m.group(1)

        title_m = re.search(
            r'class="[^"]*base-search-card__title[^"]*"[^>]*>\s*([^<]+)', b
        )
        company_m = re.search(
            r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(?:\s*<[^>]+>)?\s*([^<]+)', b
        )
        loc_m = re.search(
            r'class="[^"]*job-search-card__location[^"]*"[^>]*>\s*([^<]+)', b
        )
        # Use datetime attribute on <time> — reliable and clean
        date_m = re.search(r'<time[^>]+datetime="([^"]+)"', b)

        title = title_m.group(1).strip() if title_m else ""
        company = company_m.group(1).strip() if company_m else ""
        location = loc_m.group(1).strip() if loc_m else ""
        listed_at = date_m.group(1).strip() if date_m else ""
        url = f"https://www.linkedin.com/jobs/view/{job_id}"
        out.append(
            {
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "listed_at": listed_at,
                "url": url,
            }
        )
    return out


def _fetch_description(
    client: httpx.Client, headers: dict[str, str], job_id: str
) -> str:
    url = (
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
        f"{quote_plus(job_id)}"
    )
    try:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return _strip_html(r.text)
    except httpx.HTTPError:
        return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _infer_remote(location: str) -> bool:
    loc = location.lower()
    return "remote" in loc or "work from home" in loc


def _parse_posted(listed_at: str) -> datetime | None:
    if not listed_at:
        return None
    s = listed_at.strip().lower()
    now = datetime.now(timezone.utc)
    # ISO date from <time datetime="YYYY-MM-DD">
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        try:
            from datetime import timedelta
            d = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d
        except ValueError:
            return None
    if "hour" in s or "minute" in s or "just now" in s:
        return now
    if "day" in s:
        m = re.search(r"(\d+)", s)
        if m:
            try:
                from datetime import timedelta
                return now - timedelta(days=int(m.group(1)))
            except ValueError:
                return None
    return None
