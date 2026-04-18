from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Job
from .state import State


def dedupe_and_window(
    jobs: list[Job], state: State, window_hours: int = 24
) -> list[Job]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    seen_ids = state.seen_ids()
    by_id: dict[str, Job] = {}

    for job in jobs:
        if job.posted_at is not None:
            if job.posted_at < cutoff:
                continue
        else:
            if job.id in seen_ids:
                continue
        existing = by_id.get(job.id)
        if existing is None:
            by_id[job.id] = job
            continue
        if _source_priority(job.source) < _source_priority(existing.source):
            by_id[job.id] = job

    return list(by_id.values())


_PRIORITY = {
    "greenhouse": 0,
    "lever": 1,
    "serpapi": 2,
    "rss": 3,
    "linkedin": 4,
    "naukri": 5,
}


def _source_priority(source: str) -> int:
    return _PRIORITY.get(source, 99)
