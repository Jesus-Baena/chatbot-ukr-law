"""
Step 9: Re-extract structurally-collapsed ("suspect") laws through Docling.

The extraction quality gate (``law_processing.assess_law_quality``) flags laws
that carry substantial text but no article segmentation — the signature of an
extraction that fell back to the bounded HTML parser instead of Docling. This
script finds those laws on disk, re-fetches them, and re-extracts through
Docling (the primary path when ``DOCLING_API_URL`` is set), refreshing both the
on-disk law JSON and the Postgres staging sections, and reports whether each
law actually improved.

It targets ONLY the suspect subset — far cheaper than re-scraping the whole
corpus. Laws carrying an ``error`` marker are left to ``6_retry_failed_ingest``.

By default it does NOT embed: run a full, consistent rebuild afterwards so the
whole collection is chunked identically:

    python 8_reembed_to_gemini.py --recreate --skip-suspect

Pass ``--embed`` to also push each re-extracted law straight to Qdrant
(delete + re-upsert per law) if you need them searchable before the rebuild.

Usage:
  python 9_reextract_suspect.py --dry-run     # list suspects, fetch nothing
  python 9_reextract_suspect.py --limit 5     # smoke test on 5 laws
  python 9_reextract_suspect.py               # re-extract all suspects
  python 9_reextract_suspect.py --embed       # ... and re-embed each now
"""

import argparse
import json
import time
from pathlib import Path

from config import DATABASE_URL, DOCLING_API_URL, LAWS_DIR, REQUEST_DELAY
from embedding_pipeline import (
    delete_law_from_qdrant,
    embed_chunks,
    law_to_chunks,
    setup_qdrant,
    upsert_to_qdrant,
)
from indexed_laws_tracker import upsert_indexed_law
from law_processing import (
    assess_law_quality,
    extract_law,
    fetch_with_retry,
    summarize_quality,
)
from service_clients import get_qdrant_client
from staging_db import (
    ensure_staging_schema,
    get_postgres_connection,
    stage_chunks_for_law,
    stage_law_with_sections,
    stage_raw_law_response,
)


def _load_catalogue_map() -> dict[str, dict]:
    path = Path("data/catalogue.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(row.get("id", "")).strip(): row for row in data if str(row.get("id", "")).strip()}


def _find_suspect_laws(laws_dir: Path) -> list[tuple[Path, dict, dict]]:
    """Return (path, payload, quality) for every law currently labelled suspect."""
    suspects = []
    for path in sorted(laws_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("error"):
            continue  # error markers are 6_retry_failed_ingest's job
        quality = payload.get("quality") or assess_law_quality(payload)
        if quality.get("quality") == "suspect":
            suspects.append((path, payload, quality))
    return suspects


def _reextract(path: Path, payload: dict, catalogue_map: dict, pg_conn) -> tuple[dict | None, str]:
    law_id = str(payload.get("id", "")).strip()
    if not law_id:
        return None, "missing id"

    entry = catalogue_map.get(law_id, {})
    url = payload.get("url") or entry.get("url") or f"https://zakon.rada.gov.ua/laws/show/{law_id}"
    print_url = f"{url}/print"

    response = fetch_with_retry(print_url) or fetch_with_retry(url)
    if response is None:
        return None, "fetch failed"

    response_text = response.text
    if pg_conn is not None:
        try:
            response_text = stage_raw_law_response(
                pg_conn,
                law_id=law_id,
                source_url=response.url or print_url,
                response_body=response_text,
                http_status=response.status_code,
                response_headers=dict(response.headers),
                source_kind="law_html",
            )
        except Exception as e:
            return None, f"raw stage failed: {e}"

    law = extract_law(response_text, law_id, url)
    if not law or law.get("section_count", 0) == 0:
        return None, "empty body"

    # Preserve catalogue enrichment (prefer catalogue, fall back to prior payload).
    law["category"] = entry.get("category", payload.get("category", ""))
    law["catalogue_date"] = entry.get("date", payload.get("catalogue_date", ""))

    # Only overwrite on a successful extraction, so a bad fetch never destroys data.
    path.write_text(json.dumps(law, ensure_ascii=False, indent=2), encoding="utf-8")
    if pg_conn is not None:
        stage_law_with_sections(pg_conn, law, source_catalogue_entry=entry or None)

    return law, "ok"


def main():
    parser = argparse.ArgumentParser(description="Re-extract suspect laws through Docling")
    parser.add_argument("--dry-run", action="store_true",
                        help="List suspect laws and exit without fetching")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N suspect laws (0 = all)")
    parser.add_argument("--embed", action="store_true",
                        help="Also re-embed each improved law into Qdrant now "
                             "(default: leave the full rebuild to 8_reembed_to_gemini.py)")
    parser.add_argument("--allow-no-docling", action="store_true",
                        help="Proceed even if DOCLING_API_URL is unset (re-extraction "
                             "would just repeat the HTML fallback — not recommended)")
    args = parser.parse_args()

    print("=== Step 9: Re-extract suspect laws through Docling ===\n")

    if not DOCLING_API_URL and not args.allow_no_docling:
        print("✗ DOCLING_API_URL is not set. Re-extracting suspect laws through the")
        print("  HTML fallback would just reproduce the collapse. Set DOCLING_API_URL,")
        print("  or pass --allow-no-docling to override.")
        return

    laws_dir = Path(LAWS_DIR)
    suspects = _find_suspect_laws(laws_dir)
    print(f"Suspect laws found: {len(suspects)}")
    if not suspects:
        print("✓ Nothing to re-extract.")
        return

    if args.limit:
        suspects = suspects[: args.limit]
        print(f"Limited to first {len(suspects)}")

    if args.dry_run:
        print("\n--- Suspect laws (dry run) ---")
        for path, payload, quality in suspects:
            print(f"  {payload.get('id', path.stem):<18} "
                  f"{quality.get('section_count')} sections, "
                  f"{quality.get('total_chars')} chars")
        return

    catalogue_map = _load_catalogue_map()

    pg_conn = None
    if DATABASE_URL:
        try:
            pg_conn = get_postgres_connection()
            ensure_staging_schema(pg_conn)
            print("Postgres staging: enabled")
        except Exception as e:
            print(f"Postgres staging: disabled ({e})")
            pg_conn = None

    qdrant = None
    if args.embed:
        qdrant = get_qdrant_client()
        setup_qdrant(qdrant)
        print("Embedding: enabled (re-upserting improved laws to Qdrant)")

    improved = 0
    still_suspect = 0
    failed: list[tuple[str, str]] = []
    after_assessments: list[dict] = []

    for path, payload, before in suspects:
        law_id = str(payload.get("id", path.stem)).strip()
        law, message = _reextract(path, payload, catalogue_map, pg_conn)

        if law is None:
            failed.append((law_id, message))
            print(f"  ✗ {law_id}: {message}")
            time.sleep(REQUEST_DELAY)
            continue

        after = law.get("quality", {})
        after_assessments.append(after)
        before_n = before.get("section_count")
        after_n = after.get("section_count")

        if after.get("quality") == "suspect":
            still_suspect += 1
            print(f"  ~ {law_id}: still suspect ({before_n} → {after_n} sections)")
        else:
            improved += 1
            print(f"  ✓ {law_id}: improved ({before_n} → {after_n} sections, "
                  f"mode={law.get('extraction_mode')})")

        if args.embed and qdrant is not None:
            chunks = law_to_chunks(law)
            if chunks:
                delete_law_from_qdrant(qdrant, law_id)
                embeddings = embed_chunks(chunks)
                upsert_to_qdrant(qdrant, chunks, embeddings)
                upsert_indexed_law(law, len(chunks))
                if pg_conn is not None:
                    stage_chunks_for_law(pg_conn, law, chunks, mark_qdrant_synced=True)

        time.sleep(REQUEST_DELAY)

    if pg_conn is not None:
        pg_conn.close()

    print("\n=== Re-extract done ===")
    print(f"  Suspect laws processed: {len(suspects)}")
    print(f"  Improved (no longer suspect): {improved}")
    print(f"  Still suspect: {still_suspect}")
    print(f"  Failed: {len(failed)}")
    for law_id, msg in failed:
        print(f"    - {law_id}: {msg}")

    print("\n=== Extraction quality (after re-extract) ===")
    print(summarize_quality(after_assessments))

    if not args.embed:
        print("\nNext: rebuild the collection so chunking is consistent:")
        print("  python 8_reembed_to_gemini.py --recreate --skip-suspect")


if __name__ == "__main__":
    main()
