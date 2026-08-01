"""Date parsing for the varied formats property sources emit."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

# "Added on 12/03/2026", "Reduced on 3 March 2026"
_DMY_SLASH = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_DMY_TEXT = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})\b",
    re.I,
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Rightmove's "addedOrReduced" text: "Added today", "Reduced yesterday",
# "Added on 30/07/2026"
_RELATIVE = re.compile(r"\b(today|yesterday)\b", re.I)


def parse_date(value, today: date | None = None) -> date | None:
    """Parse a date from an ISO timestamp, a d/m/Y string, or relative words."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text:
        return None

    today = today or date.today()

    m = _ISO.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _RELATIVE.search(text)
    if m:
        return today if m.group(1).lower() == "today" else today - timedelta(days=1)

    m = _DMY_SLASH.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    m = _DMY_TEXT.search(text)
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        except (ValueError, KeyError):
            return None

    return None


def days_since(when: date | None, today: date | None = None) -> int | None:
    """Whole days between `when` and today, or None if `when` is unknown."""
    if when is None:
        return None
    return ((today or date.today()) - when).days


def iso(value: date | None) -> str | None:
    """ISO string for storage, or None."""
    return value.isoformat() if value else None
