from __future__ import annotations

import json
import logging
import re

from .llm_providers import ScorerChain
from .models import Job, Profile

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a job-fit evaluator. Given a candidate's resume and a job posting, "
    "you return ONLY a compact JSON object with keys: "
    '"score" (integer 0-100), '
    '"verdict" (one of "strong", "maybe", "weak"), '
    '"reason" (one short sentence, <= 25 words). '
    "Score based on: title/role alignment, experience level match (reject if posting clearly requires seniority far above or below candidate), "
    "required skills overlap, and any deal-breakers stated in the posting. "
    "Verdict mapping: score >= 80 strong, 60-79 maybe, <60 weak. "
    "Output raw JSON only — no prose, no markdown fences."
)


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def score_jobs(
    jobs: list[Job],
    profile: Profile,
    *,
    llm_keys: dict[str, str] | None = None,
) -> list[Job]:
    if not jobs:
        return jobs

    if llm_keys:
        chain = ScorerChain.from_keys(llm_keys)
    else:
        chain = ScorerChain.from_env()
    n = len(jobs)
    for i, job in enumerate(jobs, 1):
        title = (job.title or "")[:72]
        logger.info(
            "  Score %d/%d: %s — %s",
            i,
            n,
            job.company or "?",
            title + ("…" if job.title and len(job.title) > 72 else ""),
        )
        job_text = (
            f"JOB POSTING\n"
            f"Title: {job.title}\n"
            f"Company: {job.company}\n"
            f"Location: {job.location} (remote={job.remote})\n"
            f"Source: {job.source}\n"
            f"Description:\n{job.description[:3000]}"
        )
        user_content = (
            f"CANDIDATE RESUME:\n\n{profile.resume_markdown}\n\n---\n\n{job_text}"
        )

        try:
            provider_name, text = chain.score_one(SYSTEM_PROMPT, user_content)
        except RuntimeError as exc:
            job.fit_score = 0
            job.fit_verdict = "weak"
            job.fit_reason = f"all providers exhausted: {exc}"
            logger.warning("  → %s", job.fit_reason)
            continue
        except Exception as exc:
            job.fit_score = 0
            job.fit_verdict = "weak"
            job.fit_reason = f"scoring failed: {exc}"
            logger.warning("  → scoring failed: %s", exc)
            continue

        parsed = _parse_json(text) or {}
        try:
            score = int(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        verdict = parsed.get("verdict") or (
            "strong" if score >= 80 else "maybe" if score >= 60 else "weak"
        )
        job.fit_score = max(0, min(100, score))
        job.fit_verdict = verdict if verdict in ("strong", "maybe", "weak") else "weak"
        job.fit_reason = str(parsed.get("reason", ""))[:300]
        logger.info(
            "  → score=%s (%s) via %s",
            job.fit_score,
            job.fit_verdict,
            provider_name,
        )

    return jobs
