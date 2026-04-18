from __future__ import annotations

import re

from .models import Job, Profile

# Broad ML/AI domain keywords — if ANY of these appear in the title the job is
# worth sending to the LLM scorer. This intentionally casts a wide net; the LLM
# does the precise fit judgment.
_DOMAIN_KEYWORDS = [
    "machine learning", "ml ", " ml", "data scien", "ai ", " ai",
    "artificial intelligence", "deep learning", "neural", "nlp",
    "natural language", "computer vision", "llm", "large language",
    "generative", "gen ai", "genai", "reinforcement", "recommendation",
    "analytics engineer", "data engineer", "research scientist",
    "applied scientist", "mlops", "ml platform", "ml infra",
    "prompt engineer", "ai engineer", "ai researcher",
]

# Domains that are clearly irrelevant regardless of any ML keywords present
_DOMAIN_BLOCKLIST = [
    "sales", "marketing", "recruiter", "talent acquisition",
    "account manager", "customer success", "business development",
    "finance", "accounting", "hr ", " hr", "human resources",
    "legal", "paralegal", "administrative", "receptionist",
]


def _title_match(title: str, allowed: list[str]) -> bool:
    t = title.lower()
    # First check the explicit profile allowlist (exact phrase / all tokens)
    for phrase in allowed:
        p = phrase.lower()
        if re.search(rf"\b{re.escape(p)}\b", t):
            return True
        if all(tok in t for tok in p.split()):
            return True
    # Fallback: broad domain keyword match — catches "Data Science Engineer",
    # "Deep Learning Researcher", "CV Engineer", etc.
    if any(kw in t for kw in _DOMAIN_KEYWORDS):
        # But exclude obviously irrelevant domain titles that happen to contain
        # a keyword (e.g. "AI Sales Manager")
        if not any(bl in t for bl in _DOMAIN_BLOCKLIST):
            return True
    return False


def _location_match(job: Job, locations: list[str]) -> bool:
    if not locations:
        return True
    # Any remote job is reachable regardless of which country's remote it says.
    if job.remote:
        return True
    loc = job.location.lower()
    return any(l.lower() in loc for l in locations if l.lower() != "remote")


def filter_jobs(jobs: list[Job], profile: Profile) -> list[Job]:
    allowed_titles = profile.target_titles + profile.related_titles
    reject_terms = [r.lower() for r in profile.seniority.reject_if_title_contains]
    deal_breakers = [d.lower() for d in profile.deal_breakers]

    out: list[Job] = []
    for job in jobs:
        title_lc = job.title.lower()
        if any(term in title_lc for term in reject_terms):
            continue
        if not _title_match(job.title, allowed_titles):
            continue
        if not _location_match(job, profile.locations):
            continue
        desc_lc = job.description.lower()
        if any(db in desc_lc for db in deal_breakers):
            continue
        out.append(job)
    return out
