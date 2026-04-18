"""Build full profile YAML from user-editable fields + repo template."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def build_serpapi_queries(target_titles: list[str], *, max_queries: int = 48) -> list[str]:
    """Derive SerpAPI search strings from target role titles (India + remote)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in target_titles:
        t = str(t).strip()
        if not t:
            continue
        for q in (f"{t} India", f"{t} remote India"):
            if q not in seen:
                seen.add(q)
                out.append(q)
            if len(out) >= max_queries:
                return out
    return out


def merge_profile_with_template(
    user: dict[str, Any],
    template_path: Path,
) -> str:
    """
    Overlay ``name``, ``target_titles``, ``related_titles``, ``seniority``, ``locations``
    from ``user`` onto the template. Copy ``deal_breakers``, ``greenhouse_companies``,
    ``lever_companies``, ``min_fit_score`` from the template. Replace
    ``serpapi_queries`` using ``target_titles``.
    """
    raw = template_path.read_text(encoding="utf-8")
    base = yaml.safe_load(raw)
    if not isinstance(base, dict):
        raise ValueError("Profile template must be a YAML mapping")

    for key in ("name", "target_titles", "related_titles", "seniority", "locations"):
        if key in user and user[key] is not None:
            base[key] = user[key]

    tt = base.get("target_titles")
    if not isinstance(tt, list) or not tt or not any(str(x).strip() for x in tt):
        raise ValueError("target_titles must be a non-empty list")

    base["serpapi_queries"] = build_serpapi_queries([str(x) for x in tt])

    return yaml.dump(
        base,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
