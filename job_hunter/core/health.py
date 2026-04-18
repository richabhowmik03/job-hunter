from __future__ import annotations

from dataclasses import dataclass

from .models import SourceResult


@dataclass
class HealthSummary:
    per_source: list[tuple[str, str, str, int]]
    action_needed: bool

    def to_html(self) -> str:
        rows = "".join(
            f"<tr><td>{s}</td><td>{status}</td>"
            f"<td>{count}</td><td style='color:#888'>{note}</td></tr>"
            for s, status, note, count in self.per_source
        )
        return (
            "<h3 style='margin-top:24px'>Source health</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse;font-family:monospace;font-size:12px'>"
            "<tr><th>source</th><th>status</th><th>jobs</th><th>note</th></tr>"
            f"{rows}</table>"
        )


def build_health(
    source_results: list[SourceResult], failure_counts: dict[str, int]
) -> HealthSummary:
    rows: list[tuple[str, str, str, int]] = []
    action = False
    for sr in source_results:
        note = sr.error or ""
        fails = failure_counts.get(sr.source, 0)
        if fails >= 3:
            action = True
            note = f"{note} [FAILED {fails}x in a row]".strip()
        rows.append((sr.source, sr.status, note, len(sr.jobs)))
    return HealthSummary(per_source=rows, action_needed=action)
