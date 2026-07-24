"""
Step 4: Incremental update — fetch and embed newly enacted laws since last run

Designed to run daily via n8n cron or a standalone cron job.

Fetching goes through the SAME robust catalogue source as the bootstrap
(``catalogue_source.load_catalogue_entries``): open-data feed -> doc.txt full
feed -> minimal seed. The previous implementation had its own parser that
assumed ``zak.json`` returns a flat list of law records — it does not, so the
incremental path silently ingested nothing on every run.

Watermark discipline (``data/state.json``):
  - ``last_run`` advances to today ONLY after a genuinely successful fetch that
    processed every candidate.
  - On a run bounded by ``INCREMENTAL_MAX_LAWS``, the watermark advances only to
    the newest enactment date actually processed, so the remainder is picked up
    next run (no silent gap).
  - If every live source failed (source == "seed"), the watermark does NOT move.

State tracked in data/state.json: { "last_run": "2024-01-15", ... }
"""

import json
import time
from datetime import datetime, date

from config import (
    DATABASE_URL, LAWS_DIR, STATE_PATH, QDRANT_COLLECTION,
    REQUEST_DELAY, env_int,
)
from qdrant_client import QdrantClient

from catalogue_source import load_catalogue_entries
from embedding_pipeline import (
    delete_law_from_qdrant,
    embed_chunks,
    law_to_chunks,
    setup_qdrant,
    upsert_to_qdrant,
)
from indexed_laws_tracker import upsert_indexed_law
from law_processing import (
    extract_law,
    fetch_with_retry,
    safe_filename,
    summarize_quality,
    warn_if_docling_disabled,
)
from service_clients import get_qdrant_client
from staging_db import ensure_staging_schema, get_postgres_connection, stage_law_with_sections
from staging_db import stage_raw_law_response


# Upper bound on laws processed per run. A first run (last_run defaults far in
# the past) would otherwise try to ingest the entire back-catalogue in one go.
INCREMENTAL_MAX_LAWS = env_int("INCREMENTAL_MAX_LAWS", 500)


def load_state() -> dict:
    """Load run state. Returns default if no state file exists."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_run": "2020-01-01", "total_laws_processed": 0}


def save_state(state: dict):
    """Persist run state."""
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def fetch_updated_ids(since_date: str) -> tuple[list[dict] | None, str]:
    """Return laws enacted on/after ``since_date`` plus the catalogue source.

    Returns ``(None, source)`` when the catalogue could not be fetched from any
    live source (``source == "seed"``) — the caller must treat this as a failed
    run and leave the watermark untouched. Otherwise returns entries sorted
    oldest-first so a bounded run advances the watermark monotonically.
    """
    entries, source = load_catalogue_entries()
    if source == "seed":
        return None, source

    updated = [
        e for e in entries
        if e.get("date") and e["date"][:10] >= since_date
    ]
    updated.sort(key=lambda e: e.get("date", ""))
    return updated, source


def process_law(entry: dict, client: QdrantClient, pg_conn=None,
                assessments: list | None = None) -> bool:
    """Scrape, embed, and upsert a single law. Returns True on success."""
    law_id = entry["id"]
    url = f"https://zakon.rada.gov.ua/laws/show/{law_id}"

    response = fetch_with_retry(url)
    if response is None:
        print(f"  ✗ Failed to fetch: {law_id}")
        return False

    response_text = response.text
    if pg_conn is not None:
        try:
            response_text = stage_raw_law_response(
                pg_conn,
                law_id=law_id,
                source_url=response.url or url,
                response_body=response_text,
                http_status=response.status_code,
                response_headers=dict(response.headers),
                source_kind="law_html",
            )
        except Exception as e:
            print(f"  ✗ Raw stage failed: {law_id} ({e})")
            return False

    law = extract_law(response_text, law_id, url)
    if not law or law.get("section_count", 0) == 0:
        print(f"  ✗ Empty body: {law_id}")
        return False

    law["category"] = entry.get("category", "")
    law["catalogue_date"] = entry.get("date", "")
    if assessments is not None:
        assessments.append(law.get("quality", {}))

    # Save to disk (overwrite if exists)
    out_path = LAWS_DIR / safe_filename(law_id)
    out_path.write_text(
        json.dumps(law, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    if pg_conn is not None:
        stage_law_with_sections(pg_conn, law, source_catalogue_entry=entry)

    # Remove old vectors for this law, then re-insert
    delete_law_from_qdrant(client, law_id)

    chunks = law_to_chunks(law)
    if chunks:
        embeddings = embed_chunks(chunks)
        upsert_to_qdrant(client, chunks, embeddings)
        upsert_indexed_law(law, len(chunks))

    return True


def main():
    print("=== Step 4: Incremental update ===\n")
    print(f"Run time: {datetime.now().isoformat()}\n")

    warn_if_docling_disabled()

    state = load_state()
    last_run = state["last_run"]
    print(f"Last run: {last_run}")

    # Fetch new/updated law IDs through the shared catalogue source.
    print(f"Fetching laws enacted since {last_run}...")
    updated_entries, source = fetch_updated_ids(last_run)

    if updated_entries is None:
        print(
            "✗ Catalogue fetch failed — every live source was unavailable "
            f"(source={source}). Leaving watermark at {last_run} so no window "
            "is skipped. Will retry next run."
        )
        return

    print(f"Catalogue source: {source}")
    print(f"Found {len(updated_entries)} candidate laws")

    # Bound the run so a first/backfill run does not ingest the whole corpus.
    truncated = False
    if len(updated_entries) > INCREMENTAL_MAX_LAWS:
        print(
            f"⚠ Capping to {INCREMENTAL_MAX_LAWS} laws this run (oldest first); "
            f"{len(updated_entries) - INCREMENTAL_MAX_LAWS} remaining will be "
            "picked up on the next run."
        )
        updated_entries = updated_entries[:INCREMENTAL_MAX_LAWS]
        truncated = True

    if not updated_entries:
        print("✓ Nothing to update.")
        state["last_run"] = date.today().isoformat()
        save_state(state)
        return

    # Setup
    client = get_qdrant_client()
    setup_qdrant(client)

    pg_conn = None
    if DATABASE_URL:
        try:
            pg_conn = get_postgres_connection()
            ensure_staging_schema(pg_conn)
            print("Postgres staging: enabled")
        except Exception as e:
            print(f"Postgres staging: disabled ({e})")
            pg_conn = None
    else:
        print("Postgres staging: disabled (DATABASE_URL not set)")

    # Process each updated law
    success = 0
    failed = 0
    assessments: list = []
    processed_dates: list[str] = []

    for i, entry in enumerate(updated_entries):
        print(f"[{i+1}/{len(updated_entries)}] {entry['id']}: {entry['title'][:60]}")
        ok = process_law(entry, client, pg_conn=pg_conn, assessments=assessments)
        if ok:
            success += 1
            if entry.get("date"):
                processed_dates.append(entry["date"][:10])
        else:
            failed += 1
        time.sleep(REQUEST_DELAY)

    # Advance the watermark carefully.
    if truncated:
        # Only advance to the newest enactment date we actually processed, so
        # the remaining (newer) laws are still in-window next run.
        frontier = max(processed_dates) if processed_dates else last_run
        state["last_run"] = frontier
        print(f"\nBounded run: watermark advanced to processed frontier {frontier}.")
    else:
        # Whole candidate set handled — safe to advance to today.
        state["last_run"] = date.today().isoformat()

    state["total_laws_processed"] = state.get("total_laws_processed", 0) + success
    save_state(state)

    # Report
    collection_info = client.get_collection(QDRANT_COLLECTION)
    print(f"\n=== Done ===")
    print(f"  Updated: {success}")
    print(f"  Failed:  {failed}")
    print(f"  Qdrant total vectors: {collection_info.points_count}")
    print(f"  Next run will check from: {state['last_run']}")

    print("\n=== Extraction quality ===")
    print(summarize_quality(assessments))

    if pg_conn is not None:
        pg_conn.close()


if __name__ == "__main__":
    main()
