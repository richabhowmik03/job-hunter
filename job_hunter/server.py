"""FastAPI server for UI-triggered job hunter runs."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .core.pipeline_checkpoint import has_checkpoint
from .core.profile_merge import merge_profile_with_template
from .core.resume_text import decode_resume_bytes
from .core.tier_gate import GateError, check_and_count
from .main import run

FREE_TIER_SOURCES = ["greenhouse", "lever", "rss"]
BYOK_HEADERS = {
    "groq": "x-llm-groq-key",
    "gemini": "x-llm-gemini-key",
    "openrouter": "x-llm-openrouter-key",
}
BYOK_SOURCE_HEADERS = {"serpapi": "x-serpapi-key"}

# UI runs are serialized because main.run() temporarily mutates os.environ for
# BYOK source keys. Concurrent runs with different keys would race.
_run_serializer = threading.Lock()


def _read_byok_headers(request: Request) -> tuple[dict[str, str], dict[str, str]]:
    llm_keys: dict[str, str] = {}
    for name, header in BYOK_HEADERS.items():
        v = (request.headers.get(header) or "").strip()
        if v:
            llm_keys[name] = v
    source_keys: dict[str, str] = {}
    for name, header in BYOK_SOURCE_HEADERS.items():
        v = (request.headers.get(header) or "").strip()
        if v:
            source_keys[name] = v
    return llm_keys, source_keys

MAX_UPLOAD_BYTES = 256 * 1024
MAX_PROFILE_JSON_BYTES = 32 * 1024
SLUG_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _normalize_slug(raw: str) -> str:
    """Accept ``richa2``, ``profiles/richa2``, ``Richa2.yaml``; return basename."""
    s = raw.strip().lower().replace("\\", "/")
    s = s.strip("/")
    for prefix in ("./profiles/", "profiles/"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if s.endswith((".yaml", ".yml")):
        s = s.rsplit(".", 1)[0]
    return s.strip("/")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RATE_WINDOW_SEC = 60.0

_last_post: dict[str, float] = {}
_runs: dict[str, "RunState"] = {}
_lock = threading.Lock()


@dataclass
class RunState:
    status: Literal["running", "done", "error"] = "running"
    summary: dict[str, Any] | None = None
    email_sent: bool = False
    error: str | None = None
    progress: list[dict[str, Any]] = field(default_factory=list)
    report_html: str | None = None
    report_subject: str | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _smtp_credentials_ok() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_APP_PASSWORD"))


def _should_send_smtp_ui(requester_email: str) -> bool:
    """
    Only the address in ``JOB_HUNTER_UI_OWNER_EMAIL`` receives email from the UI.
    Everyone else gets an HTML download (no SMTP to arbitrary addresses).
    """
    owner = os.environ.get("JOB_HUNTER_UI_OWNER_EMAIL", "").strip().lower()
    if not owner or not _smtp_credentials_ok():
        return False
    return requester_email.strip().lower() == owner


def _summary_without_report(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k not in ("report_html", "report_subject")}


def _default_enabled_sources() -> list[str]:
    cfg_path = _repo_root() / "config.yaml"
    if not cfg_path.is_file():
        return ["greenhouse", "lever", "rss"]
    data = yaml.safe_load(cfg_path.read_text()) or {}
    return list(data.get("enabled_sources") or ["greenhouse", "lever", "rss"])


def _normalize_profile_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only editable keys; coerce types (matches richa.yaml top section)."""
    name = str(raw.get("name", "")).strip()
    target_titles = [
        str(x).strip() for x in (raw.get("target_titles") or []) if str(x).strip()
    ]
    related_titles = [
        str(x).strip() for x in (raw.get("related_titles") or []) if str(x).strip()
    ]
    locations = [
        str(x).strip() for x in (raw.get("locations") or []) if str(x).strip()
    ]
    sen = raw.get("seniority") if isinstance(raw.get("seniority"), dict) else {}
    seniority = {
        "min_years": int(sen.get("min_years", 0)),
        "max_years": int(sen.get("max_years", 99)),
        "reject_if_title_contains": [
            str(x).strip()
            for x in (sen.get("reject_if_title_contains") or [])
            if str(x).strip()
        ],
    }
    return {
        "name": name,
        "target_titles": target_titles,
        "related_titles": related_titles,
        "seniority": seniority,
        "locations": locations,
    }


def _validate_profile(user: dict[str, Any]) -> None:
    if not user["name"] or len(user["name"]) > 200:
        raise HTTPException(
            status_code=400, detail="name is required (max 200 characters)."
        )
    if not user["target_titles"]:
        raise HTTPException(
            status_code=400, detail="Add at least one target job title."
        )
    for key in ("target_titles", "related_titles", "locations"):
        if len(user[key]) > 80:
            raise HTTPException(
                status_code=400,
                detail=f"Too many entries in {key} (max 80).",
            )
    if len(user["seniority"]["reject_if_title_contains"]) > 80:
        raise HTTPException(
            status_code=400,
            detail="Too many seniority reject patterns (max 80).",
        )


def _write_workspace(
    root: Path,
    slug: str,
    resume_bytes: bytes,
    yaml_bytes: bytes,
    enabled_sources: list[str] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    prof = root / "profiles"
    prof.mkdir(exist_ok=True)
    (prof / f"{slug}.yaml").write_bytes(yaml_bytes)
    (prof / f"{slug}_resume.md").write_bytes(resume_bytes)
    cfg = {
        "profile": slug,
        "enabled_sources": enabled_sources or _default_enabled_sources(),
    }
    (root / "config.yaml").write_text(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _rate_limited(client_host: str) -> bool:
    now = time.monotonic()
    with _lock:
        last = _last_post.get(client_host, 0.0)
        if now - last < RATE_WINDOW_SEC:
            return True
        _last_post[client_host] = now
    return False


def _spawn_run(run_id: str, root: Path, email: str) -> None:
    send_smtp = _should_send_smtp_ui(email)

    def hook(ev: dict[str, Any]) -> None:
        payload = {**ev, "ts": time.time()}
        with _lock:
            st = _runs.get(run_id)
            if st:
                st.progress.append(payload)
                if len(st.progress) > 200:
                    st.progress = st.progress[-120:]

    def worker() -> None:
        try:
            summary = run(
                root,
                dry_run=False,
                verbose=False,
                discover=False,
                email_to=email if send_smtp else None,
                send_smtp=send_smtp,
                progress_hook=hook,
            )
            with _lock:
                st = _runs.get(run_id)
                if st:
                    st.status = "done"
                    st.summary = _summary_without_report(summary)
                    st.report_html = summary.get("report_html")
                    st.report_subject = summary.get("report_subject")
                    st.email_sent = bool(summary.get("email_sent"))
        except Exception as exc:  # noqa: BLE001 — surface to client
            with _lock:
                st = _runs.get(run_id)
                if st:
                    st.status = "error"
                    st.error = f"{type(exc).__name__}: {exc}"

    threading.Thread(target=worker, daemon=True).start()


def create_app() -> FastAPI:
    app = FastAPI(title="Job Hunter UI")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/api/runs")
    async def post_run(
        request: Request,
        email: str = Form(...),
        slug: str = Form(...),
        resume: UploadFile = File(...),
        profile_json: str = Form(...),
    ) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        if client not in ("127.0.0.1", "::1", "localhost") and _rate_limited(
            client
        ):
            raise HTTPException(
                status_code=429,
                detail="Rate limited: at most one run per minute per IP.",
            )

        slug = _normalize_slug(slug)
        email = email.strip()
        if not SLUG_RE.match(slug):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid slug: use 1–32 characters [a-z0-9_-] only "
                    "(e.g. richa2). Paths like profiles/richa2 are accepted and "
                    "normalized."
                ),
            )
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid email address.")

        pj = profile_json.strip()
        if len(pj.encode("utf-8")) > MAX_PROFILE_JSON_BYTES:
            raise HTTPException(
                status_code=400,
                detail="Profile JSON is too large.",
            )
        try:
            raw = json.loads(pj)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid profile JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Profile must be a JSON object.")

        user = _normalize_profile_payload(raw)
        _validate_profile(user)

        template_path = _repo_root() / "profiles" / "richa.yaml"
        if not template_path.is_file():
            raise HTTPException(
                status_code=500,
                detail="Server is missing profiles/richa.yaml (template).",
            )
        try:
            yaml_text = merge_profile_with_template(user, template_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        yb = yaml_text.encode("utf-8")
        rb = await resume.read()
        if len(rb) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Resume must be at most {MAX_UPLOAD_BYTES} bytes.",
            )
        try:
            resume_out = decode_resume_bytes(rb, resume.filename).encode("utf-8")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        run_id = str(uuid.uuid4())
        root = Path.home() / ".job_hunter" / "ui-runs" / run_id

        try:
            _write_workspace(root, slug, resume_out, yb)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        with _lock:
            _runs[run_id] = RunState()

        _spawn_run(run_id, root, email)

        return JSONResponse({"run_id": run_id})

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        with _lock:
            st = _runs.get(run_id)
        if not st:
            raise HTTPException(status_code=404, detail="Unknown run_id.")

        body: dict[str, Any] = {
            "status": st.status,
            "summary": st.summary,
            "email_sent": st.email_sent,
            "progress": st.progress,
            "download_available": bool(st.report_html),
        }
        if st.error:
            body["error"] = st.error
        return JSONResponse(body)

    @app.get("/api/runs/{run_id}/report.html")
    async def get_report_html(run_id: str) -> HTMLResponse:
        with _lock:
            st = _runs.get(run_id)
            html = st.report_html if st else None
        if not html:
            raise HTTPException(
                status_code=404,
                detail="Report not found or expired (only kept for recent UI runs).",
            )
        return HTMLResponse(
            content=html,
            headers={
                "Content-Disposition": 'attachment; filename="job-hunter-report.html"',
            },
        )

    @app.post("/api/run/stream")
    async def post_run_stream(
        request: Request,
        email: str = Form(...),
        slug: str = Form(...),
        resume: UploadFile = File(...),
        profile_json: str = Form(...),
    ) -> StreamingResponse:
        # Reuse the same validation path as /api/runs so behavior stays consistent.
        client = request.client.host if request.client else "unknown"
        if client not in ("127.0.0.1", "::1", "localhost") and _rate_limited(
            client
        ):
            raise HTTPException(
                status_code=429,
                detail="Rate limited: at most one run per minute per IP.",
            )

        slug = _normalize_slug(slug)
        email = email.strip()
        if not SLUG_RE.match(slug):
            raise HTTPException(status_code=400, detail="Invalid slug.")
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid email address.")

        pj = profile_json.strip()
        if len(pj.encode("utf-8")) > MAX_PROFILE_JSON_BYTES:
            raise HTTPException(status_code=400, detail="Profile JSON is too large.")
        try:
            raw = json.loads(pj)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid profile JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Profile must be a JSON object.")

        user = _normalize_profile_payload(raw)
        _validate_profile(user)

        template_path = _repo_root() / "profiles" / "richa.yaml"
        if not template_path.is_file():
            raise HTTPException(
                status_code=500,
                detail="Server is missing profiles/richa.yaml (template).",
            )
        try:
            yaml_text = merge_profile_with_template(user, template_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        yb = yaml_text.encode("utf-8")
        rb = await resume.read()
        if len(rb) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Resume must be at most {MAX_UPLOAD_BYTES} bytes.",
            )
        try:
            resume_out = decode_resume_bytes(rb, resume.filename).encode("utf-8")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Classify the request: owner / BYOK / free.
        owner_email = os.environ.get("JOB_HUNTER_UI_OWNER_EMAIL", "").strip().lower()
        is_owner = bool(owner_email) and email.lower() == owner_email
        llm_keys, source_keys = _read_byok_headers(request)
        is_byok = bool(llm_keys or source_keys)

        tier: str
        gate_info: dict[str, int] | None = None
        if is_owner:
            tier = "owner"
        elif is_byok:
            tier = "byok"
        else:
            tier = "free"
            try:
                gate_info = check_and_count(client)
            except GateError as exc:
                raise HTTPException(
                    status_code=429,
                    detail={"message": str(exc), "scope": exc.scope},
                ) from exc

        # Source allowlist: free tier gets GH+Lever+RSS only; BYOK/owner get
        # everything the server supports (config.yaml default).
        enabled: list[str] | None = (
            list(FREE_TIER_SOURCES) if tier == "free" else None
        )

        run_id = str(uuid.uuid4())
        root = Path.home() / ".job_hunter" / "ui-runs" / run_id
        try:
            _write_workspace(root, slug, resume_out, yb, enabled_sources=enabled)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        send_smtp = _should_send_smtp_ui(email)
        with _lock:
            _runs[run_id] = RunState()

        # Thread-safe queue bridges the sync worker thread → async generator.
        q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        done_event = threading.Event()
        result_box: dict[str, Any] = {}

        def hook(ev: dict[str, Any]) -> None:
            partial_html = ev.get("partial_report_html")
            if partial_html:
                with _lock:
                    st = _runs.get(run_id)
                    if st and not st.report_html:
                        st.report_html = partial_html
                q.put({"type": "progress", "phase": "interim_report",
                       "partial_download_available": True, "ts": time.time()})
                return
            q.put({"type": "progress", **ev, "ts": time.time()})

        def worker() -> None:
            try:
                # Serialize: run() temporarily mutates os.environ for BYOK
                # source keys, so we can't let two runs interleave.
                with _run_serializer:
                    summary = run(
                        root,
                        dry_run=False,
                        verbose=False,
                        discover=False,
                        email_to=email if send_smtp else None,
                        send_smtp=send_smtp,
                        progress_hook=hook,
                        llm_keys=llm_keys or None,
                        source_keys=source_keys or None,
                    )
                result_box["summary"] = summary
                clean = _summary_without_report(summary)
                with _lock:
                    st = _runs.get(run_id)
                    if st:
                        st.status = "done"
                        st.summary = clean
                        st.report_html = summary.get("report_html")
                        st.report_subject = summary.get("report_subject")
                        st.email_sent = bool(summary.get("email_sent"))
                q.put(
                    {
                        "type": "done",
                        "summary": clean,
                        "email_sent": bool(summary.get("email_sent")),
                        "download_available": bool(summary.get("report_html")),
                        "run_id": run_id,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                q.put(
                    {
                        "type": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            finally:
                done_event.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        start_payload: dict[str, Any] = {
            "type": "start",
            "run_id": run_id,
            "tier": tier,
            "enabled_sources": enabled or _default_enabled_sources(),
        }
        if gate_info:
            start_payload["gate"] = gate_info

        async def event_stream() -> AsyncIterator[bytes]:
            # Initial event so the client knows the connection is live.
            yield _sse(start_payload)
            loop = asyncio.get_running_loop()
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    # Client bailed; the worker thread continues but its output
                    # is discarded. We don't kill it — the email will still send.
                    return
                try:
                    ev = await loop.run_in_executor(
                        None, lambda: q.get(timeout=1.0)
                    )
                except queue.Empty:
                    # Keepalive comment every 15s so proxies don't close the
                    # connection. SSE comments start with ":" and are ignored
                    # by the client.
                    now = time.monotonic()
                    if now - last_keepalive >= 15.0:
                        yield b": keepalive\n\n"
                        last_keepalive = now
                    if done_event.is_set() and q.empty():
                        return
                    continue
                yield _sse(ev)
                last_keepalive = time.monotonic()
                if ev.get("type") in ("done", "error"):
                    return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
                "Connection": "keep-alive",
            },
        )

    def _parse_ui_run_id(run_id: str) -> str | None:
        try:
            return str(uuid.UUID((run_id or "").strip()))
        except ValueError:
            return None

    @app.get("/api/run/checkpoint/{run_id}")
    async def get_run_checkpoint(run_id: str) -> JSONResponse:
        rid = _parse_ui_run_id(run_id)
        if not rid:
            raise HTTPException(status_code=400, detail="Invalid run_id.")
        root = Path.home() / ".job_hunter" / "ui-runs" / rid
        return JSONResponse(
            {"run_id": rid, "resumable": root.is_dir() and has_checkpoint(root)}
        )

    @app.post("/api/run/stream/resume")
    async def post_run_stream_resume(
        request: Request,
        email: str = Form(...),
        run_id: str = Form(...),
    ) -> StreamingResponse:
        """Continue a failed UI run from on-disk checkpoint (no re-upload)."""
        client = request.client.host if request.client else "unknown"
        if client not in ("127.0.0.1", "::1", "localhost") and _rate_limited(
            client
        ):
            raise HTTPException(
                status_code=429,
                detail="Rate limited: at most one run per minute per IP.",
            )

        rid = _parse_ui_run_id(run_id)
        if not rid:
            raise HTTPException(status_code=400, detail="Invalid run_id.")

        email = email.strip()
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid email address.")

        root = Path.home() / ".job_hunter" / "ui-runs" / rid
        if not root.is_dir():
            raise HTTPException(
                status_code=404,
                detail="That run workspace no longer exists on this machine.",
            )
        if not has_checkpoint(root):
            raise HTTPException(
                status_code=400,
                detail="No resumable checkpoint for this run (only runs that failed "
                "after saving progress can be resumed).",
            )

        owner_email = os.environ.get("JOB_HUNTER_UI_OWNER_EMAIL", "").strip().lower()
        is_owner = bool(owner_email) and email.lower() == owner_email
        llm_keys, source_keys = _read_byok_headers(request)
        is_byok = bool(llm_keys or source_keys)

        if is_owner:
            tier = "owner"
        elif is_byok:
            tier = "byok"
        else:
            tier = "free"

        send_smtp = _should_send_smtp_ui(email)
        with _lock:
            _runs[rid] = RunState()

        q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        done_event = threading.Event()

        def hook(ev: dict[str, Any]) -> None:
            partial_html = ev.get("partial_report_html")
            if partial_html:
                with _lock:
                    st = _runs.get(rid)
                    if st and not st.report_html:
                        st.report_html = partial_html
                q.put({"type": "progress", "phase": "interim_report",
                       "partial_download_available": True, "ts": time.time()})
                return
            q.put({"type": "progress", **ev, "ts": time.time()})

        def worker() -> None:
            try:
                with _run_serializer:
                    summary = run(
                        root,
                        dry_run=False,
                        verbose=False,
                        discover=False,
                        email_to=email if send_smtp else None,
                        send_smtp=send_smtp,
                        progress_hook=hook,
                        llm_keys=llm_keys or None,
                        source_keys=source_keys or None,
                        resume_from_checkpoint=True,
                    )
                clean = _summary_without_report(summary)
                with _lock:
                    st = _runs.get(rid)
                    if st:
                        st.status = "done"
                        st.summary = clean
                        st.report_html = summary.get("report_html")
                        st.report_subject = summary.get("report_subject")
                        st.email_sent = bool(summary.get("email_sent"))
                q.put(
                    {
                        "type": "done",
                        "summary": clean,
                        "email_sent": bool(summary.get("email_sent")),
                        "download_available": bool(summary.get("report_html")),
                        "run_id": rid,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                q.put(
                    {
                        "type": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            finally:
                done_event.set()

        threading.Thread(target=worker, daemon=True).start()

        start_payload: dict[str, Any] = {
            "type": "start",
            "run_id": rid,
            "tier": tier,
            "resuming": True,
            "enabled_sources": _default_enabled_sources(),
        }

        async def event_stream() -> AsyncIterator[bytes]:
            yield _sse(start_payload)
            loop = asyncio.get_running_loop()
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await loop.run_in_executor(
                        None, lambda: q.get(timeout=1.0)
                    )
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_keepalive >= 15.0:
                        yield b": keepalive\n\n"
                        last_keepalive = now
                    if done_event.is_set() and q.empty():
                        return
                    continue
                yield _sse(ev)
                last_keepalive = time.monotonic()
                if ev.get("type") in ("done", "error"):
                    return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    static_dir = _repo_root() / "web" / "dist"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "job_hunter.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
