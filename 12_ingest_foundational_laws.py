"""
Step 12: Ingest foundational framework laws into the curated collection.

The auto-scraped Rada corpus (`rada_legislation`) is ~99.7% recent (2025-26)
resolutions and amendments; the foundational framework laws NGOs actually need
("On Public Associations", codes, data-protection, mobilisation, etc.) were never
scraped. This script fetches a curated list of those laws directly from
zakon.rada.gov.ua, extracts them with the HTML parser (no Docling needed),
chunks + Gemini-embeds them, and upserts into CURATED_COLLECTION.

It mirrors the curated payload schema AND writes the nested `metadata` object
Flowise reads for citations (top-level upsert does not build it), with a clean
English title per law so citations are human-readable.

Usage:
  python 12_ingest_foundational_laws.py --dry-run   # fetch+extract, report only
  python 12_ingest_foundational_laws.py             # ingest
"""
import argparse
import sys
import time

import requests

from config import CURATED_COLLECTION, QDRANT_COLLECTION, LAW_BASE_URL, REQUEST_TIMEOUT
from embedding_pipeline import embed_chunks, law_to_chunks, setup_qdrant, upsert_to_qdrant, get_processed_ids
from law_processing import extract_law_from_html
from service_clients import get_qdrant_client

UA = {"User-Agent": "Mozilla/5.0 (compatible; baena-rag/1.0)"}

# Foundational laws by Rada id (zakon.rada.gov.ua/laws/show/{id}) + a clean EN title.
# Grouped by the priority topics the NGO audience asks about.
LAWS: list[tuple[str, str, str]] = [
    # topic, rada id, English title
    ("registration", "4572-17", "On Public Associations"),
    ("registration", "5073-17", "On Charitable Activity and Charitable Organizations"),
    ("registration", "3236-17", "On Volunteer Activity"),
    ("registration", "755-15", "On State Registration of Legal Entities, Individual Entrepreneurs and Public Formations"),
    ("humanitarian", "1192-14", "On Humanitarian Aid"),
    ("data-protection", "2297-17", "On Personal Data Protection"),
    ("data-protection", "2657-12", "On Information"),
    ("customs", "4495-17", "Customs Code of Ukraine"),
    ("tax-finance", "2473-19", "On Currency and Currency Operations"),
    ("tax-finance", "361-20", "On Prevention and Counteraction to Legalization (Laundering) of the Proceeds of Crime"),
    ("labour", "322-08", "Labour Code of Ukraine"),
    ("labour", "2136-20", "On the Organization of Labour Relations under Martial Law"),
    ("mobilisation", "3543-12", "On Mobilization Preparation and Mobilization"),
    ("mobilisation", "2232-12", "On Military Duty and Military Service"),
    ("health", "2168-19", "On State Financial Guarantees of Medical Care for the Population"),
    ("health", "2469-20", "On Medicinal Products"),
]

MIN_CHARS = 400  # skip pages that clearly didn't extract real legal text


def fetch(law_id: str) -> str | None:
    url = LAW_BASE_URL.format(law_id=law_id)
    try:
        r = requests.get(url, headers=UA, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200 or len(r.text) < 1000:
            print(f"  ! {law_id}: HTTP {r.status_code}, {len(r.text)} bytes", file=sys.stderr)
            return None
        return r.text
    except requests.RequestException as e:  # noqa: BLE001
        print(f"  ! {law_id}: fetch error {type(e).__name__}: {e}", file=sys.stderr)
        return None


def enrich(chunks: list[dict], law_id: str, title: str, url: str, topic: str) -> list[dict]:
    """Curated fields + the nested `metadata` object Flowise reads for citations."""
    for c in chunks:
        c["source"] = "curated_kb"
        c["category"] = "curated_kb"
        c["topics"] = [topic]
        c["title"] = title
        c["metadata"] = {
            "law_id": law_id,
            "title": title,
            "url": url,
            "enacted_date": c.get("enacted_date", ""),
            "category": "curated_kb",
            "source": "curated_kb",
        }
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch + extract + report, no embedding/upsert")
    args = ap.parse_args()

    client = get_qdrant_client()
    if not args.dry_run:
        setup_qdrant(client, collection_name=CURATED_COLLECTION)

    print("Checking existing coverage (curated + rada)...")
    existing = get_processed_ids(client, CURATED_COLLECTION) | get_processed_ids(client, QDRANT_COLLECTION)
    print(f"  {len(existing)} law_ids already present across both collections\n")

    total_chunks = 0
    ingested = skipped = failed = 0
    for topic, law_id, title in LAWS:
        if law_id in existing:
            print(f"  = {law_id} ({title[:40]}) already present — skip")
            skipped += 1
            continue
        html = fetch(law_id)
        if not html:
            failed += 1
            continue
        url = LAW_BASE_URL.format(law_id=law_id)
        law = extract_law_from_html(html, law_id, url)
        text_len = sum(len(s.get("text", "")) for s in (law.get("sections") if law else []) or [])
        if not law or not law.get("sections") or text_len < MIN_CHARS:
            print(f"  ! {law_id} ({title[:40]}): extracted only {text_len} chars — skip", file=sys.stderr)
            failed += 1
            continue
        law["title"] = title
        law["url"] = url
        chunks = enrich(law_to_chunks(law), law_id, title, url, topic)
        if not chunks:
            print(f"  ! {law_id}: no chunks after filtering — skip", file=sys.stderr)
            failed += 1
            continue
        if args.dry_run:
            print(f"  ✓ {law_id} [{topic}] {title[:45]} — {len(chunks)} chunks ({text_len:,} chars)")
        else:
            embeddings = embed_chunks(chunks)
            upsert_to_qdrant(client, chunks, embeddings, collection_name=CURATED_COLLECTION)
            print(f"  ✓ {law_id} [{topic}] {title[:45]} — {len(chunks)} chunks upserted")
        total_chunks += len(chunks)
        ingested += 1
        time.sleep(0.6)  # be polite to zakon.rada.gov.ua

    print(f"\n=== {'DRY-RUN ' if args.dry_run else ''}Summary ===")
    print(f"  ingested/would-ingest: {ingested} laws, {total_chunks} chunks")
    print(f"  skipped (already present): {skipped}")
    print(f"  failed (fetch/extract): {failed}")
    if not args.dry_run:
        info = client.get_collection(CURATED_COLLECTION)
        print(f"  {CURATED_COLLECTION} size now: {info.points_count} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
