from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..core.models import Profile, RawJob
from .base import get, truncate

SOURCE_NAME = "naukri"

_API_URL = "https://www.naukri.com/jobapi/v2/search"
_API_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.naukri.com/",
    "appid": "109",
    "systemid": "Naukri",
}

_CTC_FILTERS = "10to15,15to25,25to50,50to75"

_LOC_MAP = {
    "bangalore": "bangalore",
    "bengaluru": "bangalore",
    "hyderabad": "hyderabad",
    "delhi": "delhi",
    "delhi ncr": "delhi-ncr",
    "mumbai": "mumbai",
    "pune": "pune",
    "chennai": "chennai",
    "india": "",
    "remote": "",
}


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        # Format: "2026-04-17 15:34:33.0"
        return datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_page(keyword: str, location: str, experience: int | None, page: int) -> list[dict]:
    params: dict[str, str | int] = {
        "keyword": keyword,
        "noOfResults": 20,
        "pageNo": page,
        "sort": "1",        # sort by date
        "jobAge": 1,        # posted in last 24h
        "wfhType": "0,3",   # on-site + remote/hybrid
        "ctcFilter": _CTC_FILTERS,
    }
    if location:
        params["location"] = location
    if experience:
        params["experience"] = experience
    try:
        r = get(_API_URL, params=params, headers=_API_HEADERS)  # type: ignore[arg-type]
        data = r.json()
        return data.get("list", [])
    except Exception:
        return []


def fetch(profile: Profile) -> list[RawJob]:
    titles = profile.target_titles + profile.related_titles
    experience: int | None = profile.seniority.min_years or None

    # Deduplicate locations using the mapping
    seen_locs: set[str] = set()
    locations: list[str] = []
    for loc in profile.locations:
        mapped = _LOC_MAP.get(loc.lower(), "")
        if mapped not in seen_locs:
            seen_locs.add(mapped)
            locations.append(mapped)

    out: list[RawJob] = []
    seen_ids: set[str] = set()

    for title in titles:
        for loc in locations:
            for page in range(1, 4):  # up to 3 pages = 60 jobs per title+loc
                jobs = _fetch_page(title, loc, experience, page)
                if not jobs:
                    break
                new_this_page = 0
                for j in jobs:
                    job_id = str(j.get("jobId", ""))
                    if not job_id or job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                    new_this_page += 1

                    desc_raw = j.get("jobDesc") or j.get("tupleDesc") or ""
                    desc = _strip_html(desc_raw)
                    exp_line = f"Experience: {j.get('minExp', '')}-{j.get('maxExp', '')} years"
                    loc_text = j.get("cityfield", "").strip() or loc
                    wfh = str(j.get("wfhType", ""))
                    is_remote = "3" in wfh or "remote" in loc_text.lower()

                    out.append(RawJob(
                        source=SOURCE_NAME,
                        source_job_id=job_id,
                        url=j.get("urlStr") or f"https://www.naukri.com/job-listings-{job_id}",
                        title=j.get("post") or "",
                        company=j.get("companyName") or j.get("CONTCOM") or "",
                        location=loc_text,
                        remote=is_remote,
                        description=truncate(f"{exp_line}\n{desc}", 4000),
                        posted_at=_parse_date(j.get("addDate") or ""),
                        raw={"query": title, "location": loc},
                    ))
                if new_this_page == 0 or len(jobs) < 20:
                    break
                time.sleep(1.0)
            time.sleep(1.5)

    return out
