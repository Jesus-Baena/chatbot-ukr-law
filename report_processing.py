"""
Extraction for secondary reports (expert analyses that review specific laws).

Reports differ from primary legislation: they are prose documents (PDF/DOCX/HTML)
with narrative headings rather than ``Стаття``/``РОЗДІЛ`` articles, and they
*review* specific laws rather than being law themselves. This module extracts a
report into the same section shape the chunker consumes, and auto-derives the
linkage metadata that makes reports useful:

  - ``reviews_law_refs`` : law numbers the report cites (e.g. "2297-VI", "8153")
  - ``pub_date``         : publication date (latest dated mention as a proxy)
  - ``title``            : the report's real title (not the source filename)
  - ``summary``          : the executive-summary section when present

Docling is the primary extractor (handles PDF/DOCX/HTML layouts); a bundled
PyMuPDF/HTML fallback keeps the module usable without Docling.
"""

import re
from datetime import datetime
from pathlib import Path

import requests

from config import DOCLING_API_URL, REQUEST_TIMEOUT
from date_utils import normalize_date
from law_processing import _markdown_to_sections, _normalize_sections, extract_law_from_html

DOCLING_TIMEOUT = max(60, REQUEST_TIMEOUT * 3)

_MONTHS = {
    m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}

# Narrative heading forms: "I." / "IV." roman top-level, "A." / "B." letter sub.
_H1_RE = re.compile(r"^((?:M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})))\.\s+(\S.*)$")
_H2_RE = re.compile(r"^([A-E])\.\s+([A-Z].*)$")
# Law / draft-law references: "No. 2297-VI", "№ 8153".
_LAW_REF_RE = re.compile(r"(?:No\.|№)\s*([0-9][0-9A-Za-z\-\/]*)")

_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _looks_h1(line: str) -> bool:
    m = _H1_RE.match(line)
    return bool(m and line[:1] in "IVX" and len(line) < 100)


def _looks_h2(line: str) -> bool:
    return bool(_H2_RE.match(line) and len(line) < 100)


def _sectionize_text(text: str) -> list[dict]:
    """Split narrative text into heading-led sections (roman/letter headings)."""
    sections = []
    heading = ""
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _looks_h1(line) or _looks_h2(line):
            if buf:
                sections.append({"heading": heading, "text": " ".join(buf).strip()})
                buf = []
            heading = line
        else:
            buf.append(line)
    if buf:
        sections.append({"heading": heading, "text": " ".join(buf).strip()})
    return [s for s in sections if s["text"]]


def _pdf_to_text(path: Path) -> str:
    """Extract text from a PDF using PyMuPDF (bundled fallback extractor)."""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "PyMuPDF (pymupdf) is required for PDF fallback extraction; "
            "install it or configure DOCLING_API_URL."
        ) from exc
    doc = fitz.open(path)
    try:
        return "\n".join(doc[i].get_text() for i in range(doc.page_count))
    finally:
        doc.close()


def extract_title(text: str) -> str:
    """The report title is the text before the first narrative heading."""
    title_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if _looks_h1(line):
            break
        if line:
            title_lines.append(line)
        if len(" ".join(title_lines)) > 300:
            break
    return " ".join(title_lines).strip()


def extract_reviews_law_refs(text: str) -> list[str]:
    """Law/draft-law numbers the report reviews, in first-seen order."""
    seen: list[str] = []
    for ref in _LAW_REF_RE.findall(text):
        token = ref.strip().rstrip(".,")
        if any(ch.isdigit() for ch in token) and token not in seen:
            seen.append(token)
    return seen


def extract_pub_date(text: str) -> str:
    """Publication-date proxy: the latest 'Month D, YYYY' mentioned (ISO)."""
    dates = []
    pattern = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})")
    for month, day, year in pattern.findall(text):
        try:
            dates.append(datetime(int(year), _MONTHS[month], int(day)))
        except ValueError:
            continue
    return max(dates).date().isoformat() if dates else ""


def _extract_via_docling(path: Path) -> list[dict]:
    """Send the raw file to Docling and normalize its response into sections."""
    base_url = DOCLING_API_URL.rstrip("/")
    endpoint = f"{base_url}/v1/convert/file"
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
    with path.open("rb") as handle:
        files = {"files": (path.name, handle.read(), mime)}
    response = requests.post(endpoint, files=files, timeout=DOCLING_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    document = data.get("document") if isinstance(data.get("document"), dict) else {}

    for candidate in (data.get("sections"), document.get("sections")):
        if isinstance(candidate, list):
            sections = _normalize_sections(candidate)
            if sections:
                return sections

    for candidate in (
        document.get("md_content"), data.get("md_content"),
        document.get("text_content"), data.get("text_content"),
        data.get("markdown"), document.get("markdown"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            sections = _markdown_to_sections(candidate.strip())
            if sections:
                return sections
    return []


def _extract_sections(path: Path) -> tuple[list[dict], str]:
    """Return (sections, extraction_mode). Docling first, then a local fallback."""
    if DOCLING_API_URL:
        try:
            sections = _extract_via_docling(path)
            if sections:
                return sections, "docling"
        except (requests.RequestException, ValueError) as exc:
            print(f"  Docling failed for {path.name} ({exc}); using local fallback")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        sections = _sectionize_text(_pdf_to_text(path))
        return sections, "pdf_fallback"
    if suffix in (".html", ".htm"):
        html = path.read_text(encoding="utf-8", errors="ignore")
        law = extract_law_from_html(html, path.stem, "")
        return (law.get("sections", []) if law else []), "html_fallback"
    return [], "unsupported"


def _report_text(sections: list[dict]) -> str:
    return "\n".join(
        f"{s.get('heading', '')}\n{s.get('text', '')}" for s in sections
    )


def extract_report(path: Path) -> dict | None:
    """Extract a report file into a law-like dict plus report metadata.

    Returns ``None`` when no text could be extracted. The returned dict is shaped
    for ``embedding_pipeline.law_to_chunks`` (``id``/``title``/``sections``/…)
    with report-specific fields added by the ingester.
    """
    sections, mode = _extract_sections(path)
    if not sections:
        return None

    text = _report_text(sections)
    summary = next(
        (s["text"] for s in sections if "executive summary" in (s.get("heading", "").lower())),
        "",
    )
    total_chars = sum(len(s.get("text", "")) for s in sections)

    return {
        "id": "",  # assigned by the ingester (filename slug)
        "title": extract_title(text),
        "sections": sections,
        "section_count": len(sections),
        "extraction_mode": mode,
        "reviews_law_refs": extract_reviews_law_refs(text),
        "pub_date": extract_pub_date(text),
        "summary": summary,
        "total_chars": total_chars,
    }
