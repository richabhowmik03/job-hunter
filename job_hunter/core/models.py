from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


@dataclass
class RawJob:
    source: str
    source_job_id: str
    url: str
    title: str
    company: str
    location: str = ""
    remote: bool = False
    description: str = ""
    posted_at: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    id: str
    source: str
    url: str
    title: str
    company: str
    location: str
    remote: bool
    description: str
    posted_at: Optional[datetime]
    seniority_hint: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    fit_score: Optional[int] = None
    fit_verdict: Optional[Literal["strong", "maybe", "weak"]] = None
    fit_reason: Optional[str] = None


@dataclass
class Seniority:
    min_years: int = 0
    max_years: int = 99
    reject_if_title_contains: list[str] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    target_titles: list[str]
    related_titles: list[str]
    seniority: Seniority
    locations: list[str]
    deal_breakers: list[str]
    greenhouse_companies: list[str]
    lever_companies: list[str]
    serpapi_queries: list[str]
    min_fit_score: int
    resume_markdown: str


@dataclass
class SourceResult:
    source: str
    status: Literal["ok", "error", "empty"]
    jobs: list[RawJob] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class RunResult:
    started_at: datetime
    finished_at: Optional[datetime] = None
    source_results: list[SourceResult] = field(default_factory=list)
    jobs_after_window: int = 0
    jobs_after_rules: int = 0
    jobs_after_llm: int = 0
    final_jobs: list[Job] = field(default_factory=list)
