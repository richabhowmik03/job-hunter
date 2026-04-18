from __future__ import annotations

import re

from .models import Job, Profile


def _title_match(title: str, allowed: list[str]) -> bool:
    t = title.lower()
    for phrase in allowed:
        p = phrase.lower()
        if re.search(rf"\b{re.escape(p)}\b", t):
            return True
        if all(tok in t for tok in p.split()):
            return True
    return False


def _location_match(job: Job, locations: list[str]) -> bool:
    if not locations:
        return True
    # Any remote job is reachable regardless of which country's remote it says.
    if job.remote:
        return True
    loc = job.location.lower()
    return any(l.lower() in loc for l in locations if l.lower() != "remote")


def filter_jobs(jobs: list[Job], profile: Profile) -> list[Job]:
    allowed_titles = profile.target_titles + profile.related_titles
    reject_terms = [r.lower() for r in profile.seniority.reject_if_title_contains]
    deal_breakers = [d.lower() for d in profile.deal_breakers]

    out: list[Job] = []
    for job in jobs:
        title_lc = job.title.lower()
        if any(term in title_lc for term in reject_terms):
            continue
        if not _title_match(job.title, allowed_titles):
            continue
        if not _location_match(job, profile.locations):
            continue
        desc_lc = job.description.lower()
        if any(db in desc_lc for db in deal_breakers):
            continue
        out.append(job)
    return out
