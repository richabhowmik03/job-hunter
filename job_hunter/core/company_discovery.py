from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import requests
import yaml

from .models import Job

DISCOVERY_SOURCES = {"serpapi", "linkedin", "naukri", "rss"}
_STOP_SUFFIXES = (
    "inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation",
    "technologies", "technology", "tech", "labs", "lab", "solutions", "systems",
    "ai", "io", "co",
)


def _slug_candidates(company: str) -> list[str]:
    base = re.sub(r"[^a-z0-9\s]", "", company.lower()).strip()
    if not base:
        return []
    tokens = base.split()
    seen: list[str] = []

    def add(s: str) -> None:
        s = s.strip("-")
        if s and s not in seen:
            seen.append(s)

    add("".join(tokens))
    add("-".join(tokens))
    trimmed = [t for t in tokens if t not in _STOP_SUFFIXES]
    if trimmed and trimmed != tokens:
        add("".join(trimmed))
        add("-".join(trimmed))
    return seen[:4]


def _probe_greenhouse(slug: str) -> bool:
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            timeout=10,
        )
        return r.status_code == 200 and "jobs" in r.text[:200]
    except Exception:
        return False


def _probe_lever(slug: str) -> bool:
    try:
        r = requests.get(
            f"https://api.lever.co/v0/postings/{slug}",
            params={"mode": "json"},
            timeout=10,
        )
        return r.status_code == 200 and r.text.strip().startswith("[")
    except Exception:
        return False


def _load(path: Path) -> dict:
    if not path.exists():
        return {"greenhouse": [], "lever": [], "tried_failed": []}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("greenhouse", [])
    data.setdefault("lever", [])
    data.setdefault("tried_failed", [])
    return data


def _save(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def discover_companies(jobs: Iterable[Job], discovered_path: Path) -> dict:
    data = _load(discovered_path)
    known_gh = set(data["greenhouse"])
    known_lv = set(data["lever"])
    tried = set(data["tried_failed"])

    new_gh: list[str] = []
    new_lv: list[str] = []

    for job in jobs:
        if job.source not in DISCOVERY_SOURCES:
            continue
        if job.fit_verdict != "strong":
            continue
        if not job.company:
            continue
        for cand in _slug_candidates(job.company):
            if cand in known_gh or cand in known_lv or cand in tried:
                continue
            if _probe_greenhouse(cand):
                known_gh.add(cand)
                new_gh.append(cand)
                break
            if _probe_lever(cand):
                known_lv.add(cand)
                new_lv.append(cand)
                break
            tried.add(cand)

    data["greenhouse"] = sorted(known_gh)
    data["lever"] = sorted(known_lv)
    data["tried_failed"] = sorted(tried)
    _save(discovered_path, data)
    return {"new_greenhouse": new_gh, "new_lever": new_lv}


def load_discovered(discovered_path: Path) -> tuple[list[str], list[str]]:
    data = _load(discovered_path)
    return data["greenhouse"], data["lever"]
