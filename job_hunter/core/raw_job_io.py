"""Serialize RawJob lists to JSON for ingest between scrape and pipeline runs."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import RawJob


def raw_jobs_to_jsonable(jobs: list[RawJob]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for j in jobs:
        d = asdict(j)
        if d.get("posted_at") is not None:
            pa = d["posted_at"]
            if isinstance(pa, datetime):
                d["posted_at"] = pa.astimezone(timezone.utc).isoformat()
        out.append(d)
    return out


def raw_jobs_from_jsonable(rows: list[dict[str, Any]]) -> list[RawJob]:
    jobs: list[RawJob] = []
    for d in rows:
        pa = d.get("posted_at")
        posted: datetime | None = None
        if pa:
            if isinstance(pa, str):
                try:
                    posted = datetime.fromisoformat(pa.replace("Z", "+00:00"))
                except ValueError:
                    posted = None
            elif isinstance(pa, datetime):
                posted = pa
        raw_val = d.get("raw") or {}
        if not isinstance(raw_val, dict):
            raw_val = {}
        jobs.append(
            RawJob(
                source=str(d.get("source", "")),
                source_job_id=str(d.get("source_job_id", "")),
                url=str(d.get("url", "")),
                title=str(d.get("title", "")),
                company=str(d.get("company", "")),
                location=str(d.get("location", "")),
                remote=bool(d.get("remote", False)),
                description=str(d.get("description", "")),
                posted_at=posted,
                raw=raw_val,
            )
        )
    return jobs


def save_raw_jobs(path: Path, jobs: list[RawJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw_jobs_to_jsonable(jobs), indent=0, ensure_ascii=False),
        encoding="utf-8",
    )


def load_raw_jobs(path: Path) -> list[RawJob]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Ingest file must be a JSON array")
    return raw_jobs_from_jsonable(data)
