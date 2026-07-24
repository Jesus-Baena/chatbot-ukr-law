"""
Corpus data-quality report.

Scans the scraped laws on disk (``data/laws/*.json``) and reports the
extraction-quality distribution: section-count histogram, quality labels
(ok / thin / suspect), extraction-mode mix, and the share of laws missing a
title. Use it to catch the "structurally collapsed" pattern (review finding #1)
before those laws reach the vector store.

Usage:
  python corpus_quality_report.py
  python corpus_quality_report.py --worst 25      # list the 25 largest suspects
  python corpus_quality_report.py --laws-dir data/laws
"""

import argparse
import json
from pathlib import Path

from config import LAWS_DIR
from law_processing import assess_law_quality, summarize_quality


def _iter_law_files(laws_dir: Path):
    for path in sorted(laws_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        yield path, payload


def main():
    parser = argparse.ArgumentParser(description="Report corpus extraction quality")
    parser.add_argument("--laws-dir", default=str(LAWS_DIR),
                        help="Directory of scraped law JSON files")
    parser.add_argument("--worst", type=int, default=15,
                        help="How many largest 'suspect' laws to list")
    args = parser.parse_args()

    laws_dir = Path(args.laws_dir)
    if not laws_dir.exists():
        print(f"✗ Laws directory not found: {laws_dir}")
        return

    total = 0
    errored = 0
    missing_title = 0
    mode_counts: dict[str, int] = {}
    assessments: list[dict] = []
    suspects: list[tuple[int, str, str]] = []  # (total_chars, law_id, title)

    for _, payload in _iter_law_files(laws_dir):
        total += 1
        if payload.get("error"):
            errored += 1
            continue

        if not (payload.get("title") or "").strip():
            missing_title += 1

        mode = payload.get("extraction_mode", "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

        # Reuse the persisted assessment when present, else recompute.
        quality = payload.get("quality") or assess_law_quality(payload)
        assessments.append(quality)

        if quality.get("quality") == "suspect":
            suspects.append(
                (quality.get("total_chars", 0), payload.get("id", "?"), payload.get("title", ""))
            )

    valid = total - errored
    print("=== Corpus quality report ===")
    print(f"  Law files:        {total}")
    print(f"  Error markers:    {errored}")
    print(f"  Valid laws:       {valid}")
    if valid:
        pct = 100.0 * missing_title / valid
        print(f"  Missing title:    {missing_title}  ({pct:4.1f}% of valid)")

    print("\n  Extraction modes:")
    for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"    {mode:>14}: {count}")

    print()
    print(summarize_quality(assessments))

    if suspects and args.worst > 0:
        suspects.sort(reverse=True)
        print(f"\n  Largest {min(args.worst, len(suspects))} suspect laws "
              "(substantial text, no article segmentation):")
        for chars, law_id, title in suspects[: args.worst]:
            title_str = (title or "").strip()[:60]
            print(f"    {chars:>8} chars  {law_id:<18} {title_str}")


if __name__ == "__main__":
    main()
