"""
Step 10: Ingest secondary reports into the `secondary_reports` collection.

Secondary reports are expert analyses that *review* specific laws/topics (e.g.
"Ukraine Data Protection vs. GDPR"). They are stored in their own Qdrant
collection and tagged ``source_type="secondary_report"`` so the serving layer
can present them as clearly-labelled commentary — never as primary law.

Each report is extracted (Docling-first, PyMuPDF/HTML fallback), chunked with
the same config as the rest of the corpus, and enriched with linkage metadata:
which laws it reviews (auto-detected from cited numbers), its publication date,
topics, and title. An optional metadata sheet overrides/augments the
auto-detected values.

Input layout (point --reports-path at the folder of report files):
  <reports-path>/*.pdf | *.html | *.docx
  <reports-path>/reports_metadata.csv        (optional; keyed by filename)

Metadata CSV columns (all optional): filename, title, authoring_org,
source_url, pub_date, topics, reviews_law_ids. Topics / reviews_law_ids accept a
comma- or semicolon-separated list.

Usage:
  python 10_ingest_reports.py --reports-path "../2025-ukraine-law-knowledgebase/raw data/curated"
  python 10_ingest_reports.py --reports-path ./reports --dry-run
"""

import argparse
import csv
import os
import re
from pathlib import Path

from qdrant_client.models import PayloadSchemaType

from config import REPORTS_COLLECTION
from date_utils import normalize_date
from embedding_pipeline import embed_chunks, law_to_chunks, setup_qdrant, upsert_to_qdrant
from report_processing import extract_report
from service_clients import get_qdrant_client

REPORT_FILE_GLOBS = ("*.pdf", "*.html", "*.htm", "*.docx")
METADATA_CSV = "reports_metadata.csv"

# Payload indexes specific to the reports collection.
REPORT_INDEXES = {
    "source_type": PayloadSchemaType.KEYWORD,
    "reviews_law_refs": PayloadSchemaType.KEYWORD,
    "topics": PayloadSchemaType.KEYWORD,
}


def _slug(text: str, max_len: int = 70) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len] or "report"


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", value or "") if part.strip()]


def _load_metadata(reports_path: Path) -> dict[str, dict]:
    """Load the optional per-report metadata sheet keyed by filename."""
    csv_path = reports_path / METADATA_CSV
    if not csv_path.exists():
        return {}
    rows: dict[str, dict] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            filename = (row.get("filename") or "").strip()
            if not filename:
                continue
            rows[filename] = {
                "title": (row.get("title") or "").strip(),
                "authoring_org": (row.get("authoring_org") or "").strip(),
                "source_url": (row.get("source_url") or "").strip(),
                "pub_date": normalize_date(row.get("pub_date")),
                "topics": _split_list(row.get("topics")),
                "reviews_law_ids": _split_list(row.get("reviews_law_ids")),
            }
    return rows


def _enrich(chunks: list[dict], *, report_id, title, pub_date, reviews, topics,
            authoring_org, summary) -> list[dict]:
    for chunk in chunks:
        chunk["source_type"] = "secondary_report"
        chunk["category"] = "secondary_report"
        chunk["report_title"] = title
        chunk["reviews_law_refs"] = reviews
        chunk["topics"] = topics
        chunk["pub_date"] = pub_date
        chunk["authoring_org"] = authoring_org
        chunk["summary"] = summary
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Ingest secondary reports into Qdrant")
    parser.add_argument("--reports-path", default=os.getenv("REPORTS_PATH", ""),
                        help="Directory containing report files (pdf/html/docx)")
    parser.add_argument("--limit", type=int, default=0, help="Only process N reports (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and print detected metadata without embedding")
    args = parser.parse_args()

    if not args.reports_path:
        print("✗ Set --reports-path (or REPORTS_PATH) to the reports directory.")
        return

    reports_path = Path(args.reports_path)
    print("=== Step 10: Ingesting secondary reports ===")
    print(f"  Reports path: {reports_path}")
    print(f"  Target collection: {REPORTS_COLLECTION}\n")

    if not reports_path.exists():
        print(f"✗ Reports directory not found: {reports_path}")
        return

    files = sorted({p for glob in REPORT_FILE_GLOBS for p in reports_path.glob(glob)})
    if not files:
        print("✗ No report files (pdf/html/docx) found.")
        return
    if args.limit:
        files = files[: args.limit]

    metadata = _load_metadata(reports_path)
    print(f"Report files: {len(files)}  |  metadata rows: {len(metadata)}\n")

    client = None
    if not args.dry_run:
        client = get_qdrant_client()
        setup_qdrant(client, collection_name=REPORTS_COLLECTION, extra_payload_indexes=REPORT_INDEXES)

    total_chunks = 0
    ingested = 0
    for path in files:
        report = extract_report(path)
        if not report or not report.get("sections"):
            print(f"  ⚠ could not extract: {path.name}")
            continue

        meta = metadata.get(path.name, {})
        report_id = _slug(path.stem)
        title = meta.get("title") or report["title"] or path.stem
        pub_date = meta.get("pub_date") or report["pub_date"]
        reviews = meta.get("reviews_law_ids") or report["reviews_law_refs"]
        topics = meta.get("topics") or []
        authoring_org = meta.get("authoring_org", "")
        source_url = meta.get("source_url", "")

        print(f"  • {path.name}")
        print(f"      title: {title[:70]}")
        print(f"      reviews_law_refs: {reviews}")
        print(f"      pub_date: {pub_date or '(unknown)'}  sections: {report['section_count']}"
              f"  chars: {report['total_chars']}  mode: {report['extraction_mode']}")

        if args.dry_run:
            continue

        law_like = {
            "id": report_id,
            "title": title,
            "url": source_url,
            "category": "secondary_report",
            "enacted_date": pub_date,  # -> enacted_date_int for unified date filtering
            "sections": report["sections"],
            "section_count": report["section_count"],
        }
        chunks = _enrich(
            law_to_chunks(law_like),
            report_id=report_id, title=title, pub_date=pub_date,
            reviews=reviews, topics=topics, authoring_org=authoring_org,
            summary=report.get("summary", ""),
        )
        if not chunks:
            print("      (no chunks)")
            continue

        embeddings = embed_chunks(chunks)
        upsert_to_qdrant(client, chunks, embeddings, collection_name=REPORTS_COLLECTION)
        total_chunks += len(chunks)
        ingested += 1
        print(f"      ✓ {len(chunks)} chunks")

    if args.dry_run:
        print("\n(dry run — nothing embedded)")
        return

    info = client.get_collection(REPORTS_COLLECTION)
    print("\n=== Done ===")
    print(f"  Reports ingested: {ingested}")
    print(f"  Chunks upserted this run: {total_chunks}")
    print(f"  Collection size: {info.points_count} vectors")


if __name__ == "__main__":
    main()
