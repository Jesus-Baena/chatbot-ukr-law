#!/usr/bin/env python3
"""Backfill empty law titles in the Qdrant `rada_legislation` collection.

The auto-scraped Rada corpus was embedded with an empty `title` (both the
top-level payload and the nested `metadata.title` that Flowise surfaces in
`sourceDocuments`). Clean Ukrainian titles exist in the Postgres staging table
`rada_staging_laws.title`, keyed by `law_id`. This script copies them into the
Qdrant payload so the chat UI can render human-readable citations instead of
bare law ids.

Requires the Postgres staging tunnel to be running (see
`0_start_postgres_tunnel.sh`); Qdrant is reached over its public HTTPS endpoint.

Usage:
  python 11_backfill_law_titles.py --dry-run   # report only
  python 11_backfill_law_titles.py             # apply
  python 11_backfill_law_titles.py --collection rada_plus_reports   # also patch the merged coll
"""
import argparse
import sys

import psycopg  # type: ignore[import-not-found]
import requests

# Force IPv4 — this host has no IPv6 route, and qdrant.baena.info returns AAAA.
import urllib3.util.connection as _uc
import socket
_uc.allowed_gai_family = lambda: socket.AF_INET

from config import DATABASE_URL, QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION

# Cloudflare 1010-blocks non-browser User-Agents in front of qdrant.baena.info.
HEADERS = {"User-Agent": "Mozilla/5.0", "api-key": QDRANT_API_KEY, "Content-Type": "application/json"}


def load_titles() -> dict[str, str]:
    """Return {law_id: title} for staged laws that have a usable title.

    Prefers the curated English translation (`english_title`); falls back to the
    Ukrainian catalogue title, which carries trailing tab-delimited junk from the
    Rada open-data feed, so we keep only the text before the first tab. The
    dedicated `title` column is unpopulated by the current scraper, so it is
    intentionally not used here.
    """
    sql = r"""
        SELECT law_id,
               COALESCE(NULLIF(english_title, ''),
                        NULLIF(split_part(source_catalogue_json->>'title', E'\t', 1), '')) AS title
        FROM rada_staging_laws
        WHERE COALESCE(english_title, '') <> ''
           OR source_catalogue_json->>'title' IS NOT NULL
    """
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return {law_id: title.strip() for law_id, title in cur.fetchall() if title and title.strip()}


def law_ids_with_empty_title(collection: str) -> set[str]:
    """Scroll the collection and collect law_ids whose nested metadata.title is empty."""
    empty: set[str] = set()
    seen_any = False
    next_page = None
    base = QDRANT_URL.rstrip("/")
    while True:
        body: dict = {"limit": 512, "with_payload": True, "with_vector": False}
        if next_page is not None:
            body["offset"] = next_page
        r = requests.post(f"{base}/collections/{collection}/points/scroll",
                          json=body, headers=HEADERS, timeout=30)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res["points"]:
            pl = p.get("payload", {})
            meta = pl.get("metadata") or {}
            title = (meta.get("title") or pl.get("title") or "").strip()
            law_id = pl.get("law_id") or meta.get("law_id")
            if law_id:
                seen_any = True
                if not title:
                    empty.add(law_id)
        next_page = res.get("next_page_offset")
        if next_page is None:
            break
    if not seen_any:
        print(f"  ! no points scrolled from {collection}", file=sys.stderr)
    return empty


def set_title(collection: str, law_id: str, title: str) -> None:
    """Set both top-level `title` and nested `metadata.title` for all points of a law."""
    base = QDRANT_URL.rstrip("/")
    flt = {"must": [{"key": "law_id", "match": {"value": law_id}}]}
    # nested metadata.title (what Flowise reads via metadataPayloadKey=metadata)
    r1 = requests.post(f"{base}/collections/{collection}/points/payload",
                       json={"payload": {"title": title}, "filter": flt, "key": "metadata"},
                       headers=HEADERS, timeout=30)
    r1.raise_for_status()
    # top-level title (kept in sync for completeness)
    r2 = requests.post(f"{base}/collections/{collection}/points/payload",
                       json={"payload": {"title": title}, "filter": flt},
                       headers=HEADERS, timeout=30)
    r2.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collection", default=QDRANT_COLLECTION, help="Qdrant collection to patch")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--limit", type=int, default=0, help="cap number of laws patched (0 = all)")
    args = ap.parse_args()

    print(f"Loading titles from Postgres staging ({'dry-run' if args.dry_run else 'APPLY'})...")
    titles = load_titles()
    print(f"  staged laws with a title: {len(titles)}")

    print(f"Scanning Qdrant '{args.collection}' for empty-title laws...")
    empty = law_ids_with_empty_title(args.collection)
    print(f"  laws currently missing a title: {len(empty)}")

    fixable = sorted(empty & set(titles))
    unmatched = len(empty) - len(fixable)
    print(f"  fixable (have a staged title): {len(fixable)}")
    print(f"  still unknown (no staged title): {unmatched}")
    if args.limit:
        fixable = fixable[: args.limit]

    if args.dry_run:
        for law_id in fixable[:10]:
            print(f"    would set {law_id} -> {titles[law_id][:70]}")
        print(f"  (dry-run) {len(fixable)} laws would be patched")
        return 0

    done = 0
    for law_id in fixable:
        try:
            set_title(args.collection, law_id, titles[law_id])
            done += 1
            if done % 200 == 0:
                print(f"  patched {done}/{len(fixable)}...")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {law_id}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"Done. Patched {done}/{len(fixable)} laws in '{args.collection}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
