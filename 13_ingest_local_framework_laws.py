"""
Step 13: Ingest locally-provided foundational framework laws into the curated
collection.

`zakon.rada.gov.ua` blocks this host, so framework laws (e.g. "On Public
Associations", codes) are supplied as local files (PDF / DOCX / HTML) — typically
English translations dropped into `data/`. This script extracts them (Docling for
PDF/DOCX, HTML parser for HTML), chunks + Gemini-embeds, and upserts into
CURATED_COLLECTION as `source="curated_kb"` LAW points (not secondary reports),
with the nested `metadata` object Flowise reads for citations and a clean title.

Title / law_id / date are parsed from the filename when possible, e.g.
  "On public associations _ dated 22.03.2012 No. 4572-VI.pdf"
   -> title "On Public Associations", law_id "4572-vi", date 2012-03-22

Usage:
  python 13_ingest_local_framework_laws.py --path data          # scan a dir (non-recursive)
  python 13_ingest_local_framework_laws.py --path "data/foo.pdf"  # a single file
  python 13_ingest_local_framework_laws.py --path data --dry-run
"""
import argparse
import re
import sys
from pathlib import Path

from config import CURATED_COLLECTION, QDRANT_COLLECTION
from date_utils import normalize_date, enacted_date_fields
from embedding_pipeline import embed_chunks, law_to_chunks, setup_qdrant, upsert_to_qdrant, get_processed_ids
from law_processing import extract_law_from_html
from report_processing import _extract_sections
from service_clients import get_qdrant_client

FILE_GLOBS = ("*.pdf", "*.docx", "*.htm", "*.html")


def parse_filename(stem: str) -> tuple[str, str, str]:
    """Return (law_id, title, iso_date) parsed best-effort from a filename stem."""
    law_id = ""
    m = re.search(r'(?:No\.?|№)\s*([0-9][0-9A-Za-z\-\/_]*)', stem)
    if m:
        law_id = m.group(1).strip().lower()
    iso = ""
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', stem)
    if m:
        iso = normalize_date(m.group(0))
    # title = text before " _ dated" / " dated " / " No." / " №"
    title = re.split(r'\s+_\s+dated|\s+dated\s+|\s+No\.|\s+№', stem)[0].strip(" _-")
    if title:
        title = title[0].upper() + title[1:]
    return law_id or re.sub(r'[^0-9A-Za-z]+', '-', stem.lower()).strip('-')[:50], title or stem, iso


def build_law(path: Path) -> dict | None:
    law_id, title, iso = parse_filename(path.stem)
    url = f"https://zakon.rada.gov.ua/laws/show/{law_id}" if re.match(r'^\d', law_id) else ""
    if path.suffix.lower() in (".htm", ".html"):
        law = extract_law_from_html(path.read_text(encoding="utf-8", errors="ignore"), law_id, url)
        sections = (law or {}).get("sections") or []
    else:
        sections, _mode = _extract_sections(path)
    text_len = sum(len(s.get("text", "")) for s in sections)
    if not sections or text_len < 400:
        print(f"  ! {path.name}: extracted only {text_len} chars — skip", file=sys.stderr)
        return None
    return {"id": law_id, "title": title, "url": url, "enacted_date": iso,
            "sections": sections, "_text_len": text_len}


def enrich(chunks: list[dict], law: dict) -> list[dict]:
    for c in chunks:
        c["source"] = "curated_kb"
        c["category"] = "curated_kb"
        c["title"] = law["title"]
        c["metadata"] = {
            "law_id": law["id"], "title": law["title"], "url": law["url"],
            "enacted_date": c.get("enacted_date", ""), "category": "curated_kb", "source": "curated_kb",
        }
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default="data", help="file or directory (non-recursive) of law files")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = Path(args.path)
    if p.is_file():
        files = [p]
    else:
        files = sorted({f for g in FILE_GLOBS for f in p.glob(g)})
    if not files:
        print(f"No law files under {p}")
        return 1

    client = get_qdrant_client()
    if not args.dry_run:
        setup_qdrant(client, collection_name=CURATED_COLLECTION)
    existing = get_processed_ids(client, CURATED_COLLECTION) | get_processed_ids(client, QDRANT_COLLECTION)

    total = ingested = skipped = failed = 0
    for f in files:
        law = build_law(f)
        if not law:
            failed += 1
            continue
        if law["id"] in existing:
            print(f"  = {law['id']} ({law['title'][:45]}) already present — skip")
            skipped += 1
            continue
        chunks = enrich(law_to_chunks(law), law)
        if not chunks:
            print(f"  ! {f.name}: no chunks — skip", file=sys.stderr)
            failed += 1
            continue
        if args.dry_run:
            print(f"  ✓ {law['id']} '{law['title'][:45]}' date={law['enacted_date'] or '?'} "
                  f"— {len(chunks)} chunks ({law['_text_len']:,} chars)")
        else:
            upsert_to_qdrant(client, chunks, embed_chunks(chunks), collection_name=CURATED_COLLECTION)
            print(f"  ✓ {law['id']} '{law['title'][:45]}' — {len(chunks)} chunks upserted")
        total += len(chunks)
        ingested += 1

    print(f"\n=== {'DRY-RUN ' if args.dry_run else ''}Summary ===")
    print(f"  ingested: {ingested} laws, {total} chunks | skipped: {skipped} | failed: {failed}")
    if not args.dry_run:
        print(f"  {CURATED_COLLECTION}: {client.get_collection(CURATED_COLLECTION).points_count} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
