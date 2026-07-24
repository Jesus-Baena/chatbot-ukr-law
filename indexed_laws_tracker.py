import re
from datetime import datetime, timezone
from pathlib import Path

from config import CHUNK_OVERLAP, CHUNK_SIZE, QDRANT_COLLECTION

BASE_DIR = Path(__file__).parent
TRACKER_PATH = BASE_DIR / "INDEXED_LAWS.md"

_TABLE_HEADER = [
    "# Indexed Laws Tracker",
    "",
    f"This table tracks laws currently embedded into the Qdrant collection (`{QDRANT_COLLECTION}`).",
    "",
    "| Law ID | English Title | Sections | Chunks Indexed | Indexed Date (UTC) | Source URL |",
    "|---|---|---:|---:|---|---|",
]

_NOTES_HEADER = [
    "",
    "## Notes",
    "",
]

_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*(https?://[^|\s]+)\s*\|\s*$")
_ROW_RE_LEGACY = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(https?://[^|\s]+)\s*\|\s*$")


def _parse_existing_rows() -> dict[str, dict]:
    if not TRACKER_PATH.exists():
        return {}

    rows: dict[str, dict] = {}
    for raw_line in TRACKER_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        match = _ROW_RE.match(line)
        if match:
            law_id = match.group(1)
            rows[law_id] = {
                "law_id": law_id,
                "english_title": match.group(2),
                "sections": int(match.group(3)),
                "chunks_indexed": int(match.group(4)),
                "indexed_date": match.group(5).strip(),
                "source_url": match.group(6),
            }
            continue

        legacy = _ROW_RE_LEGACY.match(line)
        if legacy:
            law_id = legacy.group(1)
            rows[law_id] = {
                "law_id": law_id,
                "english_title": legacy.group(2),
                "sections": int(legacy.group(3)),
                "chunks_indexed": int(legacy.group(4)),
                "indexed_date": _today_utc_iso(),
                "source_url": legacy.group(5),
            }
    return rows


def _row_line(row: dict) -> str:
    return (
        f"| `{row['law_id']}` | {row['english_title']} | {row['sections']} | "
        f"{row['chunks_indexed']} | {row['indexed_date']} | {row['source_url']} |"
    )


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def upsert_indexed_law(law: dict, chunk_count: int, indexed_date: str | None = None):
    """Upsert one law entry in INDEXED_LAWS.md with date tracking."""
    rows = _parse_existing_rows()
    law_id = law["id"]
    prior = rows.get(law_id, {})

    rows[law_id] = {
        "law_id": law_id,
        "english_title": prior.get("english_title") or law.get("title", ""),
        "sections": int(law.get("section_count", len(law.get("sections", [])))),
        "chunks_indexed": int(chunk_count),
        "indexed_date": indexed_date or _today_utc_iso(),
        "source_url": law.get("url", ""),
    }

    _write_tracker(rows)


def _write_tracker(rows: dict[str, dict]):
    ordered = [rows[law_id] for law_id in sorted(rows.keys())]
    total_chunks = sum(row["chunks_indexed"] for row in ordered)

    lines = []
    lines.extend(_TABLE_HEADER)
    for row in ordered:
        lines.append(_row_line(row))

    lines.extend(_NOTES_HEADER)
    lines.append(f"- Indexed total: **{total_chunks}** chunks/vectors.")
    lines.append(f"- Chunk counts are based on the current chunking config (`CHUNK_SIZE={CHUNK_SIZE}`, `CHUNK_OVERLAP={CHUNK_OVERLAP}`).")
    lines.append("- Indexed Date (UTC) records when each law was last embedded/backfilled into the tracker.")
    lines.append("- If chunking settings or extraction logic changes, regenerate this table after re-indexing.")

    TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
