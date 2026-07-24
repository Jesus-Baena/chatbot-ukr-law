import re
import time

import requests
from bs4 import BeautifulSoup

from config import DOCLING_API_URL, MAX_RETRIES, REQUEST_TIMEOUT, env_int
from service_clients import require_docling_url


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "uk,en;q=0.9",
}

DOCLING_TIMEOUT = max(60, REQUEST_TIMEOUT * 3)

LOW_SIGNAL_PATTERNS = [
    re.compile(r"верховна\s+рада\s+україни", re.I),
    re.compile(r"законодавство\s+україни", re.I),
]


def _is_low_signal_section_text(text: str) -> bool:
    """Detect placeholder/boilerplate extracts that contain no legal body."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return True

    # Very short fragments are usually navigation or header-only noise.
    if len(normalized) < 80:
        if any(pattern.search(normalized) for pattern in LOW_SIGNAL_PATTERNS):
            return True

    # If every line is a known boilerplate phrase, treat as empty content.
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    if lines and all(any(pattern.search(line) for pattern in LOW_SIGNAL_PATTERNS) for line in lines):
        return True

    return False


def safe_filename(law_id: str) -> str:
    """Convert law ID to safe filename."""
    return re.sub(r"[^\w\-]", "_", law_id) + ".json"


def _extract_metadata_from_html(html: str) -> dict:
    """Extract lightweight metadata from the raw HTML page."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    enacted_date = ""
    meta_area = (
        soup.find("div", class_=re.compile(r"(meta|header|info)", re.I))
        or soup.find("table", class_=re.compile(r"(meta|header|info)", re.I))
    )
    if meta_area:
        text = meta_area.get_text(" ", strip=True)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}", text)
        if dates:
            enacted_date = dates[0]

    return {
        "title": title,
        "law_number": "",
        "enacted_date": enacted_date,
        "status": "",
        "issuer": "",
    }


def _normalize_sections(raw_sections: list) -> list[dict]:
    """Normalize section-like objects from Docling or fallback outputs."""
    sections = []

    for item in raw_sections:
        if isinstance(item, str):
            text = item.strip()
            if text:
                sections.append({"heading": "", "text": text})
            continue

        if not isinstance(item, dict):
            continue

        heading = str(
            item.get("heading")
            or item.get("title")
            or item.get("name")
            or item.get("label")
            or ""
        ).strip()
        text = str(
            item.get("text")
            or item.get("content")
            or item.get("body")
            or item.get("markdown")
            or ""
        ).strip()

        if text:
            sections.append({"heading": heading, "text": text})

    return sections


def _markdown_to_sections(markdown_text: str) -> list[dict]:
    """Split markdown-like content into headed sections."""
    sections = []
    heading = ""
    lines = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#"):
            if lines:
                sections.append({"heading": heading, "text": "\n".join(lines).strip()})
                lines = []
            heading = line.lstrip("#").strip()
            continue

        lines.append(line)

    if lines:
        sections.append({"heading": heading, "text": "\n".join(lines).strip()})

    return [section for section in sections if section["text"]]


def _docling_response_to_law(data: dict, law_id: str, url: str, html: str) -> dict | None:
    """Convert a Docling response into the project's law JSON shape."""
    document = data.get("document") if isinstance(data.get("document"), dict) else {}
    metadata = {}
    if isinstance(data.get("metadata"), dict):
        metadata.update(data["metadata"])
    if isinstance(document.get("metadata"), dict):
        metadata.update(document["metadata"])

    sections = []
    for candidate in (
        data.get("sections"),
        document.get("sections"),
        data.get("content"),
        document.get("content"),
    ):
        if isinstance(candidate, list):
            sections = _normalize_sections(candidate)
            if sections:
                break

    if not sections:
        markdown_text = ""
        for candidate in (
            document.get("md_content"),
            data.get("md_content"),
            document.get("text_content"),
            data.get("text_content"),
            data.get("markdown"),
            document.get("markdown"),
            data.get("text"),
            document.get("text"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                markdown_text = candidate.strip()
                break
        if markdown_text:
            sections = _markdown_to_sections(markdown_text)
            if not sections:
                sections = [{"heading": "", "text": markdown_text}]

    if not sections:
        return None

    html_metadata = _extract_metadata_from_html(html)
    title = (
        data.get("title")
        or document.get("title")
        or metadata.get("title")
        or html_metadata["title"]
    )
    law_number = data.get("law_number") or document.get("law_number") or metadata.get("law_number") or ""
    enacted_date = (
        data.get("enacted_date")
        or document.get("enacted_date")
        or metadata.get("enacted_date")
        or html_metadata["enacted_date"]
    )
    status = data.get("status") or document.get("status") or metadata.get("status") or ""
    issuer = data.get("issuer") or document.get("issuer") or metadata.get("issuer") or ""

    return {
        "id": law_id,
        "title": title,
        "law_number": law_number,
        "enacted_date": enacted_date,
        "status": status,
        "issuer": issuer,
        "url": url,
        "sections": sections,
        "section_count": len(sections),
    }


def extract_law_from_html(html: str, law_id: str, url: str) -> dict | None:
    """Legacy HTML extraction retained as a bounded fallback."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    law_number = ""
    enacted_date = ""
    status = ""
    issuer = ""

    meta_area = (
        soup.find("div", class_=re.compile(r"(meta|header|info)", re.I))
        or soup.find("table", class_=re.compile(r"(meta|header|info)", re.I))
    )
    if meta_area:
        text = meta_area.get_text(" ", strip=True)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}", text)
        if dates:
            enacted_date = dates[0]

    body_selectors = [
        {"id": re.compile(r"law", re.I)},
        {"class": re.compile(r"(document|content|law|text)", re.I)},
        {"id": "content"},
        {"id": "main"},
    ]

    body_div = None
    for selector in body_selectors:
        body_div = soup.find("div", selector)
        if body_div:
            break

    if not body_div:
        body_div = soup.find("article") or soup.find("main") or soup.find("body")

    if not body_div:
        return None

    for tag in body_div.find_all(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()

    sections = []
    current_section = {"heading": "", "text": ""}
    for element in body_div.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        tag = element.name
        text = element.get_text(" ", strip=True)
        if not text or len(text) < 3:
            continue

        if tag in ["h1", "h2", "h3", "h4"]:
            if current_section["text"].strip():
                sections.append(current_section)
            current_section = {"heading": text, "text": ""}
            continue

        if current_section["text"]:
            current_section["text"] += "\n" + text
        else:
            current_section["text"] = text

    if current_section["text"].strip():
        sections.append(current_section)

    if not sections:
        full_text = body_div.get_text("\n", strip=True)
        paragraphs = [part.strip() for part in full_text.split("\n\n") if part.strip()]
        sections = [{"heading": "", "text": paragraph} for paragraph in paragraphs]

    if not sections:
        return None

    if len(sections) == 1 and _is_low_signal_section_text(sections[0].get("text", "")):
        return None

    return {
        "id": law_id,
        "title": title,
        "law_number": law_number,
        "enacted_date": enacted_date,
        "status": status,
        "issuer": issuer,
        "url": url,
        "sections": sections,
        "section_count": len(sections),
    }


_ARTICLE_HEADING_RE = re.compile(
    r"^(Стаття\s+\d+[\d\-\.]*[\.:]?|РОЗДІЛ\s+[IVXLCDM\d]+|Глава\s+\d+|ЧАСТИНА\s+\w+)",
    re.I | re.UNICODE,
)


def _normalize_html_for_docling(html: str) -> str:
    """Promote Rada's article/chapter <p><span> headings to real <h3>/<h2>
    elements so Docling's HTML pipeline can segment the document correctly.

    Rada's print pages store every paragraph — including article titles — as
    <p><span class="rvtsN">...</span></p>.  Docling only uses proper heading
    tags (<h1>–<h6>) to split a document into sections, so without this
    promotion it only finds the single <h2 class="hdr1"> at the very end.
    """
    soup = BeautifulSoup(html, "lxml")

    for p in soup.find_all("p"):
        # Collect all text from direct span/em/b children (skip nested links)
        text = p.get_text(" ", strip=True)
        if _ARTICLE_HEADING_RE.match(text):
            # Replace the <p> with an <h3> preserving the text
            h3 = soup.new_tag("h3")
            h3.string = text
            p.replace_with(h3)

    return str(soup)


def extract_law_via_docling(html: str, law_id: str, url: str) -> dict | None:
    """Use the configured Docling service as the primary extraction path."""
    base_url = require_docling_url().rstrip("/")
    endpoint = f"{base_url}/v1/convert/file"
    normalized_html = _normalize_html_for_docling(html)
    html_bytes = normalized_html.encode("utf-8")
    files = {"files": (f"{law_id}.html", html_bytes, "text/html")}
    response = requests.post(endpoint, files=files, timeout=DOCLING_TIMEOUT)
    response.raise_for_status()
    return _docling_response_to_law(response.json(), law_id, url, html)


_DOCLING_MIN_TOTAL_CHARS = 500  # fall back if Docling returns less than this


def extract_law(html: str, law_id: str, url: str) -> dict | None:
    """Extract a law using Docling first, with a bounded HTML fallback.

    The returned law dict carries an ``extraction_mode`` and a ``quality``
    assessment (see :func:`assess_law_quality`) so downstream steps and the
    corpus report can gate on structurally-collapsed extractions.
    """
    result = None

    if DOCLING_API_URL:
        try:
            docling_result = extract_law_via_docling(html, law_id, url)
            if docling_result:
                total_chars = sum(len(s.get("text", "")) for s in docling_result.get("sections", []))
                if total_chars >= _DOCLING_MIN_TOTAL_CHARS:
                    docling_result["extraction_mode"] = "docling"
                    result = docling_result
                else:
                    print(f"\n  Docling returned too little content for {law_id} ({total_chars} chars), using HTML fallback")
        except requests.RequestException as exc:
            print(f"\n  Docling request failed for {law_id}: {exc}")
        except ValueError as exc:
            print(f"\n  Docling returned invalid JSON for {law_id}: {exc}")

    if result is None:
        result = extract_law_from_html(html, law_id, url)
        if result and DOCLING_API_URL:
            result["extraction_mode"] = "html_fallback"
        elif result:
            result["extraction_mode"] = "html_only"

    if result is not None:
        result["quality"] = assess_law_quality(result)

    return result


# --- Extraction quality gate -------------------------------------------------
# A collapsed/failed extraction typically yields a document that carries a
# substantial amount of body text but was never segmented into articles or
# sections (see review finding #1: ~96% of the indexed corpus landed with 1–3
# sections). These thresholds flag that pattern without penalising genuinely
# short decrees, which legitimately have 1–2 short sections.
LOW_QUALITY_MIN_CHARS = env_int("LOW_QUALITY_MIN_CHARS", 2000)
LOW_QUALITY_MAX_SECTIONS = env_int("LOW_QUALITY_MAX_SECTIONS", 2)
THIN_MAX_CHARS = env_int("THIN_MAX_CHARS", 200)

_docling_warning_emitted = False


def warn_if_docling_disabled() -> None:
    """Emit a loud, one-time warning when Docling extraction is unavailable.

    Without Docling the pipeline falls back to the bounded HTML parser, which is
    the primary cause of structurally-collapsed extractions.
    """
    global _docling_warning_emitted
    if DOCLING_API_URL or _docling_warning_emitted:
        return
    _docling_warning_emitted = True
    banner = "!" * 72
    print(
        "\n" + banner
        + "\nWARNING: DOCLING_API_URL is not set — using the bounded HTML fallback"
        + "\nparser. Extraction quality will be degraded and many laws may collapse"
        + "\nto 1–2 sections. Set DOCLING_API_URL for structured article segmentation."
        + "\n" + banner + "\n",
        flush=True,
    )


def count_article_headings(sections: list[dict]) -> int:
    """Count sections whose heading looks like a real article/chapter marker."""
    count = 0
    for section in sections:
        heading = (section.get("heading") or "").strip()
        if heading and _ARTICLE_HEADING_RE.match(heading):
            count += 1
    return count


def assess_law_quality(law: dict) -> dict:
    """Assess whether an extracted law looks structurally complete.

    Returns the raw signals plus a ``quality`` label:
      - ``"suspect"``: substantial body text but almost no structure — the
        signature of a collapsed/failed extraction.
      - ``"thin"``: barely any content at all.
      - ``"ok"``: otherwise.
    """
    sections = law.get("sections", []) or []
    section_count = len(sections)
    total_chars = sum(len(s.get("text") or "") for s in sections)
    article_headings = count_article_headings(sections)

    suspect = (
        total_chars >= LOW_QUALITY_MIN_CHARS
        and section_count <= LOW_QUALITY_MAX_SECTIONS
        and article_headings == 0
    )
    if suspect:
        quality = "suspect"
    elif total_chars < THIN_MAX_CHARS:
        quality = "thin"
    else:
        quality = "ok"

    return {
        "section_count": section_count,
        "total_chars": total_chars,
        "article_headings": article_headings,
        "quality": quality,
    }


def summarize_quality(assessments: list[dict]) -> str:
    """Render a per-run section-count histogram and quality-label breakdown."""
    if not assessments:
        return "  (no laws assessed)"

    buckets = [
        ("0", lambda n: n == 0),
        ("1", lambda n: n == 1),
        ("2", lambda n: n == 2),
        ("3", lambda n: n == 3),
        ("4-9", lambda n: 4 <= n <= 9),
        ("10-49", lambda n: 10 <= n <= 49),
        ("50+", lambda n: n >= 50),
    ]
    total = len(assessments)
    lines = [f"  Laws assessed: {total}", "  Section-count distribution:"]
    for label, predicate in buckets:
        count = sum(1 for a in assessments if predicate(a.get("section_count", 0)))
        if count:
            pct = 100.0 * count / total
            lines.append(f"    {label:>6} sections: {count:>6}  ({pct:4.1f}%)")

    quality_counts = {"ok": 0, "thin": 0, "suspect": 0}
    for a in assessments:
        quality_counts[a.get("quality", "ok")] = quality_counts.get(a.get("quality", "ok"), 0) + 1
    lines.append("  Quality labels:")
    for label in ("ok", "thin", "suspect"):
        count = quality_counts.get(label, 0)
        pct = 100.0 * count / total
        lines.append(f"    {label:>8}: {count:>6}  ({pct:4.1f}%)")

    suspect = quality_counts.get("suspect", 0)
    if suspect:
        pct = 100.0 * suspect / total
        lines.append(
            f"  ⚠ {suspect} law(s) ({pct:.1f}%) look structurally collapsed "
            "(substantial text, no article segmentation)."
        )
    return "\n".join(lines)


def fetch_with_retry(url: str) -> requests.Response | None:
    """Fetch URL with exponential backoff retry."""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response
            if response.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"\n  Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            if response.status_code == 404:
                return None
            print(f"\n  HTTP {response.status_code} for {url}")
            time.sleep(5)
        except requests.RequestException as exc:
            wait = 5 * (attempt + 1)
            print(f"\n  Error: {exc}. Retry in {wait}s...")
            time.sleep(wait)
    return None
