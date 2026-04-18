from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from ..core.models import Profile, RawJob
from .base import get, truncate

SOURCE_NAME = "serpapi"


_NO_RESULTS_MSG = "google hasn't returned any results for this query"


def _json_or_fail(resp: requests.Response) -> dict | None:
    """Parse SerpAPI response. Returns None for empty-results (not an error),
    raises RuntimeError for quota exhaustion or real errors."""
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SerpAPI invalid JSON: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        msg = str(data["error"]).lower()
        if _NO_RESULTS_MSG in msg:
            return None  # not an error — just no jobs for this query
        raise RuntimeError(f"SerpAPI: {data['error']}")
    return data


def _parse_posted_ago(text: str) -> datetime | None:
    if not text:
        return None
    t = text.lower().strip()
    now = datetime.now(timezone.utc)
    try:
        num = int("".join(ch for ch in t if ch.isdigit()) or "0")
    except ValueError:
        return None
    if num == 0:
        return now
    if "hour" in t:
        return now - timedelta(hours=num)
    if "day" in t:
        return now - timedelta(days=num)
    if "minute" in t:
        return now - timedelta(minutes=num)
    return None


def fetch(profile: Profile) -> list[RawJob]:
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_KEY not set")

    out: list[RawJob] = []
    seen_ids: set[str] = set()
    for query in profile.serpapi_queries:
        resp = get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_jobs",
                "q": query,
                "hl": "en",
                "gl": "in",
                "api_key": api_key,
                "chips": "date_posted:today",
            },
        )
        data = _json_or_fail(resp)
        if data is None:
            continue  # no results for this query — not an error
        for job in data.get("jobs_results", []) or []:
            jid = job.get("job_id") or job.get("share_link") or ""
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            detected = job.get("detected_extensions", {}) or {}
            posted = _parse_posted_ago(detected.get("posted_at", ""))
            apply_url = ""
            for opt in job.get("apply_options", []) or []:
                if opt.get("link"):
                    apply_url = opt["link"]
                    break
            apply_url = apply_url or job.get("share_link", "")
            loc = job.get("location", "") or ""
            out.append(
                RawJob(
                    source=SOURCE_NAME,
                    source_job_id=jid,
                    url=apply_url,
                    title=job.get("title", ""),
                    company=job.get("company_name", ""),
                    location=loc,
                    remote="remote" in loc.lower() or bool(detected.get("work_from_home")),
                    description=truncate(job.get("description", ""), 4000),
                    posted_at=posted,
                    raw={"query": query, "via": job.get("via", "")},
                )
            )
    return out
