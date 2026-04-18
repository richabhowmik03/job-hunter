from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser

from ..core.models import Profile, RawJob
from .base import truncate

SOURCE_NAME = "rss"

FEEDS = [
    ("RemoteOK", "https://remoteok.com/remote-jobs.rss"),
    ("WeWorkRemotely", "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    ("WWR-DataScience", "https://weworkremotely.com/remote-jobs/search.rss?term=data+scientist"),
    ("WWR-ML", "https://weworkremotely.com/remote-jobs/search.rss?term=machine+learning"),
]


def fetch(profile: Profile) -> list[RawJob]:
    out: list[RawJob] = []
    for label, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        for entry in parsed.entries:
            posted = None
            if getattr(entry, "published_parsed", None):
                posted = datetime.fromtimestamp(
                    mktime(entry.published_parsed), tz=timezone.utc
                )
            title = getattr(entry, "title", "")
            company = ""
            if ":" in title:
                company, _, rest = title.partition(":")
                title = rest.strip()
                company = company.strip()
            desc = truncate(getattr(entry, "summary", ""), 4000)
            link = getattr(entry, "link", "")
            out.append(
                RawJob(
                    source=SOURCE_NAME,
                    source_job_id=getattr(entry, "id", link) or link,
                    url=link,
                    title=title,
                    company=company or label,
                    location="Remote",
                    remote=True,
                    description=desc,
                    posted_at=posted,
                    raw={"feed": label},
                )
            )
    return out
