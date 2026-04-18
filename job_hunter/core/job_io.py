"""JSON round-trip for ``Job`` lists (pipeline checkpoints)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .models import Job


def jobs_to_jsonable(jobs: list[Job]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for j in jobs:
        d = asdict(j)
        pa = d.get("posted_at")
        if isinstance(pa, datetime):
            d["posted_at"] = pa.astimezone(timezone.utc).isoformat()
        out.append(d)
    return out


def jobs_from_jsonable(rows: list[dict[str, Any]]) -> list[Job]:
    jobs: list[Job] = []
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
        verdict = d.get("fit_verdict")
        v: Optional[Literal["strong", "maybe", "weak"]] = None
        if verdict in ("strong", "maybe", "weak"):
            v = verdict
        jobs.append(
            Job(
                id=str(d["id"]),
                source=str(d.get("source", "")),
                url=str(d.get("url", "")),
                title=str(d.get("title", "")),
                company=str(d.get("company", "")),
                location=str(d.get("location", "")),
                remote=bool(d.get("remote", False)),
                description=str(d.get("description", "")),
                posted_at=posted,
                seniority_hint=d.get("seniority_hint"),
                raw=raw_val,
                fit_score=d.get("fit_score"),
                fit_verdict=v,
                fit_reason=d.get("fit_reason"),
            )
        )
    return jobs


def save_jobs(path: Path, jobs: list[Job]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(jobs_to_jsonable(jobs), indent=0, ensure_ascii=False),
        encoding="utf-8",
    )


def load_jobs(path: Path) -> list[Job]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("jobs file must be a JSON array")
    return jobs_from_jsonable(data)
