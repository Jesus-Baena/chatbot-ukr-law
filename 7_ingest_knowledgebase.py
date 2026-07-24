"""
Step 7: Ingest the curated humanitarian knowledgebase into Qdrant

Source: the `2025-ukraine-law-knowledgebase` repo (004_Knowledge_Base):
  - Input/*.htm[l]                  full-text HTML of ~11 foundational codes
  - data/kb-legal-ukr - Sheet1.csv  86-row metadata index (UtilityScore, Topics,
                                    HumanitarianSpecific, Summary, …)

These hand-curated, humanitarian-scored laws are kept in a SEPARATE Qdrant
collection (CURATED_COLLECTION, default `curated_legislation`) so retrieval can
boost/filter on humanitarian relevance independently of the auto-scraped Rada
corpus. Both collections use the same Gemini embedding model / dimension.

Two kinds of points are written:
  - source="curated_kb"          chunks from a matched full-text HTML law
  - source="curated_kb_summary"  one summary point per CSV row lacking full text
                                 (so all 86 curated laws are at least discoverable)

Usage:
  python 7_ingest_knowledgebase.py
  python 7_ingest_knowledgebase.py --kb-path /path/to/004_Knowledge_Base
  KB_PATH=/path/to/004_Knowledge_Base python 7_ingest_knowledgebase.py
"""

import argparse
import ast
import csv
import os
import re
from pathlib import Path

from qdrant_client.models import PayloadSchemaType

from config import CURATED_COLLECTION
from date_utils import enacted_date_fields
from embedding_pipeline import embed_chunks, setup_qdrant, upsert_to_qdrant
from law_processing import extract_law_from_html
from service_clients import get_qdrant_client


DEFAULT_KB_PATH = Path(__file__).parent.parent / "2025-ukraine-law-knowledgebase" / "004_Knowledge_Base"
CSV_NAME = "kb-legal-ukr - Sheet1.csv"

# Qdrant payload indexes specific to the curated collection
CURATED_INDEXES = {
    "utility_score": PayloadSchemaType.INTEGER,
    "topics": PayloadSchemaType.KEYWORD,
    "humanitarian_specific": PayloadSchemaType.BOOL,
    "source": PayloadSchemaType.KEYWORD,
}

# Pull a law identifier token out of a filename or CSV identifier, e.g.
# "… № 5403-VI on October …" -> "5403-vi";  "Law No. 322-VIII" -> "322-viii".
_NUM_RE = re.compile(r"(?:№|No\.?)\s*([0-9][0-9A-Za-zА-Яа-я_\-\/]*)", re.UNICODE)


def _number_token(text: str) -> str | None:
    match = _NUM_RE.search(text or "")
    if not match:
        return None
    return match.group(1).strip().lower().rstrip(".,")


def _slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^0-9A-Za-zА-Яа-я]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len] or "kb-doc"


def _parse_bool(value: str) -> bool:
    return (value or "").strip().upper() == "TRUE"


def _parse_int(value: str) -> int:
    try:
        return int(float((value or "").strip()))
    except ValueError:
        return 0


def _parse_topics(value: str) -> list[str]:
    raw = (value or "").strip()
    if not raw or raw in ("[]", "''", '""'):
        return []
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, (list, tuple)):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (ValueError, SyntaxError):
        pass
    return [part.strip() for part in raw.strip("[]").split(",") if part.strip()]


def _load_metadata(kb_path: Path) -> list[dict]:
    csv_path = kb_path / "data" / CSV_NAME
    if not csv_path.exists():
        print(f"⚠ metadata CSV not found at {csv_path} — proceeding without metadata")
        return []
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "identifier": (row.get("Identifier") or "").strip(),
                    "title": (row.get("Title") or "").strip(),
                    "type": (row.get("Type") or "").strip(),
                    "date": (row.get("DateOfApproval") or "").strip(),
                    "currently_valid": _parse_bool(row.get("CurrentlyValid")),
                    "utility_score": _parse_int(row.get("UtilityScore")),
                    "humanitarian_specific": _parse_bool(row.get("HumanitarianSpecific")),
                    "topics": _parse_topics(row.get("Topics")),
                    "summary": (row.get("Summary") or "").strip(),
                    "token": _number_token(row.get("Identifier") or row.get("Title")),
                }
            )
    return rows


def _enrich(chunks: list[dict], meta: dict | None, source: str) -> list[dict]:
    """Attach curated metadata fields to each chunk payload."""
    for chunk in chunks:
        chunk["source"] = source
        chunk["category"] = "curated_kb"
        if meta:
            chunk["utility_score"] = meta.get("utility_score", 0)
            chunk["topics"] = meta.get("topics", [])
            chunk["humanitarian_specific"] = meta.get("humanitarian_specific", False)
            chunk["summary"] = meta.get("summary", "")
            chunk["doc_type"] = meta.get("type", "")
            chunk["currently_valid"] = meta.get("currently_valid", True)
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Ingest the curated humanitarian knowledgebase")
    parser.add_argument("--kb-path", default=os.getenv("KB_PATH", str(DEFAULT_KB_PATH)),
                        help="Path to the 004_Knowledge_Base directory")
    args = parser.parse_args()

    kb_path = Path(args.kb_path)
    input_dir = kb_path / "Input"
    print("=== Step 7: Ingesting curated knowledgebase ===")
    print(f"  KB path: {kb_path}")
    print(f"  Target collection: {CURATED_COLLECTION}\n")

    if not input_dir.exists():
        print(f"✗ Input directory not found: {input_dir}")
        return

    metadata = _load_metadata(kb_path)
    by_token = {row["token"]: row for row in metadata if row["token"]}

    client = get_qdrant_client()
    setup_qdrant(client, collection_name=CURATED_COLLECTION, extra_payload_indexes=CURATED_INDEXES)

    matched_tokens: set[str] = set()
    total_chunks = 0

    # --- Full-text HTML laws ---
    html_files = sorted(list(input_dir.glob("*.htm")) + list(input_dir.glob("*.html")))
    print(f"Full-text HTML laws: {len(html_files)}")
    for html_file in html_files:
        token = _number_token(html_file.name)
        law_id = token or _slug(html_file.stem)
        url = f"https://zakon.rada.gov.ua/laws/show/{token}" if token else ""

        html = html_file.read_text(encoding="utf-8", errors="ignore")
        law = extract_law_from_html(html, law_id, url)
        if not law or not law.get("sections"):
            print(f"  ⚠ could not extract: {html_file.name}")
            continue

        meta = by_token.get(token)
        if meta:
            matched_tokens.add(token)
            law["title"] = law.get("title") or meta["title"]
            law["enacted_date"] = law.get("enacted_date") or meta["date"]

        from embedding_pipeline import law_to_chunks  # local import to avoid cycle noise
        chunks = _enrich(law_to_chunks(law), meta, source="curated_kb")
        if not chunks:
            continue

        embeddings = embed_chunks(chunks)
        upsert_to_qdrant(client, chunks, embeddings, collection_name=CURATED_COLLECTION)
        total_chunks += len(chunks)
        print(f"  ✓ {law_id}: {len(chunks)} chunks"
              f"{' (metadata matched)' if meta else ' (no metadata match)'}")

    # --- Summary-only points for CSV rows without full text ---
    summary_chunks = []
    for row in metadata:
        if row["token"] and row["token"] in matched_tokens:
            continue
        if not row["summary"]:
            continue
        law_id = f"summary-{row['token'] or _slug(row['identifier'] or row['title'])}"
        enacted_date, enacted_date_int = enacted_date_fields(row["date"])
        chunk = {
            "text": row["summary"],
            "law_id": law_id,
            "title": row["title"] or row["identifier"],
            "url": "",
            "enacted_date": enacted_date,
            "section_heading": "Summary",
            "chunk_index": 0,
        }
        if enacted_date_int is not None:
            chunk["enacted_date_int"] = enacted_date_int
        summary_chunks.extend(_enrich([chunk], row, source="curated_kb_summary"))

    if summary_chunks:
        print(f"\nSummary-only curated laws: {len(summary_chunks)}")
        embeddings = embed_chunks(summary_chunks)
        upsert_to_qdrant(client, summary_chunks, embeddings, collection_name=CURATED_COLLECTION)
        total_chunks += len(summary_chunks)

    info = client.get_collection(CURATED_COLLECTION)
    print("\n=== Done ===")
    print(f"  Chunks upserted this run: {total_chunks}")
    print(f"  Collection size: {info.points_count} vectors")


if __name__ == "__main__":
    main()
