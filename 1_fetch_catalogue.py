"""
Step 1: Fetch the legislation catalogue from data.rada.gov.ua

Downloads the full list of law IDs, titles, dates, and categories.
This is the seed list for the scraper in step 2.

The fetch/fallback chain (open-data feed -> doc.txt -> minimal seed) lives in
``catalogue_source.py`` so the incremental updater (step 4) shares it verbatim.

Output: data/catalogue.json
"""

import json

from config import (
    CATALOGUE_PATH, DATE_FROM, MAX_LAWS, CATALOGUE_OFFSET,
    CATEGORY_FILTER, HUMANITARIAN_KEYWORDS,
)
from catalogue_source import load_catalogue_entries


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

    entries, source = load_catalogue_entries()
    print(f"Catalogue source: {source}")
    if source == "seed":
        print("⚠ All live catalogue endpoints failed — using built-in minimal seed set.")
    print(f"Normalized: {len(entries)} entries")

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
