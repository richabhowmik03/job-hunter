from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape

from ..core.models import Profile, RawJob
from .base import get, truncate

SOURCE_NAME = "greenhouse"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return unescape(_TAG_RE.sub(" ", s or "")).strip()


def fetch(profile: Profile) -> list[RawJob]:
    out: list[RawJob] = []
    for company in profile.greenhouse_companies:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
        try:
            resp = get(url, params={"content": "true"})
        except Exception:
            continue
        data = resp.json()
        for job in data.get("jobs", []):
            posted = None
            if job.get("updated_at"):
                try:
                    posted = datetime.fromisoformat(
                        job["updated_at"].replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except Exception:
                    posted = None
            location = (job.get("location") or {}).get("name", "") or ""
            desc = truncate(_strip_html(job.get("content", "")), 4000)
            out.append(
                RawJob(
                    source=SOURCE_NAME,
                    source_job_id=str(job.get("id", "")),
                    url=job.get("absolute_url", ""),
                    title=job.get("title", ""),
                    company=company,
                    location=location,
                    remote="remote" in location.lower(),
                    description=desc,
                    posted_at=posted,
                    raw={"company_slug": company},
                )
            )
    return out
