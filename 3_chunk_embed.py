"""
Step 3: Chunk law texts, generate embeddings, upsert to Qdrant

Reads data/laws/*.json, chunks each section, embeds with
Gemini gemini-embedding-001, and upserts to Qdrant.

Cross-lingual queries (English → Ukrainian docs) work out of the box.
"""

import json
from tqdm import tqdm
from config import LAWS_DIR, QDRANT_COLLECTION, QDRANT_URL
from embedding_pipeline import (
    embed_chunks,
    get_processed_ids,
    law_to_chunks,
    setup_qdrant,
    upsert_to_qdrant,
)
from indexed_laws_tracker import upsert_indexed_law
from service_clients import get_qdrant_client


def main():
    print("=== Step 3: Chunking, embedding, upserting to Qdrant ===\n")

    # Load all valid law files
    law_files = [
        f for f in LAWS_DIR.glob("*.json")
        if not json.loads(f.read_text(encoding="utf-8")).get("error")
    ]
    print(f"Valid law files: {len(law_files)}")

    if not law_files:
        print("✗ No valid law files found. Run 2_scrape_laws.py first.")
        return

    # Setup Qdrant
    client = get_qdrant_client()
    setup_qdrant(client)

    # Check what's already processed (resumability)
    try:
        already_done = get_processed_ids(client)
        print(f"Already in Qdrant: {len(already_done)} laws")
    except Exception:
        already_done = set()

    todo_files = [
        f for f in law_files
        if json.loads(f.read_text(encoding="utf-8")).get("id") not in already_done
    ]
    print(f"To process: {len(todo_files)}\n")

    if not todo_files:
        print("✓ All laws already embedded.")
        return

    # Process one law at a time so each is committed to Qdrant before the next
    total_chunks = 0

    for f in tqdm(todo_files, desc="Laws"):
        law = json.loads(f.read_text(encoding="utf-8"))
        chunks = law_to_chunks(law)
        if not chunks:
            print(f"  skip {law['id']} — no chunks", flush=True)
            continue

        print(f"\n  {law['id']} — {len(chunks)} chunks …", flush=True)
        embeddings = embed_chunks(chunks)
        upsert_to_qdrant(client, chunks, embeddings)
        upsert_indexed_law(law, len(chunks))
        total_chunks += len(chunks)
        print(f"  ✓ upserted {len(chunks)} chunks for {law['id']}", flush=True)

    # Final stats
    collection_info = client.get_collection(QDRANT_COLLECTION)
    print(f"\n=== Done ===")
    print(f"  Total chunks upserted: {total_chunks}")
    print(f"  Qdrant collection size: {collection_info.points_count} vectors")
    print(f"  Collection: {QDRANT_COLLECTION} @ {QDRANT_URL}")


if __name__ == "__main__":
    main()
