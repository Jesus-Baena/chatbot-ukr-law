"""
Step 1: Fetch the legislation catalogue from data.rada.gov.ua

Downloads the full list of law IDs, titles, dates, and categories.
This is the seed list for the scraper in step 2.

Output: data/catalogue.json
"""

import json
import csv
import io
import time
import re
import requests
from pathlib import Path
from config import (
    CATALOGUE_PATH, DATE_FROM, MAX_LAWS, CATALOGUE_OFFSET,
    CATEGORY_FILTER, HUMANITARIAN_KEYWORDS, REQUEST_TIMEOUT
)


# Open data portal catalogue endpoints
# The portal exposes CSV/JSON downloads — try both
CATALOGUE_ENDPOINTS = [
    # Primary: open data JSON feed
    "https://data.rada.gov.ua/open/data/zak.json",
    # Fallback: CSV
    "https://data.rada.gov.ua/open/data/zak.csv",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; rada-rag/1.0; research pipeline)",
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "uk,en",
}


def fetch_catalogue_json(url: str) -> list[dict]:
    """Attempt to fetch catalogue as JSON."""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Current feed is wrapped in an RSS-like envelope with records in `item`.
        if isinstance(data.get("item"), list):
            return data["item"]
    raise ValueError("Unsupported JSON catalogue format")


def fetch_catalogue_csv(url: str) -> list[dict]:
    """Attempt to fetch catalogue as CSV."""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def normalize_entry(raw: dict) -> dict | None:
    """
    Normalize a catalogue entry to a standard shape.
    
    Raw fields vary by endpoint — map them to:
      id, title, date, category, status, url
    """
    # Try to extract law ID — may be in 'id', 'num', 'number', etc.
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


def is_law_id(value: str) -> bool:
    """Heuristic: real law IDs usually contain digits (e.g. 1706-18, 2341-14)."""
    return bool(re.search(r"\d", value))


def fetch_catalogue_doc_txt(max_items: int | None = None) -> list[dict]:
    """Fetch and parse the large laws feed from doc.txt (cp1251 encoded)."""
    url = "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.txt"
    r = requests.get(url, headers=HEADERS, timeout=max(REQUEST_TIMEOUT, 60))
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


def apply_filters(entries: list[dict]) -> list[dict]:
    """Filter catalogue by date, category keywords, and max count."""
    filtered = []

    for e in entries:
        # Date filter
        if e["date"] and e["date"] < DATE_FROM:
            continue

        # Category keyword filter
        if CATEGORY_FILTER == "humanitarian":
            text = (e["title"] + " " + e["category"]).lower()
            if not any(kw.lower() in text for kw in HUMANITARIAN_KEYWORDS):
                continue
        elif CATEGORY_FILTER:
            # Custom keyword
            text = (e["title"] + " " + e["category"]).lower()
            if CATEGORY_FILTER.lower() not in text:
                continue

        filtered.append(e)

    if CATALOGUE_OFFSET > 0:
        filtered = filtered[CATALOGUE_OFFSET:]

    if MAX_LAWS > 0:
        filtered = filtered[:MAX_LAWS]

    return filtered


def main():
    print("=== Step 1: Fetching Rada legislation catalogue ===\n")

    raw_entries = None

    for url in CATALOGUE_ENDPOINTS:
        print(f"Trying: {url}")
        try:
            if url.endswith(".json"):
                raw_entries = fetch_catalogue_json(url)
            else:
                raw_entries = fetch_catalogue_csv(url)
            print(f"  ✓ Got {len(raw_entries)} raw entries")
            break
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            time.sleep(2)

    if not raw_entries:
        # Fallback: try fetching the data portal catalogue page
        # and parse the download links
        print("\nDirect endpoints failed. Attempting portal catalogue page...")
        try:
            r = requests.get(
                "https://data.rada.gov.ua/open/data/zak",
                headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            # Parse available download links from HTML
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")
            links = soup.find_all("a", href=True)
            download_links = [
                a["href"] for a in links
                if any(ext in a["href"] for ext in [".json", ".csv", ".xml"])
            ]
            print(f"  Found download links: {download_links}")
            # Try the first valid one
            for link in download_links[:3]:
                if not link.startswith("http"):
                    link = "https://data.rada.gov.ua" + link
                try:
                    raw_entries = fetch_catalogue_json(link) if ".json" in link \
                        else fetch_catalogue_csv(link)
                    print(f"  ✓ Got {len(raw_entries)} entries from {link}")
                    break
                except Exception:
                    pass
        except Exception as e:
            print(f"  ✗ Portal fallback failed: {e}")

    if not raw_entries:
        print("\n⚠ Could not fetch live catalogue. Creating minimal test set...")
        # Minimal test set of known law IDs for development
        raw_entries = [
            {"id": "2341-14", "title": "Кримінальний кодекс України", "date": "2001-04-05", "category": "Закон", "status": "Valid"},
            {"id": "254%D0%BA%2F96-%D0%B2%D1%80", "title": "Конституція України", "date": "1996-06-28", "category": "Закон", "status": "Valid"},
            {"id": "1706-18", "title": "Про забезпечення прав і свобод внутрішньо переміщених осіб", "date": "2014-10-20", "category": "Закон", "status": "Valid"},
            {"id": "389-19", "title": "Про правовий режим воєнного стану", "date": "2015-05-12", "category": "Закон", "status": "Valid"},
            {"id": "2801-12", "title": "Основи законодавства України про охорону здоров'я", "date": "1992-11-19", "category": "Закон", "status": "Valid"},
        ]

    # Normalize
    entries = [normalize_entry(r) for r in raw_entries]
    entries = [e for e in entries if e]  # drop None

    # The `zak.json` endpoint may return dataset metadata (laws/docs/dict/...) instead
    # of law records. If IDs don't look like laws, use the built-in minimal seed set.
    if entries and sum(1 for e in entries if is_law_id(e["id"])) < max(1, len(entries) // 2):
        print("\n⚠ Feed returned metadata entries instead of law records. Trying doc.txt fallback...")
        try:
            entries = fetch_catalogue_doc_txt()
            print(f"  ✓ Parsed {len(entries)} candidate law entries from doc.txt")
        except Exception as e:
            print(f"  ✗ doc.txt fallback failed: {e}")
            print("  Using minimal test set...")
            entries = [
                {"id": "2341-14", "title": "Кримінальний кодекс України", "date": "2001-04-05", "category": "Закон", "status": "Valid", "url": "https://zakon.rada.gov.ua/laws/show/2341-14"},
                {"id": "254%D0%BA%2F96-%D0%B2%D1%80", "title": "Конституція України", "date": "1996-06-28", "category": "Закон", "status": "Valid", "url": "https://zakon.rada.gov.ua/laws/show/254%D0%BA%2F96-%D0%B2%D1%80"},
                {"id": "1706-18", "title": "Про забезпечення прав і свобод внутрішньо переміщених осіб", "date": "2014-10-20", "category": "Закон", "status": "Valid", "url": "https://zakon.rada.gov.ua/laws/show/1706-18"},
                {"id": "389-19", "title": "Про правовий режим воєнного стану", "date": "2015-05-12", "category": "Закон", "status": "Valid", "url": "https://zakon.rada.gov.ua/laws/show/389-19"},
                {"id": "2801-12", "title": "Основи законодавства України про охорону здоров'я", "date": "1992-11-19", "category": "Закон", "status": "Valid", "url": "https://zakon.rada.gov.ua/laws/show/2801-12"},
            ]
    print(f"\nNormalized: {len(entries)} entries")

    # Apply filters
    entries = apply_filters(entries)
    print(
        f"After filters (date≥{DATE_FROM}, offset={CATALOGUE_OFFSET}, max={MAX_LAWS}): "
        f"{len(entries)} entries"
    )

    # Save
    CATALOGUE_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n✓ Saved to {CATALOGUE_PATH}")

    # Summary stats
    by_category = {}
    for e in entries:
        by_category[e["category"]] = by_category.get(e["category"], 0) + 1
    print("\nTop categories:")
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1])[:10]:
        print(f"  {cat or '(none)'}: {count}")


if __name__ == "__main__":
    main()
