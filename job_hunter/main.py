from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "job_hunter"

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from .core.company_discovery import discover_companies
from .core.filter_window import dedupe_and_window
from .core.fit_scorer import score_jobs
from .core.health import build_health
from .core.models import Job, Profile, SourceResult
from .core.normalize import _stable_id, normalize
from .core.notify_email import render_html, send_email
from .core.pipeline_checkpoint import (
    CheckpointBundle,
    PHASE_AFTER_RULES,
    PHASE_AFTER_SOURCES,
    PHASE_READY_FINALIZE,
    clear_checkpoint,
    load_checkpoint_bundle,
    save_checkpoint_after_rules,
    save_checkpoint_after_sources,
    save_checkpoint_ready_finalize,
)
from .core.profile_loader import load_config, load_profile
from .core.rules import filter_jobs
from .core.state import State
from .sources import load_sources

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env(root: Path) -> None:
    """Load ``root/.env`` then project root ``.env`` (later files do not override)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in (root / ".env", _project_root() / ".env"):
        if path.is_file():
            load_dotenv(path, override=False)


def _run_source(name: str, fn, profile: Profile) -> SourceResult:
    start = time.monotonic()
    try:
        jobs = fn(profile) or []
        status = "ok" if jobs else "empty"
        return SourceResult(
            source=name,
            status=status,
            jobs=jobs,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return SourceResult(
            source=name,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def _restore_env_vars(_env_overrides: dict[str, str | None]) -> None:
    for k, v in _env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _resume_ready_finalize_run(
    root: Path,
    state: State,
    profile: Profile,
    bundle: CheckpointBundle,
    started: datetime,
    *,
    dry_run: bool,
    send_smtp: bool,
    email_to: str | None,
    progress_hook: Callable[[dict[str, Any]], None] | None,
    _env_overrides: dict[str, str | None],
) -> dict[str, Any]:
    def _ph(payload: dict[str, Any]) -> None:
        if progress_hook:
            progress_hook(payload)

    source_results = bundle.source_results
    failure_counts = bundle.failure_counts
    jobs = bundle.jobs_final
    after_window = bundle.after_window
    after_rules = bundle.after_rules
    after_llm = bundle.after_llm
    discovery_result = bundle.discovery_result
    report_html = bundle.report_html
    subject = bundle.subject or ""

    _ph(
        {
            "phase": "resume",
            "message": "Resuming from last step (email/report already prepared)…",
        }
    )

    health = build_health(source_results, failure_counts)
    email_sent = False
    report_out: str | None = None

    if send_smtp:
        if not bundle.email_step_done and email_to and report_html:
            _ph(
                {
                    "phase": "email",
                    "message": "Sending results email…",
                    "dry_run": dry_run,
                }
            )
            send_email(report_html, subject, dry_run=dry_run, to_addr=email_to)
        email_sent = not dry_run
    else:
        report_out = report_html
        _ph(
            {
                "phase": "email",
                "message": "Skipping email send; report available as HTML download.",
            }
        )

    if not dry_run:
        logger.info("Marking jobs as seen in state…")
        state.mark_seen(
            [
                (_stable_id(j), j.source)
                for sr in source_results
                for j in sr.jobs
                if j.title and j.url
            ]
        )
    state.record_run(
        started,
        datetime.now(timezone.utc),
        {sr.source: sr.status for sr in source_results},
    )
    state.close()
    logger.info("Run recorded; state DB closed.")

    if not dry_run:
        clear_checkpoint(root)

    line = (
        f"window={after_window} rules={after_rules} llm={after_llm} "
        f"sent={len(jobs)} sources={len(source_results)} "
        f"discovered=+{len(discovery_result['new_greenhouse'])}gh "
        f"+{len(discovery_result['new_lever'])}lv"
    )
    print(line)
    _ph({"phase": "done", "message": "Finished."})
    out: dict[str, Any] = {
        "window": after_window,
        "rules": after_rules,
        "llm": after_llm,
        "sent": len(jobs),
        "sources": len(source_results),
        "new_greenhouse": len(discovery_result["new_greenhouse"]),
        "new_lever": len(discovery_result["new_lever"]),
        "email_sent": email_sent,
        "line": line,
    }
    if report_out is not None:
        out["report_html"] = report_out
        out["report_subject"] = subject

    _restore_env_vars(_env_overrides)
    return out


def run(
    root: Path,
    dry_run: bool = False,
    *,
    verbose: bool = False,
    discover: bool = True,
    email_to: str | None = None,
    send_smtp: bool = True,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    llm_keys: dict[str, str] | None = None,
    source_keys: dict[str, str] | None = None,
    resume_from_checkpoint: bool = False,
) -> dict[str, Any]:
    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            datefmt="%H:%M:%S",
            force=True,
        )
        for noisy in ("httpx", "httpcore", "groq"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING, force=True)

    def _ph(payload: dict[str, Any]) -> None:
        if progress_hook:
            progress_hook(payload)

    _load_env(root)

    # Per-request source keys (BYOK). os.environ is process-global, so UI runs
    # should be serialized by the caller (server.py holds a lock for this).
    _env_overrides: dict[str, str | None] = {}
    if source_keys:
        _env_map = {"serpapi": "SERPAPI_KEY"}
        for name, key in source_keys.items():
            env_name = _env_map.get(name)
            if env_name and key:
                _env_overrides[env_name] = os.environ.get(env_name)
                os.environ[env_name] = key
    logger.info(
        "Starting run (root=%s, dry_run=%s, discover=%s, resume=%s)",
        root,
        dry_run,
        discover,
        resume_from_checkpoint,
    )
    _ph({"phase": "config", "message": "Loading profile and configuration…"})
    config = load_config(root / "config.yaml")
    profile = load_profile(root / "profiles", config["profile"])
    enabled = config.get("enabled_sources", [])
    sources = load_sources(enabled)
    logger.info(
        "Loaded profile %r; enabled sources: %s",
        config["profile"],
        ", ".join(sources.keys()) or "(none)",
    )

    resume_bundle: CheckpointBundle | None = None
    if resume_from_checkpoint:
        resume_bundle = load_checkpoint_bundle(root)
        if not resume_bundle:
            raise ValueError(
                "No saved pipeline checkpoint for this workspace. "
                "Run a full job first; resume only appears if a step failed mid-run."
            )

    started = (
        resume_bundle.started_at if resume_bundle else datetime.now(timezone.utc)
    )

    state = State(root / "state.db")
    logger.info("Opened state DB: %s", root / "state.db")

    if resume_bundle and resume_bundle.phase == PHASE_READY_FINALIZE:
        return _resume_ready_finalize_run(
            root,
            state,
            profile,
            resume_bundle,
            started,
            dry_run=dry_run,
            send_smtp=send_smtp,
            email_to=email_to,
            progress_hook=progress_hook,
            _env_overrides=_env_overrides,
        )

    skip_to_llm = bool(
        resume_bundle and resume_bundle.phase == PHASE_AFTER_RULES
    )

    source_results: list[SourceResult] = []
    failure_counts: dict[str, int] = {}
    jobs: list[Job] = []
    after_window = 0
    after_rules = 0

    if skip_to_llm:
        assert resume_bundle is not None
        br = resume_bundle
        source_results = br.source_results
        failure_counts = dict(br.failure_counts)
        jobs = list(br.jobs_after_rules)
        after_window = br.after_window
        after_rules = br.after_rules
        _ph(
            {
                "phase": "resume",
                "message": "Resuming before LLM scoring (boards + rules already completed)…",
            }
        )
    else:
        if resume_bundle and resume_bundle.phase == PHASE_AFTER_SOURCES:
            source_results = resume_bundle.source_results
            failure_counts = dict(resume_bundle.failure_counts)
        else:
            source_results = []
            n_sources = len(sources)
            if n_sources:
                logger.info("Fetching from %d source(s) (parallel)…", n_sources)
                _ph(
                    {
                        "phase": "sources",
                        "message": f"Fetching jobs from {n_sources} source(s)…",
                        "sources": list(sources.keys()),
                    }
                )
            else:
                logger.info("No enabled sources; skipping fetches.")
                _ph(
                    {
                        "phase": "sources",
                        "message": "No sources enabled; skipping fetches.",
                    }
                )
            with ThreadPoolExecutor(
                max_workers=min(6, max(1, len(sources)))
            ) as pool:
                futs = {
                    pool.submit(_run_source, n, f, profile): n
                    for n, f in sources.items()
                }
                for fut in as_completed(futs):
                    sr = fut.result()
                    source_results.append(sr)
                    err = f" ({sr.error})" if sr.error else ""
                    logger.info(
                        "Source %r finished: %s, %d jobs, %dms%s",
                        sr.source,
                        sr.status,
                        len(sr.jobs),
                        sr.duration_ms,
                        err,
                    )
                    _ph(
                        {
                            "phase": "source_done",
                            "message": (
                                f"Finished {sr.source}: {len(sr.jobs)} job(s) "
                                f"({sr.status})"
                            ),
                            "source": sr.source,
                            "jobs_found": len(sr.jobs),
                            "status": sr.status,
                        }
                    )
            failure_counts = {}
            for sr in source_results:
                failure_counts[sr.source] = state.update_source_health(
                    sr.source, sr.status, sr.error
                )
            if not dry_run:
                save_checkpoint_after_sources(
                    root,
                    started=started,
                    source_results=source_results,
                    failure_counts=failure_counts,
                )

        raws = [j for sr in source_results for j in sr.jobs]
        resume_norm = (
            resume_bundle and resume_bundle.phase == PHASE_AFTER_SOURCES
        )
        logger.info("Normalizing %d raw job(s)…", len(raws))
        _ph(
            {
                "phase": "normalize",
                "message": (
                    f"Normalizing {len(raws)} raw posting(s)…"
                    + (" (resumed)" if resume_norm else "")
                ),
                "raw_jobs": len(raws),
            }
        )
        jobs = normalize(raws)
        logger.info("Deduping / 24h window…")
        jobs = dedupe_and_window(jobs, state, window_hours=24)
        after_window = len(jobs)
        _ph(
            {
                "phase": "dedupe",
                "message": f"Deduped to {after_window} job(s) (24h window)…",
                "jobs_after_window": after_window,
            }
        )

        logger.info("Applying profile rules…")
        jobs = filter_jobs(jobs, profile)
        after_rules = len(jobs)
        _ph(
            {
                "phase": "rules",
                "message": f"Rules: {after_rules} job(s) remain…",
                "jobs_after_rules": after_rules,
            }
        )
        if not dry_run:
            save_checkpoint_after_rules(
                root,
                started=started,
                after_window=after_window,
                after_rules=after_rules,
                jobs=jobs,
                source_results=source_results,
                failure_counts=failure_counts,
            )

    # Emit an interim HTML snapshot before LLM scoring so that if scoring
    # crashes or the connection drops, the server still has something to serve
    # for download (free users only get 1 run/day — losing progress is painful).
    if jobs:
        _interim_health = build_health(source_results, failure_counts)
        _interim_html = render_html(jobs, _interim_health, profile.name, discovery={})
        _ph({"phase": "interim_report", "partial_report_html": _interim_html})

    logger.info("LLM fit scoring (%d job(s))…", len(jobs))
    _ph(
        {
            "phase": "llm",
            "message": f"Scoring {after_rules} job(s) with LLM…",
            "jobs_to_score": after_rules,
        }
    )
    jobs = score_jobs(jobs, profile, llm_keys=llm_keys)

    discovered_path = root / "profiles" / f"{config['profile']}_discovered.yaml"
    discovery_result = {"new_greenhouse": [], "new_lever": []}
    if not dry_run and discover:
        logger.info("Company discovery → %s", discovered_path)
        discovery_result = discover_companies(jobs, discovered_path)
        logger.info(
            "Discovery done: +%d greenhouse, +%d lever",
            len(discovery_result["new_greenhouse"]),
            len(discovery_result["new_lever"]),
        )
    else:
        logger.info("Skipping company discovery (dry_run=%s, discover=%s)", dry_run, discover)

    jobs = [j for j in jobs if (j.fit_score or 0) >= profile.min_fit_score]
    after_llm = len(jobs)
    _ph(
        {
            "phase": "filter_score",
            "message": f"{after_llm} job(s) meet min_fit_score ({profile.min_fit_score})…",
            "jobs_matched": after_llm,
        }
    )

    health = build_health(source_results, failure_counts)
    logger.info("Rendering email HTML…")
    html = render_html(jobs, health, profile.name, discovery=discovery_result)

    subject = f"[Job Hunter] {len(jobs)} matches for {profile.name}"
    if health.action_needed:
        subject = "[ACTION NEEDED] " + subject

    logger.info("Sending email (dry_run=%s, send_smtp=%s)…", dry_run, send_smtp)
    report_html: str | None = None
    if send_smtp:
        _ph(
            {
                "phase": "email",
                "message": "Sending results email…",
                "dry_run": dry_run,
            }
        )
        send_email(html, subject, dry_run=dry_run, to_addr=email_to)
        email_sent = not dry_run
    else:
        report_html = html
        _ph(
            {
                "phase": "email",
                "message": "Skipping email send; report available as HTML download.",
            }
        )
        email_sent = False

    if not dry_run:
        save_checkpoint_ready_finalize(
            root,
            started=started,
            after_window=after_window,
            after_rules=after_rules,
            after_llm=after_llm,
            discovery_result={
                "new_greenhouse": list(discovery_result["new_greenhouse"]),
                "new_lever": list(discovery_result["new_lever"]),
            },
            jobs_final=jobs,
            source_results=source_results,
            failure_counts=failure_counts,
            report_html=html,
            subject=subject,
            email_step_done=True,
            send_smtp=send_smtp,
        )

    if not dry_run:
        logger.info("Marking jobs as seen in state…")
        state.mark_seen(
            [
                (_stable_id(j), j.source)
                for sr in source_results
                for j in sr.jobs
                if j.title and j.url
            ]
        )
    else:
        logger.info("Skipping mark_seen (dry run)")
    state.record_run(
        started,
        datetime.now(timezone.utc),
        {sr.source: sr.status for sr in source_results},
    )
    state.close()
    logger.info("Run recorded; state DB closed.")

    if not dry_run:
        clear_checkpoint(root)

    line = (
        f"window={after_window} rules={after_rules} llm={after_llm} "
        f"sent={len(jobs)} sources={len(source_results)} "
        f"discovered=+{len(discovery_result['new_greenhouse'])}gh "
        f"+{len(discovery_result['new_lever'])}lv"
    )
    print(line)
    _ph({"phase": "done", "message": "Finished."})
    out: dict[str, Any] = {
        "window": after_window,
        "rules": after_rules,
        "llm": after_llm,
        "sent": len(jobs),
        "sources": len(source_results),
        "new_greenhouse": len(discovery_result["new_greenhouse"]),
        "new_lever": len(discovery_result["new_lever"]),
        "email_sent": email_sent,
        "line": line,
    }
    if report_html is not None:
        out["report_html"] = report_html
        out["report_subject"] = subject

    _restore_env_vars(_env_overrides)

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log pipeline steps (sources, normalize, scoring, email, state).",
    )
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    run(
        Path(args.root).resolve(),
        dry_run=args.dry_run,
        verbose=args.verbose,
        discover=not args.dry_run,
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
