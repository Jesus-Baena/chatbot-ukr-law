"""
Step 11: Build the ``rada_plus_reports`` *serving* collection.

Why this exists
---------------
The live paralegal chatflow (Flowise id ``6b3a8804-…``) is a **classic**
ConversationalRetrievalQAChain, which binds exactly **one** vector-store
retriever. This Flowise build has no retriever-merger node (LOTR / Merger
Retriever is not installed; RRF is single-source query-expansion, and the
Multi-Retrieval QA chain *routes* to one retriever instead of merging). So a
single flow cannot query ``rada_legislation`` and ``secondary_reports`` at the
same time.

To serve law + commentary from one retriever without re-embedding, we build a
combined serving collection = a vector copy of the primary-law collection plus
the report chunks. The canonical collections are untouched:
``rada_legislation`` (maintained by the n8n incremental job) and
``secondary_reports`` (built by ``10_ingest_reports.py``) remain the sources of
truth; this collection is a **derived, rebuildable** serving index.

Report chunks copied here get a ``[SECONDARY ANALYSIS — "<title>", commentary
dated <pub_date>; not primary law]`` prefix on their ``text`` payload (the
field Flowise stuffs into ``{context}``) so the LLM can tell dated commentary
from statute. Only the display text is annotated — the stored vector is the
one produced from the original text, so retrieval ranking is unchanged.

Re-sync
-------
``rada_legislation`` drifts as new laws are indexed. Re-run this script to
rebuild the serving copy (it recreates the target, so it is idempotent). A
future migration to an Agentflow with three independent retriever tools (one
per collection, each with its own small top-K) would remove the need for this
duplicate — see ``flowise/README.md``.

Usage:
  python 11_build_serving_collection.py            # rebuild rada_plus_reports
  python 11_build_serving_collection.py --dry-run  # show counts only
"""

import argparse
import time
from urllib.parse import urlparse

from qdrant_client import QdrantClient, models

from config import QDRANT_API_KEY, QDRANT_COLLECTION, QDRANT_URL, REPORTS_COLLECTION

SERVING_COLLECTION = "rada_plus_reports"
ANALYSIS_MARKER = "[SECONDARY ANALYSIS"


def _client() -> QdrantClient:
    parsed = urlparse(QDRANT_URL)
    return QdrantClient(
        host=parsed.hostname,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        https=parsed.scheme == "https",
        api_key=QDRANT_API_KEY or None,
        check_compatibility=False,
        timeout=300,
    )


def _retry(fn, *, tries=5, wait=5, what="op"):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - network transient
            if attempt + 1 >= tries:
                raise
            print(f"  {what} retry {attempt + 1}/{tries}: {exc.__class__.__name__}")
            time.sleep(wait)


def _copy(client: QdrantClient, src: str, dst: str, *, mark_reports: bool) -> int:
    total, offset = 0, None
    while True:
        recs, offset = _retry(
            lambda: client.scroll(collection_name=src, limit=64, offset=offset,
                                  with_payload=True, with_vectors=True),
            what=f"scroll {src}",
        )
        if not recs:
            break
        points = []
        for r in recs:
            payload = dict(r.payload or {})
            if mark_reports and payload.get("source_type") == "secondary_report":
                text = payload.get("text", "")
                if not text.startswith(ANALYSIS_MARKER):
                    title = payload.get("report_title") or "Untitled"
                    pub = payload.get("pub_date") or "undated"
                    payload["text"] = (
                        f'[SECONDARY ANALYSIS — "{title}", commentary dated {pub}; '
                        f"not primary law]\n{text}"
                    )
            points.append(models.PointStruct(id=r.id, vector=r.vector, payload=payload))
        _retry(lambda: client.upsert(collection_name=dst, points=points, wait=False),
               what=f"upsert {dst}")
        total += len(recs)
        if total % 1280 == 0:
            print(f"  {src}: {total}", flush=True)
        if offset is None:
            break
    print(f"  {src}: {total} copied")
    return total


def main():
    parser = argparse.ArgumentParser(description="Build the rada_plus_reports serving collection")
    parser.add_argument("--dry-run", action="store_true", help="Show source counts, do not write")
    args = parser.parse_args()

    client = _client()
    law_n = client.get_collection(QDRANT_COLLECTION).points_count
    rep_n = client.get_collection(REPORTS_COLLECTION).points_count
    print(f"Sources: {QDRANT_COLLECTION}={law_n}  {REPORTS_COLLECTION}={rep_n}")
    print(f"Target : {SERVING_COLLECTION}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return

    vectors_config = client.get_collection(QDRANT_COLLECTION).config.params.vectors
    if client.collection_exists(SERVING_COLLECTION):
        client.delete_collection(SERVING_COLLECTION)
    client.create_collection(collection_name=SERVING_COLLECTION, vectors_config=vectors_config)
    for field in ("law_id", "category", "source_type", "enacted_date"):
        try:
            client.create_payload_index(
                collection_name=SERVING_COLLECTION, field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception:  # noqa: BLE001 - index may already exist
            pass

    copied_law = _copy(client, QDRANT_COLLECTION, SERVING_COLLECTION, mark_reports=False)
    copied_rep = _copy(client, REPORTS_COLLECTION, SERVING_COLLECTION, mark_reports=True)
    time.sleep(2)
    final = client.get_collection(SERVING_COLLECTION).points_count
    print(f"\nDONE {SERVING_COLLECTION}: {final} points (law={copied_law} + reports={copied_rep})")


if __name__ == "__main__":
    main()
