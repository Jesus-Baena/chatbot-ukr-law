"""
Date normalization for law metadata.

Rada sources emit dates in several formats — ISO ``YYYY-MM-DD`` (open-data feed,
doc.txt), ``DD.MM.YYYY`` (HTML pages), sometimes ``DD/MM/YYYY`` or
``YYYY.MM.DD``. Storing them verbatim broke range filtering two ways: the query
side compared strings lexicographically (wrong for ``DD.MM.YYYY``), and Qdrant's
``Range`` needs a numeric/datetime field, not the ``KEYWORD``-indexed string.

This module normalizes everything to ISO ``YYYY-MM-DD`` for display and to a
sortable ``YYYYMMDD`` integer (``enacted_date_int``) for range queries.
"""

import re
from datetime import datetime

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_YMD_DOT_RE = re.compile(r"(\d{4})[./](\d{2})[./](\d{2})")
_DMY_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")


def _valid(year: int, month: int, day: int) -> bool:
    try:
        datetime(year, month, day)
        return True
    except ValueError:
        return False


def normalize_date(raw) -> str:
    """Return a validated ISO ``YYYY-MM-DD`` date, or ``""`` if unparseable.

    Accepts ISO, ``YYYY.MM.DD``, ``DD.MM.YYYY`` and ``DD/MM/YYYY`` (the day-first
    forms found on Rada HTML pages), and tolerates surrounding text.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""

    # Year-first forms (ISO or dotted).
    for pattern in (_ISO_RE, _YMD_DOT_RE):
        m = pattern.search(s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if _valid(y, mo, d):
                return f"{y:04d}-{mo:02d}-{d:02d}"

    # Day-first forms (DD.MM.YYYY / DD/MM/YYYY).
    m = _DMY_RE.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return ""


def date_to_int(iso_date: str) -> int | None:
    """Convert an ISO ``YYYY-MM-DD`` date to a sortable ``YYYYMMDD`` int."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso_date or "")
    if not m:
        return None
    return int(m.group(1) + m.group(2) + m.group(3))


def enacted_date_fields(raw) -> tuple[str, int | None]:
    """Return ``(iso_date, yyyymmdd_int_or_None)`` for a raw date value."""
    iso = normalize_date(raw)
    return iso, date_to_int(iso)
