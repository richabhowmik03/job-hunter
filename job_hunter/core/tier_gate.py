"""Per-IP and global daily run counters for the free tier.

SQLite-backed so counters survive process restarts but stay tiny (one row per
IP per UTC day). BYOK requests bypass the gate entirely — callers check for
supplied keys *before* invoking :func:`check_and_count`.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PER_IP_PER_DAY = 1
DEFAULT_GLOBAL_PER_DAY = 5

_lock = threading.Lock()


def _db_path() -> Path:
    override = os.environ.get("JOB_HUNTER_TIER_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".job_hunter" / "tier_gate.db"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS counters ("
        "  day TEXT NOT NULL,"
        "  scope TEXT NOT NULL,"
        "  count INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (day, scope)"
        ")"
    )
    return conn


def _limits() -> tuple[int, int]:
    per_ip = int(os.environ.get("FREE_TIER_PER_IP_PER_DAY", DEFAULT_PER_IP_PER_DAY))
    global_cap = int(
        os.environ.get("FREE_TIER_GLOBAL_PER_DAY", DEFAULT_GLOBAL_PER_DAY)
    )
    return max(0, per_ip), max(0, global_cap)


class GateError(Exception):
    """Raised when a free-tier request is denied."""

    def __init__(self, message: str, *, scope: str) -> None:
        super().__init__(message)
        self.scope = scope  # "ip" or "global"


def check_and_count(client_ip: str) -> dict[str, int]:
    """Atomically check quotas and increment counters.

    Raises :class:`GateError` if either the per-IP or global cap is exceeded.
    Returns the post-increment counts so callers can surface them.
    """
    per_ip_cap, global_cap = _limits()
    day = _today()
    ip_scope = f"ip:{client_ip}"
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT scope, count FROM counters WHERE day = ? AND scope IN (?, ?)",
            (day, ip_scope, "global"),
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
        ip_count = counts.get(ip_scope, 0)
        global_count = counts.get("global", 0)

        if ip_count >= per_ip_cap:
            raise GateError(
                f"Free tier: {per_ip_cap} run/day per IP. Add a Groq API key "
                f"(free at console.groq.com) to remove this limit.",
                scope="ip",
            )
        if global_count >= global_cap:
            raise GateError(
                f"Free tier is busy today ({global_cap} global runs used). "
                "Try again tomorrow, or add your own API keys to keep going.",
                scope="global",
            )

        conn.execute(
            "INSERT INTO counters (day, scope, count) VALUES (?, ?, 1) "
            "ON CONFLICT(day, scope) DO UPDATE SET count = count + 1",
            (day, ip_scope),
        )
        conn.execute(
            "INSERT INTO counters (day, scope, count) VALUES (?, 'global', 1) "
            "ON CONFLICT(day, scope) DO UPDATE SET count = count + 1",
            (day,),
        )
        conn.commit()
        return {
            "ip_count": ip_count + 1,
            "ip_cap": per_ip_cap,
            "global_count": global_count + 1,
            "global_cap": global_cap,
        }
