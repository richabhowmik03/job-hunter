from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_jobs ("
            "id TEXT PRIMARY KEY, "
            "source TEXT NOT NULL, "
            "first_seen_at TEXT NOT NULL, "
            "last_seen_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "started_at TEXT NOT NULL, "
            "finished_at TEXT, "
            "source_status_json TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS source_health ("
            "source TEXT PRIMARY KEY, "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0, "
            "last_status TEXT, "
            "last_error TEXT, "
            "updated_at TEXT)"
        )
        self.conn.commit()

    def seen_ids(self) -> set[str]:
        cur = self.conn.execute("SELECT id FROM seen_jobs")
        return {row[0] for row in cur.fetchall()}

    def mark_seen(self, ids: list[tuple[str, str]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for job_id, source in ids:
            self.conn.execute(
                "INSERT INTO seen_jobs(id, source, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (job_id, source, now, now),
            )
        self.conn.commit()

    def record_run(
        self, started: datetime, finished: datetime, source_status: dict
    ) -> None:
        self.conn.execute(
            "INSERT INTO runs(started_at, finished_at, source_status_json) VALUES (?, ?, ?)",
            (started.isoformat(), finished.isoformat(), json.dumps(source_status)),
        )
        self.conn.commit()

    def update_source_health(self, source: str, status: str, error: str | None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE source=?",
            (source,),
        )
        row = cur.fetchone()
        prev = row[0] if row else 0
        new_failures = prev + 1 if status == "error" else 0
        self.conn.execute(
            "INSERT INTO source_health(source, consecutive_failures, last_status, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "consecutive_failures=excluded.consecutive_failures, "
            "last_status=excluded.last_status, "
            "last_error=excluded.last_error, "
            "updated_at=excluded.updated_at",
            (source, new_failures, status, error, now),
        )
        self.conn.commit()
        return new_failures

    def close(self) -> None:
        self.conn.close()
