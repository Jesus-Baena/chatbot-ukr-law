"""
Retry failed law ingests and immediately vectorize recovered laws.

This script only targets `data/laws/*.json` files that contain an `error` marker.
For each recovered law it will:
1) overwrite the error artifact with full law JSON,
2) stage the law in Postgres (if configured),
3) embed and upsert to Qdrant,
4) update INDEXED_LAWS.md tracker.
"""

import json
import time
from pathlib import Path

from config import DATABASE_URL, LAWS_DIR, REQUEST_DELAY
from embedding_pipeline import embed_chunks, law_to_chunks, setup_qdrant, upsert_to_qdrant
from indexed_laws_tracker import upsert_indexed_law
from law_processing import extract_law, fetch_with_retry, summarize_quality, warn_if_docling_disabled
from service_clients import get_qdrant_client
from staging_db import ensure_staging_schema, get_postgres_connection, stage_chunks_for_law, stage_law_with_sections
from staging_db import stage_raw_law_response


def _load_catalogue_map() -> dict[str, dict]:
    path = Path("data/catalogue.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result = {}
    for row in data:
        law_id = str(row.get("id", "")).strip()
        if law_id:
            result[law_id] = row
    return result


def _find_error_files() -> list[Path]:
    files = []
    for file_path in sorted(LAWS_DIR.glob("*.json")):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("error"):
            files.append(file_path)
    return files


def _recover_law(error_file: Path, catalogue_map: dict[str, dict], pg_conn) -> tuple[bool, str, dict | None]:
    payload = json.loads(error_file.read_text(encoding="utf-8"))
    law_id = str(payload.get("id", "")).strip()
    if not law_id:
        return False, "missing law id", None

    entry = catalogue_map.get(law_id, {})
    url = payload.get("url") or entry.get("url") or f"https://zakon.rada.gov.ua/laws/show/{law_id}"
    print_url = f"{url}/print"

    response = fetch_with_retry(print_url) or fetch_with_retry(url)
    if response is None:
        return False, "fetch failed", None

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
            return False, f"raw stage failed: {e}", None

    law = extract_law(response_text, law_id, url)
    if not law or law.get("section_count", 0) == 0:
        return False, "empty body", None

    law["category"] = entry.get("category", "")
    law["catalogue_date"] = entry.get("date", "")

    error_file.write_text(json.dumps(law, ensure_ascii=False, indent=2), encoding="utf-8")

    if pg_conn is not None:
        stage_law_with_sections(pg_conn, law, source_catalogue_entry=entry)

    return True, "ok", law


def main():
    print("=== Step 6: Retry failed ingests ===")

    warn_if_docling_disabled()

    catalogue_map = _load_catalogue_map()
    error_files = _find_error_files()
    print(f"Error files found: {len(error_files)}")

    if not error_files:
        print("✓ No failed laws to retry.")
        return

    pg_conn = None
    if DATABASE_URL:
        try:
            pg_conn = get_postgres_connection()
            ensure_staging_schema(pg_conn)
            print("Postgres staging: enabled")
        except Exception as e:
            print(f"Postgres staging: disabled ({e})")
            pg_conn = None

    qdrant = get_qdrant_client()
    setup_qdrant(qdrant)

    recovered_laws = []
    failed = []

    for error_file in error_files:
        ok, message, law = _recover_law(error_file, catalogue_map, pg_conn)
        law_id = error_file.stem
        if ok and law is not None:
            recovered_laws.append(law)
            print(f"  ✓ recovered {law['id']}")
        else:
            failed.append((law_id, message))
            print(f"  ✗ still failed {law_id}: {message}")
        time.sleep(REQUEST_DELAY)

    embedded = 0
    for law in recovered_laws:
        chunks = law_to_chunks(law)
        if not chunks:
            print(f"  skip embed {law['id']} — no chunks")
            continue
        embeddings = embed_chunks(chunks)
        upsert_to_qdrant(qdrant, chunks, embeddings)
        upsert_indexed_law(law, len(chunks))
        if pg_conn is not None:
            stage_chunks_for_law(pg_conn, law, chunks, mark_qdrant_synced=True)
        embedded += 1
        print(f"  ✓ embedded {law['id']} ({len(chunks)} chunks)")

    if pg_conn is not None:
        pg_conn.close()

    print("\n=== Retry done ===")
    print(f"  Recovered: {len(recovered_laws)}")
    print(f"  Embedded:  {embedded}")
    print(f"  Still fail:{len(failed)}")
    if failed:
        for law_id, msg in failed:
            print(f"    - {law_id}: {msg}")

    print("\n=== Extraction quality (recovered laws) ===")
    print(summarize_quality([law.get("quality", {}) for law in recovered_laws]))


if __name__ == "__main__":
    main()
