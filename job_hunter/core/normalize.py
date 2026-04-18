from __future__ import annotations

import hashlib

from .models import Job, RawJob


def _stable_id(raw: RawJob) -> str:
    basis = raw.url or f"{raw.source}:{raw.source_job_id}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def normalize(raws: list[RawJob]) -> list[Job]:
    return [
        Job(
            id=_stable_id(r),
            source=r.source,
            url=r.url,
            title=r.title.strip(),
            company=r.company.strip(),
            location=r.location.strip(),
            remote=r.remote,
            description=r.description,
            posted_at=r.posted_at,
            raw=r.raw,
        )
        for r in raws
        if r.title and r.url
    ]
