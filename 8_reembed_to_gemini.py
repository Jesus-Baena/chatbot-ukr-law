"""
Step 8: Re-embed the Rada corpus with Gemini (migration backfill)

Rebuilds the `rada_legislation` Qdrant collection using Gemini
`gemini-embedding-001` embeddings, sourcing the section text straight from the
Postgres staging layer (`rada_staging_sections`) — so NO re-scraping and no
Docling re-run are needed.

Because Gemini embeddings have a different dimensionality than the old
mxbai-embed-large vectors (1024-d), the target collection must be created fresh
at the new EMBED_DIM. Use --recreate to drop an existing collection first, or
--collection to build into a versioned name (e.g. rada_legislation_g1) for a
zero-downtime switch-over.

Usage:
  python 8_reembed_to_gemini.py                       # rebuild into QDRANT_COLLECTION
  python 8_reembed_to_gemini.py --recreate            # drop + recreate first
  python 8_reembed_to_gemini.py --collection rada_legislation_g1
  python 8_reembed_to_gemini.py --limit 5             # smoke test on 5 laws
"""

import argparse

from tqdm import tqdm

from config import QDRANT_COLLECTION, CHUNK_SIZE, EMBED_DIM, EMBED_MODEL
from embedding_pipeline import (
    embed_chunks,
    get_processed_ids,
    law_to_chunks,
    setup_qdrant,
    upsert_to_qdrant,
)
from law_processing import assess_law_quality
from service_clients import get_qdrant_client
from staging_db import get_postgres_connection


def _load_law_ids(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT law_id FROM rada_staging_laws ORDER BY law_id")
        return [row[0] for row in cur.fetchall()]


def _load_law(conn, law_id: str) -> dict:
    """Reconstruct a law dict (id/title/url/.../sections) from staging tables."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, url, category, enacted_date, catalogue_date
            FROM rada_staging_laws WHERE law_id = %s
            """,
            (law_id,),
        )
        title, url, category, enacted_date, catalogue_date = cur.fetchone()

        cur.execute(
            """
            SELECT heading, text FROM rada_staging_sections
            WHERE law_id = %s ORDER BY section_index
            """,
            (law_id,),
        )
        sections = [{"heading": heading or "", "text": text or ""} for heading, text in cur.fetchall()]

    return {
        "id": law_id,
        "title": title or "",
        "url": url or "",
        "category": category or "",
        "enacted_date": enacted_date or "",
        "catalogue_date": catalogue_date or "",
        "sections": sections,
        "section_count": len(sections),
    }


def main():
    parser = argparse.ArgumentParser(description="Re-embed the Rada corpus with Gemini")
    parser.add_argument("--collection", default=QDRANT_COLLECTION,
                        help="Target Qdrant collection (default: QDRANT_COLLECTION)")
    parser.add_argument("--recreate", action="store_true",
                        help="Drop the target collection before rebuilding")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N laws (0 = all) — useful for smoke tests")
    parser.add_argument("--skip-suspect", action="store_true",
                        help="Hold back structurally-collapsed ('suspect') laws from the rebuild")
    args = parser.parse_args()

    collection = args.collection
    print("=== Step 8: Re-embedding Rada corpus with Gemini ===")
    print(f"  Model: {EMBED_MODEL} @ {EMBED_DIM}-d")
    print(f"  Chunk size: {CHUNK_SIZE} chars")
    print(f"  Target collection: {collection}")
    if args.skip_suspect:
        print("  Skip-suspect: enabled (collapsed extractions held back)")
    print()

    client = get_qdrant_client()

    existing = [c.name for c in client.get_collections().collections]
    if args.recreate and collection in existing:
        print(f"Dropping existing collection '{collection}' …")
        client.delete_collection(collection)

    setup_qdrant(client, collection_name=collection)

    conn = get_postgres_connection()
    try:
        law_ids = _load_law_ids(conn)
        if args.limit:
            law_ids = law_ids[: args.limit]
        print(f"Laws in staging: {len(law_ids)}")

        try:
            already_done = get_processed_ids(client, collection_name=collection)
            print(f"Already in '{collection}': {len(already_done)} laws")
        except Exception:
            already_done = set()

        todo = [law_id for law_id in law_ids if law_id not in already_done]
        print(f"To re-embed: {len(todo)}\n")

        total_chunks = 0
        skipped_suspect = 0
        for law_id in tqdm(todo, desc="Laws"):
            law = _load_law(conn, law_id)
            if args.skip_suspect and assess_law_quality(law).get("quality") == "suspect":
                skipped_suspect += 1
                continue
            chunks = law_to_chunks(law)
            if not chunks:
                continue
            embeddings = embed_chunks(chunks)
            upsert_to_qdrant(client, chunks, embeddings, collection_name=collection)
            total_chunks += len(chunks)
    finally:
        conn.close()

    info = client.get_collection(collection)
    print("\n=== Done ===")
    print(f"  Chunks upserted this run: {total_chunks}")
    if args.skip_suspect:
        print(f"  Suspect laws held back:   {skipped_suspect}")
    print(f"  Collection size: {info.points_count} vectors @ {EMBED_DIM}-d")


if __name__ == "__main__":
    main()
