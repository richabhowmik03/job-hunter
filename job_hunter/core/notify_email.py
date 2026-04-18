from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus

from .health import HealthSummary
from .models import Job

VERDICT_ORDER = {"strong": 0, "maybe": 1, "weak": 2}
VERDICT_COLOR = {"strong": "#1b7a33", "maybe": "#b58900", "weak": "#888"}


def _posted_ago(posted: datetime | None) -> str:
    if not posted:
        return "—"
    delta = datetime.now(timezone.utc) - posted
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "<1h ago"
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _linkedin_company_search(company: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(company)}"


def _row(job: Job) -> str:
    color = VERDICT_COLOR.get(job.fit_verdict or "weak", "#888")
    referral = ""
    if job.company:
        referral = (
            f" &middot; <a href='{_linkedin_company_search(job.company)}' "
            f"style='color:#0a66c2'>find referrals</a>"
        )
    return (
        "<tr>"
        f"<td><b>{job.title}</b><br>"
        f"<span style='color:#555'>{job.company} — {job.location or '—'}</span></td>"
        f"<td style='color:{color};font-weight:bold'>{(job.fit_verdict or '').upper()}<br>"
        f"<span style='font-weight:normal;color:#333'>{job.fit_score or 0}</span></td>"
        f"<td style='color:#333'>{job.fit_reason or ''}</td>"
        f"<td style='white-space:nowrap'>{_posted_ago(job.posted_at)}<br>"
        f"<span style='color:#888'>{job.source}</span></td>"
        f"<td style='white-space:nowrap'>"
        f"<a href='{job.url}' style='color:#0a66c2'>Apply</a>{referral}</td>"
        "</tr>"
    )


def _discovery_html(discovery: dict | None) -> str:
    if not discovery:
        return ""
    new_gh = discovery.get("new_greenhouse", [])
    new_lv = discovery.get("new_lever", [])
    if not new_gh and not new_lv:
        return ""
    parts = []
    if new_gh:
        parts.append("Greenhouse: " + ", ".join(new_gh))
    if new_lv:
        parts.append("Lever: " + ", ".join(new_lv))
    return (
        "<h3 style='margin-top:24px;font-family:Arial,sans-serif'>"
        "New companies discovered this run</h3>"
        f"<p style='font-family:Arial,sans-serif;color:#333'>"
        f"{' &middot; '.join(parts)}</p>"
    )


def _jobs_table(jobs: list[Job]) -> str:
    return (
        "<table border='1' cellpadding='8' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;width:100%'>"
        "<tr style='background:#f5f5f5'>"
        "<th align='left'>Role</th><th>Fit</th><th align='left'>Why</th>"
        "<th>Posted</th><th>Links</th></tr>"
        + "".join(_row(j) for j in jobs)
        + "</table>"
    )


def render_html(
    jobs: list[Job],
    health: HealthSummary,
    profile_name: str,
    discovery: dict | None = None,
    below_threshold: list[Job] | None = None,
) -> str:
    jobs_sorted = sorted(
        jobs,
        key=lambda j: (VERDICT_ORDER.get(j.fit_verdict or "weak", 3), -(j.fit_score or 0)),
    )

    if jobs_sorted:
        body = _jobs_table(jobs_sorted)
    else:
        body = (
            "<p style='font-family:Arial,sans-serif;color:#555'>"
            "No new matches in the last 24 hours. Pipeline ran successfully.</p>"
        )

    # Below-threshold section — collapsed <details> so it doesn't clutter the email
    below_html = ""
    if below_threshold:
        below_sorted = sorted(
            below_threshold,
            key=lambda j: (VERDICT_ORDER.get(j.fit_verdict or "weak", 3), -(j.fit_score or 0)),
        )
        below_html = (
            "<details style='margin-top:24px;font-family:Arial,sans-serif'>"
            f"<summary style='cursor:pointer;color:#555;font-size:13px'>"
            f"&#9660; {len(below_sorted)} more jobs scored below threshold (click to expand)"
            f"</summary>"
            "<p style='color:#888;font-size:12px;margin:8px 0'>These passed source/title/location "
            "filters but scored below your <code>min_fit_score</code>. Review occasionally to "
            "tune your threshold.</p>"
            + _jobs_table(below_sorted)
            + "</details>"
        )

    return (
        f"<html><body>"
        f"<h2 style='font-family:Arial,sans-serif'>Job Hunter — {profile_name}</h2>"
        f"<p style='color:#555;font-family:Arial,sans-serif'>"
        f"{len(jobs_sorted)} matches &middot; "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>"
        f"{body}"
        f"{below_html}"
        f"{_discovery_html(discovery)}"
        f"{health.to_html()}"
        f"</body></html>"
    )


def send_email(
    html: str, subject: str, *, dry_run: bool = False, to_addr: str | None = None
) -> None:
    if dry_run:
        print("=== DRY RUN EMAIL ===")
        print(f"Subject: {subject}")
        print(html)
        return

    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_APP_PASSWORD"]
    to_addr = to_addr if to_addr is not None else os.environ["SMTP_TO"]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [to_addr], msg.as_string())
