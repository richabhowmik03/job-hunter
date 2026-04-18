"""Pluggable LLM providers for the fit-scorer with fallback chain.

Providers accept explicit ``api_key`` / ``model`` so they can be built from
either process env (CLI / owner path) or per-request BYOK headers (UI path).
The ``ScorerChain`` iterates providers in order; a provider is **disabled for
the rest of the run** on rate-limit / quota errors, and the chain falls
through to the next.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimitedError(RuntimeError):
    """Raised when a provider is rate-limited or out of quota.

    The chain uses this to skip the provider for the rest of the run.
    """


class Provider:
    name: str = "base"

    def score_one(self, system: str, user: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


# --- Groq ---------------------------------------------------------------
class GroqProvider(Provider):
    name = "groq"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._client = None
        self._api_key = api_key
        self.model = model or "llama-3.3-70b-versatile"

    def _get_client(self):
        if self._client is None:
            from groq import Groq

            self._client = Groq(api_key=self._api_key)
        return self._client

    def score_one(self, system: str, user: str) -> str:
        try:
            from groq import RateLimitError as GroqRateLimit
        except Exception:
            GroqRateLimit = None  # type: ignore[assignment]
        try:
            resp = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=200,
                temperature=0.2,
            )
        except Exception as exc:
            if GroqRateLimit and isinstance(exc, GroqRateLimit):
                raise RateLimitedError(str(exc)) from exc
            msg = str(exc).lower()
            if "rate" in msg or "quota" in msg or "429" in msg:
                raise RateLimitedError(str(exc)) from exc
            raise
        msg = resp.choices[0].message
        return (msg.content or "").strip() if msg else ""


# --- Gemini -------------------------------------------------------------
class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._client = None
        self._api_key = api_key
        self.model = model or "gemini-2.0-flash"

    def _get_client(self):
        if self._client is None:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=self._api_key)
            self._client = genai.GenerativeModel(self.model)
        return self._client

    def score_one(self, system: str, user: str) -> str:
        try:
            resp = self._get_client().generate_content(
                [{"role": "user", "parts": [system + "\n\n" + user]}],
                generation_config={"temperature": 0.2, "max_output_tokens": 200},
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate" in msg or "resource exhausted" in msg:
                raise RateLimitedError(str(exc)) from exc
            raise
        text = getattr(resp, "text", None)
        if text:
            return text.strip()
        try:
            return resp.candidates[0].content.parts[0].text.strip()
        except Exception:
            return ""


# --- OpenRouter (OpenAI-compatible) ------------------------------------
class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._client = None
        self._api_key = api_key
        self.model = model or "deepseek/deepseek-chat-v3.1:free"

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key,
                base_url="https://openrouter.ai/api/v1",
            )
        return self._client

    def score_one(self, system: str, user: str) -> str:
        try:
            resp = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=200,
                temperature=0.2,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate" in msg:
                raise RateLimitedError(str(exc)) from exc
            raise
        return (resp.choices[0].message.content or "").strip()


# Order matters: first available wins, subsequent ones act as fallbacks.
_CHAIN_ORDER = ("groq", "gemini", "openrouter")


def _build(name: str, api_key: str, model: str | None) -> Provider:
    if name == "groq":
        return GroqProvider(api_key, model)
    if name == "gemini":
        return GeminiProvider(api_key, model)
    if name == "openrouter":
        return OpenRouterProvider(api_key, model)
    raise ValueError(f"Unknown LLM provider: {name!r}")


@dataclass
class ScorerChain:
    providers: list[Provider]
    active_index: int = 0

    @classmethod
    def from_env(cls) -> "ScorerChain":
        """Build the chain from process env vars (CLI / owner path)."""
        keys = {
            "groq": os.environ.get("GROQ_API_KEY", "").strip(),
            "gemini": os.environ.get("GEMINI_API_KEY", "").strip(),
            "openrouter": os.environ.get("OPENROUTER_API_KEY", "").strip(),
        }
        models = {
            "groq": os.environ.get("GROQ_MODEL") or None,
            "gemini": os.environ.get("GEMINI_MODEL") or None,
            "openrouter": os.environ.get("OPENROUTER_MODEL") or None,
        }
        return cls.from_keys(keys, models)

    @classmethod
    def from_keys(
        cls,
        keys: dict[str, str],
        models: dict[str, str | None] | None = None,
    ) -> "ScorerChain":
        """Build the chain from an explicit ``{provider_name: api_key}`` map.

        Missing / empty keys are silently skipped. Raises if no provider is
        configured so the caller can surface a clear error to the user.
        """
        models = models or {}
        active: list[Provider] = []
        for name in _CHAIN_ORDER:
            k = (keys.get(name) or "").strip()
            if not k:
                continue
            active.append(_build(name, k, models.get(name)))
        if not active:
            raise RuntimeError(
                "No LLM provider configured. Set at least one API key "
                "(Groq / Gemini / OpenRouter)."
            )
        logger.info("LLM chain: %s", " → ".join(p.name for p in active))
        return cls(providers=active)

    @property
    def current(self) -> Optional[Provider]:
        if self.active_index >= len(self.providers):
            return None
        return self.providers[self.active_index]

    def score_one(self, system: str, user: str) -> tuple[str, str]:
        """Returns (provider_name, raw_text). Raises RuntimeError when exhausted."""
        while self.current is not None:
            p = self.current
            try:
                return p.name, p.score_one(system, user)
            except RateLimitedError as exc:
                logger.warning(
                    "Provider %r rate-limited; falling back. (%s)", p.name, exc
                )
                self.active_index += 1
            except Exception as exc:
                logger.warning(
                    "Provider %r failed with %s: %s; falling back.",
                    p.name,
                    type(exc).__name__,
                    exc,
                )
                self.active_index += 1
        raise RuntimeError("All LLM providers exhausted for this run.")
