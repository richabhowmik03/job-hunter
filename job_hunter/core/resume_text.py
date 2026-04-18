"""Decode uploaded resume bytes to plain text for the LLM pipeline (markdown file on disk)."""

from __future__ import annotations

from io import BytesIO


def _looks_like_pdf(data: bytes, filename: str | None) -> bool:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):
        return True
    return len(data) >= 4 and data[:4] == b"%PDF"


def _decode_plain_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(data))
    except PdfReadError as exc:
        raise ValueError(
            "Could not read this PDF (file may be corrupt or unsupported)."
        ) from exc

    if reader.is_encrypted:
        # Empty password works for some "encrypted" PDFs with no user password.
        if reader.decrypt("") == 0:
            raise ValueError(
                "This PDF is password-protected. Remove the password or "
                "export as Markdown or plain text."
            )

    parts: list[str] = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip()


def decode_resume_bytes(data: bytes, filename: str | None = None) -> str:
    """
    Return UTF-8 text suitable for ``profiles/<slug>_resume.md``.

    Supports Markdown/plain text and PDF (text-based PDFs; scanned images are
    not OCR'd).
    """
    if not data.strip():
        raise ValueError("Resume file is empty.")

    if _looks_like_pdf(data, filename):
        text = _extract_pdf_text(data)
        if len(text) < 40:
            raise ValueError(
                "Could not extract enough text from this PDF. It may be a "
                "scanned image—export a text-based PDF, or upload Markdown or "
                "plain text."
            )
        return text

    return _decode_plain_text(data)
