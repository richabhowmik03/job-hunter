"""On-disk checkpoints so UI runs can resume after a late failure (no full re-fetch)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .job_io import load_jobs, save_jobs
from .models import Job, SourceResult
from .raw_job_io import raw_jobs_from_jsonable, raw_jobs_to_jsonable

CHECKPOINT_SUBDIR = ".pipeline_checkpoint"
META_NAME = "meta.json"
SRC_NAME = "source_results.json"
JOBS_RULES_NAME = "jobs_after_rules.json"
JOBS_FINAL_NAME = "jobs_final.json"
REPORT_NAME = "report.html"

PHASE_AFTER_SOURCES = "after_sources"
PHASE_AFTER_RULES = "after_rules"
PHASE_READY_FINALIZE = "ready_to_finalize"

Meta = dict[str, Any]


def checkpoint_dir(root: Path) -> Path:
    return root / CHECKPOINT_SUBDIR


def clear_checkpoint(root: Path) -> None:
    d = checkpoint_dir(root)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=0, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_results_to_jsonable(srs: list[SourceResult]) -> list[dict[str, Any]]:
    return [
        {
            "source": sr.source,
            "status": sr.status,
            "jobs": raw_jobs_to_jsonable(sr.jobs),
            "error": sr.error,
            "duration_ms": sr.duration_ms,
        }
        for sr in srs
    ]


def source_results_from_jsonable(rows: list[dict[str, Any]]) -> list[SourceResult]:
    out: list[SourceResult] = []
    for r in rows:
        status = r.get("status", "ok")
        if status not in ("ok", "error", "empty"):
            status = "error"
        out.append(
            SourceResult(
                source=str(r.get("source", "")),
                status=status,  # type: ignore[arg-type]
                jobs=raw_jobs_from_jsonable(r.get("jobs") or []),
                error=r.get("error"),
                duration_ms=int(r.get("duration_ms", 0)),
            )
        )
    return out


@dataclass
class CheckpointBundle:
    version: int
    phase: str
    started_at: datetime
    after_window: int = 0
    after_rules: int = 0
    after_llm: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    discovery_result: dict[str, list[str]] = field(
        default_factory=lambda: {"new_greenhouse": [], "new_lever": []}
    )
    source_results: list[SourceResult] = field(default_factory=list)
    jobs_after_rules: list[Job] = field(default_factory=list)
    jobs_final: list[Job] = field(default_factory=list)
    report_html: str | None = None
    subject: str | None = None
    email_step_done: bool = False
    send_smtp: bool = False


def has_checkpoint(root: Path) -> bool:
    mp = checkpoint_dir(root) / META_NAME
    return mp.is_file()


def load_checkpoint_bundle(root: Path) -> CheckpointBundle | None:
    base = checkpoint_dir(root)
    meta_path = base / META_NAME
    if not meta_path.is_file():
        return None
    try:
        raw = _read_json(meta_path)
        if not isinstance(raw, dict):
            return None
        ver = int(raw.get("version", 0))
        if ver != 1:
            return None
        phase = str(raw.get("phase", ""))
        if phase not in (
            PHASE_AFTER_SOURCES,
            PHASE_AFTER_RULES,
            PHASE_READY_FINALIZE,
        ):
            return None
        sa = raw.get("started_at")
        if not sa or not isinstance(sa, str):
            return None
        try:
            started = datetime.fromisoformat(sa.replace("Z", "+00:00"))
        except ValueError:
            return None
        fc = raw.get("failure_counts")
        if not isinstance(fc, dict):
            fc = {}
        failure_counts = {str(k): int(v) for k, v in fc.items() if str(k)}
        dr = raw.get("discovery_result")
        if not isinstance(dr, dict):
            dr = {"new_greenhouse": [], "new_lever": []}
        discovery_result = {
            "new_greenhouse": list(dr.get("new_greenhouse") or []),
            "new_lever": list(dr.get("new_lever") or []),
        }
        b = CheckpointBundle(
            version=ver,
            phase=phase,
            started_at=started,
            after_window=int(raw.get("after_window", 0)),
            after_rules=int(raw.get("after_rules", 0)),
            after_llm=int(raw.get("after_llm", 0)),
            failure_counts=failure_counts,
            discovery_result=discovery_result,  # type: ignore[arg-type]
            email_step_done=bool(raw.get("email_step_done", False)),
            send_smtp=bool(raw.get("send_smtp", False)),
        )
        sr_path = base / SRC_NAME
        if sr_path.is_file():
            data = _read_json(sr_path)
            if isinstance(data, list):
                b.source_results = source_results_from_jsonable(data)
        if phase in (PHASE_AFTER_RULES, PHASE_READY_FINALIZE):
            p = base / JOBS_RULES_NAME
            if p.is_file():
                b.jobs_after_rules = load_jobs(p)
        if phase == PHASE_READY_FINALIZE:
            p = base / JOBS_FINAL_NAME
            if p.is_file():
                b.jobs_final = load_jobs(p)
            rp = base / REPORT_NAME
            if rp.is_file():
                b.report_html = rp.read_text(encoding="utf-8")
            b.subject = raw.get("subject") if isinstance(raw.get("subject"), str) else None
        return b
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_checkpoint_after_sources(
    root: Path,
    *,
    started: datetime,
    source_results: list[SourceResult],
    failure_counts: dict[str, int],
) -> None:
    base = checkpoint_dir(root)
    meta: Meta = {
        "version": 1,
        "phase": PHASE_AFTER_SOURCES,
        "started_at": started.astimezone(timezone.utc).isoformat(),
        "failure_counts": failure_counts,
    }
    _write_json(base / META_NAME, meta)
    _write_json(base / SRC_NAME, source_results_to_jsonable(source_results))


def save_checkpoint_after_rules(
    root: Path,
    *,
    started: datetime,
    after_window: int,
    after_rules: int,
    jobs: list[Job],
    source_results: list[SourceResult],
    failure_counts: dict[str, int],
) -> None:
    base = checkpoint_dir(root)
    meta: Meta = {
        "version": 1,
        "phase": PHASE_AFTER_RULES,
        "started_at": started.astimezone(timezone.utc).isoformat(),
        "after_window": after_window,
        "after_rules": after_rules,
        "failure_counts": failure_counts,
    }
    _write_json(base / META_NAME, meta)
    _write_json(base / SRC_NAME, source_results_to_jsonable(source_results))
    save_jobs(base / JOBS_RULES_NAME, jobs)


def save_checkpoint_ready_finalize(
    root: Path,
    *,
    started: datetime,
    after_window: int,
    after_rules: int,
    after_llm: int,
    discovery_result: dict[str, list[str]],
    jobs_final: list[Job],
    source_results: list[SourceResult],
    failure_counts: dict[str, int],
    report_html: str,
    subject: str,
    email_step_done: bool,
    send_smtp: bool,
) -> None:
    base = checkpoint_dir(root)
    meta: Meta = {
        "version": 1,
        "phase": PHASE_READY_FINALIZE,
        "started_at": started.astimezone(timezone.utc).isoformat(),
        "after_window": after_window,
        "after_rules": after_rules,
        "after_llm": after_llm,
        "failure_counts": failure_counts,
        "discovery_result": discovery_result,
        "subject": subject,
        "email_step_done": email_step_done,
        "send_smtp": send_smtp,
    }
    _write_json(base / META_NAME, meta)
    _write_json(base / SRC_NAME, source_results_to_jsonable(source_results))
    save_jobs(base / JOBS_FINAL_NAME, jobs_final)
    _atomic_write_text(base / REPORT_NAME, report_html)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
