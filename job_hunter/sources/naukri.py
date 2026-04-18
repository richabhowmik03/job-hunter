from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..core.models import Profile, RawJob
from .base import get, truncate

SOURCE_NAME = "naukri"

# wfhType=0 = on-site, wfhType=3 = remote/hybrid — include both
_WFH_PARAMS = ["0", "3"]
# CTC bands in LPA
_CTC_FILTERS = ["10to15", "15to25", "25to50", "50to75"]

# Bangalore and Bengaluru share one combined Naukri slug
_LOC_SLUG_MAP = {
    "bangalore": "bangalore-bengaluru",
    "bengaluru": "bangalore-bengaluru",
    "hyderabad": "hyderabad",
    "delhi": "delhi",
    "delhi ncr": "delhi-ncr",
    "mumbai": "mumbai",
    "pune": "pune",
    "chennai": "chennai",
    "india": "",   # no city suffix → national search
    "remote": "",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _loc_slug(loc: str) -> str:
    return _LOC_SLUG_MAP.get(loc.lower(), "")


def _build_url(title: str, loc_slug: str, experience: int | None) -> str:
    title_slug = _slug(title)
    if loc_slug:
        path = f"https://www.naukri.com/{title_slug}-jobs-in-{loc_slug}"
    else:
        path = f"https://www.naukri.com/{title_slug}-jobs"
    params: list[tuple[str, str]] = []
    for wfh in _WFH_PARAMS:
        params.append(("wfhType", wfh))
    if experience:
        params.append(("experience", str(experience)))
    for ctc in _CTC_FILTERS:
        params.append(("ctcFilter", ctc))
    return f"{path}?{urlencode(params)}"


def _parse_age(text: str) -> datetime | None:
    if not text:
        return None
    t = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "just now" in t or "few" in t or "today" in t:
        return now
    m = re.search(r"(\d+)\s*(hour|day|minute)", t)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2)
    if unit == "hour":
        return now - timedelta(hours=num)
    if unit == "minute":
        return now - timedelta(minutes=num)
    if unit == "day":
        return now - timedelta(days=num)
    return None


def _scrape_page(url: str, title: str, seen: set[str]) -> list[RawJob]:
    try:
        resp = get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.naukri.com/",
            },
        )
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = (
        soup.select("div.srp-jobtuple-wrapper")
        or soup.select("article.jobTuple")
        or soup.select("div.jobTuple")
    )

    out: list[RawJob] = []
    for card in cards:
        link_el = card.select_one("a.title") or card.select_one("a.jobtitle") or card.select_one("a")
        if not link_el or not link_el.get("href"):
            continue
        job_url = link_el["href"]
        m = re.search(r"-(\d+)(?:\?|$)", job_url)
        job_id = m.group(1) if m else job_url
        if job_id in seen:
            continue
        seen.add(job_id)

        company_el = card.select_one("a.comp-name") or card.select_one(".companyInfo")
        loc_el = card.select_one("span.locWdth") or card.select_one(".location")
        exp_el = card.select_one("span.expwdth") or card.select_one(".experience")
        desc_el = card.select_one("span.job-desc") or card.select_one(".job-description")
        age_el = card.select_one("span.job-post-day") or card.select_one(".postedDate")

        posted = _parse_age(age_el.get_text(" ", strip=True) if age_el else "")
        if not posted or posted < datetime.now(timezone.utc) - timedelta(hours=30):
            continue

        loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        is_remote = bool(re.search(r"\b(remote|work from home|wfh)\b", loc_text.lower()))

        desc_parts = []
        if exp_el:
            desc_parts.append(f"Experience: {exp_el.get_text(' ', strip=True)}")
        if desc_el:
            desc_parts.append(desc_el.get_text(" ", strip=True))

        out.append(
            RawJob(
                source=SOURCE_NAME,
                source_job_id=job_id,
                url=job_url,
                title=link_el.get_text(" ", strip=True),
                company=(company_el.get_text(" ", strip=True) if company_el else ""),
                location=loc_text,
                remote=is_remote,
                description=truncate("\n".join(desc_parts), 4000),
                posted_at=posted,
                raw={"query": title},
            )
        )
    return out


def fetch(profile: Profile) -> list[RawJob]:
    out: list[RawJob] = []
    seen: set[str] = set()
    titles = profile.target_titles + profile.related_titles
    experience: int | None = profile.seniority.min_years or None

    # Deduplicate slugs — Bangalore+Bengaluru both map to "bangalore-bengaluru"
    loc_slugs: list[str] = []
    seen_slugs: set[str] = set()
    for loc in profile.locations:
        s = _loc_slug(loc)
        if s not in seen_slugs:
            seen_slugs.add(s)
            loc_slugs.append(s)

    for title in titles:
        for loc_slug in loc_slugs:
            url = _build_url(title, loc_slug, experience)
            jobs = _scrape_page(url, title, seen)
            out.extend(jobs)
            time.sleep(1.5)

    return out
