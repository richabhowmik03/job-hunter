from __future__ import annotations

from pathlib import Path

import yaml

from .models import Profile, Seniority


def load_profile(profile_dir: Path, profile_name: str) -> Profile:
    yaml_path = profile_dir / f"{profile_name}.yaml"
    resume_path = profile_dir / f"{profile_name}_resume.md"
    discovered_path = profile_dir / f"{profile_name}_discovered.yaml"

    data = yaml.safe_load(yaml_path.read_text())
    resume = resume_path.read_text() if resume_path.exists() else ""

    discovered_gh: list[str] = []
    discovered_lv: list[str] = []
    if discovered_path.exists():
        disc = yaml.safe_load(discovered_path.read_text()) or {}
        discovered_gh = disc.get("greenhouse", []) or []
        discovered_lv = disc.get("lever", []) or []

    sen = data.get("seniority", {}) or {}
    return Profile(
        name=data["name"],
        target_titles=data.get("target_titles", []),
        related_titles=data.get("related_titles", []),
        seniority=Seniority(
            min_years=sen.get("min_years", 0),
            max_years=sen.get("max_years", 99),
            reject_if_title_contains=sen.get("reject_if_title_contains", []),
        ),
        locations=data.get("locations", []),
        deal_breakers=data.get("deal_breakers", []),
        greenhouse_companies=sorted(
            set(data.get("greenhouse_companies", []) or []) | set(discovered_gh)
        ),
        lever_companies=sorted(
            set(data.get("lever_companies", []) or []) | set(discovered_lv)
        ),
        serpapi_queries=data.get("serpapi_queries", []),
        min_fit_score=data.get("min_fit_score", 70),
        resume_markdown=resume,
    )


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())
