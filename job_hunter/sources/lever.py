from __future__ import annotations

from datetime import datetime, timezone

from ..core.models import Profile, RawJob
from .base import get, truncate

SOURCE_NAME = "lever"


def fetch(profile: Profile) -> list[RawJob]:
    out: list[RawJob] = []
    for company in profile.lever_companies:
        url = f"https://api.lever.co/v0/postings/{company}"
        try:
            resp = get(url, params={"mode": "json"})
        except Exception:
            continue
        for job in resp.json() or []:
            posted = None
            ts = job.get("createdAt")
            if isinstance(ts, (int, float)):
                posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            categories = job.get("categories", {}) or {}
            location = categories.get("location", "") or ""
            commitment = categories.get("commitment", "") or ""
            desc_parts = [
                job.get("descriptionPlain") or "",
                job.get("additionalPlain") or "",
            ]
            desc = truncate("\n\n".join(p for p in desc_parts if p), 4000)
            out.append(
                RawJob(
                    source=SOURCE_NAME,
                    source_job_id=str(job.get("id", "")),
                    url=job.get("hostedUrl") or job.get("applyUrl") or "",
                    title=job.get("text", ""),
                    company=company,
                    location=f"{location} {commitment}".strip(),
                    remote="remote" in location.lower(),
                    description=desc,
                    posted_at=posted,
                    raw={"company_slug": company},
                )
            )
    return out
