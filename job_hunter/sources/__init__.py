from __future__ import annotations

from importlib import import_module
from typing import Callable

from ..core.models import Profile, RawJob

FetchFn = Callable[[Profile], list[RawJob]]

_SOURCE_MODULES = [
    "greenhouse",
    "lever",
    "rss",
    "serpapi",
    "linkedin",
    "naukri",
]


def load_sources(enabled: list[str]) -> dict[str, FetchFn]:
    sources: dict[str, FetchFn] = {}
    for name in _SOURCE_MODULES:
        if name not in enabled:
            continue
        mod = import_module(f".{name}", package=__name__)
        sources[name] = mod.fetch
    return sources
