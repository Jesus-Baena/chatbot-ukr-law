"""
Shared catalogue-source logic for the Rada legislation catalogue.

Both the bootstrap fetch (``1_fetch_catalogue.py``) and the incremental update
(``4_incremental_update.py``) go through this module so they use the *exact same*
robust fetch/fallback chain:

    open-data JSON feed  ->  CSV feed  ->  portal page links  ->
    doc.txt full feed (cp1251)  ->  built-in minimal seed set

``load_catalogue_entries()`` returns ``(entries, source)`` where ``source``
records which provider actually produced the data. A ``source`` of ``"seed"``
means every live endpoint failed and only the built-in test set is available —
callers that must not corrupt persistent state (e.g. the incremental update
watermark) should treat that as a *failed fetch*, not as "no new laws".

Previously the incremental updater had its own ad-hoc feed parser that assumed
``zak.json`` returns a flat list of law records. It does not (it returns an
RSS-like envelope of dataset metadata), so the incremental path silently
ingested nothing. Consolidating the logic here keeps the two entry points from
drifting apart again.
"""

import csv
import io
import re
import time

import requests

from config import REQUEST_TIMEOUT


# Open data portal catalogue endpoints (tried in order).
CATALOGUE_ENDPOINTS = [
    "https://data.rada.gov.ua/open/data/zak.json",
    "https://data.rada.gov.ua/open/data/zak.csv",
]

# Large full-text laws feed (cp1251 encoded) used when the primary feed only
# returns dataset metadata instead of law records.
DOC_TXT_URL = "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; rada-rag/1.0; research pipeline)",
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "uk,en",
}

# Minimal, known-good seed set used only when every live endpoint fails. Kept as
# raw dicts and normalized through the same path as live data.
MINIMAL_SEED_RAW = [
    {"id": "2341-14", "title": "Кримінальний кодекс України", "date": "2001-04-05", "category": "Закон", "status": "Valid"},
    {"id": "254%D0%BA%2F96-%D0%B2%D1%80", "title": "Конституція України", "date": "1996-06-28", "category": "Закон", "status": "Valid"},
    {"id": "1706-18", "title": "Про забезпечення прав і свобод внутрішньо переміщених осіб", "date": "2014-10-20", "category": "Закон", "status": "Valid"},
    {"id": "389-19", "title": "Про правовий режим воєнного стану", "date": "2015-05-12", "category": "Закон", "status": "Valid"},
    {"id": "2801-12", "title": "Основи законодавства України про охорону здоров'я", "date": "1992-11-19", "category": "Закон", "status": "Valid"},
]


def is_law_id(value: str) -> bool:
    """Heuristic: real law IDs usually contain digits (e.g. 1706-18, 2341-14)."""
    return bool(re.search(r"\d", value or ""))


def normalize_entry(raw: dict) -> dict | None:
    """Normalize a catalogue entry to the standard shape.

    Raw fields vary by endpoint — map them to id/title/date/category/status/url.
    Returns ``None`` when no law ID can be found.
    """
    law_id = (
        raw.get("id") or raw.get("num") or raw.get("number") or
        raw.get("law_id") or raw.get("zakon_id")
    )
    if not law_id:
        return None

    law_id = str(law_id).strip()

    title = (
        raw.get("title") or raw.get("name") or
        raw.get("назва") or raw.get("заголовок") or ""
    ).strip()

    date = (
        raw.get("date") or raw.get("enacted") or
        raw.get("дата") or raw.get("date_signed") or ""
    ).strip()[:10]  # keep YYYY-MM-DD

    category = (
        raw.get("category") or raw.get("type") or
        raw.get("вид") or raw.get("тип") or ""
    ).strip()

    status = (
        raw.get("status") or raw.get("статус") or "unknown"
    ).strip()

    return {
        "id": law_id,
        "title": title,
        "date": date,
        "category": category,
        "status": status,
        "url": f"https://zakon.rada.gov.ua/laws/show/{law_id}",
    }


def fetch_catalogue_json(url: str) -> list[dict]:
    """Fetch catalogue as JSON, unwrapping the RSS-like ``item`` envelope."""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Current feed wraps records in an RSS-like envelope under `item`.
        if isinstance(data.get("item"), list):
            return data["item"]
    raise ValueError("Unsupported JSON catalogue format")


def fetch_catalogue_csv(url: str) -> list[dict]:
    """Fetch catalogue as CSV."""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def fetch_catalogue_doc_txt(max_items: int | None = None) -> list[dict]:
    """Fetch and parse the large laws feed from doc.txt (cp1251 encoded)."""
    r = requests.get(DOC_TXT_URL, headers=HEADERS, timeout=max(REQUEST_TIMEOUT, 60))
    r.raise_for_status()

    text = r.content.decode("cp1251", errors="ignore")
    entries = []
    pattern = re.compile(r"^\s*\d+\s+(\S+)\s+(.*?)\s+(\d{8})\s*$")

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if not m:
            continue

        law_id = m.group(1).strip()
        title = m.group(2).strip()
        raw_date = m.group(3)
        date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

        if not is_law_id(law_id):
            continue

        entries.append(
            {
                "id": law_id,
                "title": title,
                "date": date,
                "category": "",
                "status": "unknown",
                "url": f"https://zakon.rada.gov.ua/laws/show/{law_id}",
            }
        )
        if max_items is not None and len(entries) >= max_items:
            break

    return entries


def _fetch_from_portal_page() -> tuple[list[dict] | None, str | None]:
    """Last-ditch: scrape download links off the portal catalogue page."""
    try:
        r = requests.get(
            "https://data.rada.gov.ua/open/data/zak",
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        links = soup.find_all("a", href=True)
        download_links = [
            a["href"] for a in links
            if any(ext in a["href"] for ext in [".json", ".csv", ".xml"])
        ]
        for link in download_links[:3]:
            if not link.startswith("http"):
                link = "https://data.rada.gov.ua" + link
            try:
                raw = fetch_catalogue_json(link) if ".json" in link else fetch_catalogue_csv(link)
                if raw:
                    return raw, "portal"
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def load_catalogue_entries() -> tuple[list[dict], str]:
    """Load normalized catalogue entries plus the provider they came from.

    Returns ``(entries, source)`` with ``source`` in
    ``{"json", "csv", "portal", "doc_txt", "seed"}``. ``"seed"`` means all live
    sources failed and the caller is looking at the built-in test set only.
    """
    raw_entries: list[dict] | None = None
    source: str | None = None

    for url in CATALOGUE_ENDPOINTS:
        try:
            if url.endswith(".json"):
                raw_entries = fetch_catalogue_json(url)
                source = "json"
            else:
                raw_entries = fetch_catalogue_csv(url)
                source = "csv"
            if raw_entries:
                break
        except Exception:
            time.sleep(1)
            continue

    if not raw_entries:
        raw_entries, source = _fetch_from_portal_page()

    if not raw_entries:
        raw_entries, source = list(MINIMAL_SEED_RAW), "seed"

    entries = [e for e in (normalize_entry(r) for r in raw_entries) if e]

    # The `zak.json` endpoint may return dataset metadata (laws/docs/dict/...)
    # rather than law records. If the IDs don't look like laws, fall back to the
    # full doc.txt feed, which is the real bulk catalogue.
    if (
        source != "seed"
        and entries
        and sum(1 for e in entries if is_law_id(e["id"])) < max(1, len(entries) // 2)
    ):
        try:
            doc_entries = fetch_catalogue_doc_txt()
            if doc_entries:
                entries, source = doc_entries, "doc_txt"
        except Exception:
            entries, source = [normalize_entry(r) for r in MINIMAL_SEED_RAW], "seed"

    if not entries:
        entries, source = [normalize_entry(r) for r in MINIMAL_SEED_RAW], "seed"

    return entries, source or "seed"
