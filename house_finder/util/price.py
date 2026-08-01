"""Parsing UK property price strings and floor areas.

Handles the shapes portals actually use:
  "£350,000", "Guide Price £350,000", "£350,000-£375,000", "POA",
  "Offers in Region of £250,000", "£1,200 pcm", "£150 pw"
"""

from __future__ import annotations

import re

# Qualifiers that appear before or after a sale price.
_QUALIFIER_PATTERNS = (
    (re.compile(r"guide\s*price", re.I), "Guide Price"),
    (re.compile(r"offers?\s+(?:in\s+)?(?:the\s+)?region\s+of", re.I), "Offers in Region of"),
    (re.compile(r"offers?\s+(?:over|above|in\s+excess\s+of)", re.I), "Offers Over"),
    (re.compile(r"offers?\s+invited", re.I), "Offers Invited"),
    (re.compile(r"fixed\s*price", re.I), "Fixed Price"),
    (re.compile(r"asking\s*price", re.I), "Asking Price"),
    (re.compile(r"from\b", re.I), "From"),
    (re.compile(r"\bauction\b", re.I), "Auction"),
    (re.compile(r"shared\s+ownership", re.I), "Shared Ownership"),
    (re.compile(r"\bpoa\b|price\s+on\s+application", re.I), "POA"),
)

_MONEY = re.compile(r"£\s*([\d,]+(?:\.\d+)?)\s*(k|m)?", re.I)
_PCM = re.compile(r"\bpcm\b|per\s+calendar\s+month|per\s+month", re.I)
_PW = re.compile(r"\bpw\b|per\s+week", re.I)

# "1,104 sq. ft." / "1104 sqft" / "102 sq m" / "102 m²"
_SQFT = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*\.?\s*ft|sqft|square\s+feet)", re.I)
_SQM = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*\.?\s*m|sqm|m²|square\s+met(?:re|er)s?)", re.I)

SQM_TO_SQFT = 10.7639


def _to_int(number: str, suffix: str | None) -> int:
    value = float(number.replace(",", ""))
    if suffix:
        if suffix.lower() == "k":
            value *= 1_000
        elif suffix.lower() == "m":
            value *= 1_000_000
    return int(round(value))


def parse_price(text: str | None) -> tuple[int | None, str | None]:
    """Return (amount, qualifier) from a price string.

    For a range ("£350,000-£375,000") the LOWER figure is returned - that is
    the number a buyer filters on, and taking the top of a guide range would
    wrongly push properties out of budget.
    """
    if not text:
        return None, None

    qualifier = None
    for pattern, label in _QUALIFIER_PATTERNS:
        if pattern.search(text):
            qualifier = label
            break

    if qualifier == "POA":
        return None, "POA"

    amounts = [_to_int(m.group(1), m.group(2)) for m in _MONEY.finditer(text)]
    # Ignore implausibly small figures picked up from fee text ("£50 admin").
    amounts = [a for a in amounts if a >= 100]
    if not amounts:
        return None, qualifier

    if _PW.search(text) and not _PCM.search(text):
        # Normalise weekly rent to monthly so one column means one thing.
        return int(round(min(amounts) * 52 / 12)), qualifier or "pcm"
    if _PCM.search(text):
        return min(amounts), qualifier or "pcm"

    return min(amounts), qualifier


def parse_rent_pcm(amount: int | None, frequency: str | None) -> int | None:
    """Normalise a rent amount to monthly given the source's frequency label."""
    if amount is None:
        return None
    freq = (frequency or "").lower()
    if "week" in freq:
        return int(round(amount * 52 / 12))
    if "annual" in freq or "year" in freq:
        return int(round(amount / 12))
    return amount


def parse_floor_area_sqft(text: str | None) -> float | None:
    """Return floor area in square feet from a display string, if present."""
    if not text:
        return None
    m = _SQFT.search(text)
    if m:
        return round(float(m.group(1).replace(",", "")), 1)
    m = _SQM.search(text)
    if m:
        return round(float(m.group(1).replace(",", "")) * SQM_TO_SQFT, 1)
    return None


def format_price(amount: int | None, listing_type: str = "sale") -> str:
    """Human-readable price for the workbook, dashboard, and email."""
    if amount is None:
        return "POA"
    if listing_type == "rent":
        return f"£{amount:,} pcm"
    return f"£{amount:,}"
