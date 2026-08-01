"""Cleaning listing text and computing the change-detection hash.

Ranking code assumes descriptions have already been through clean_description().
The content hash decides whether a listing is re-sent to the LLM, so it must
cover exactly the fields that would change a score - and nothing volatile, or
every run would look like a change and re-scoring would never stop.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

# Agent boilerplate that adds no signal but does add tokens.
_BOILERPLATE = (
    re.compile(r"council tax band[:\s]*[a-h]\b.*?(?=\.|$)", re.I),
    re.compile(r"tenure[:\s]*(freehold|leasehold).*?(?=\.|$)", re.I),
    re.compile(r"viewing[s]?\s+(?:strictly\s+)?by\s+appointment.*?(?=\.|$)", re.I),
    re.compile(r"to arrange a viewing.*?(?=\.|$)", re.I),
    re.compile(r"please contact.{0,40}(?:office|branch|team).*?(?=\.|$)", re.I),
    re.compile(r"all measurements are approximate.*?(?=\.|$)", re.I),
    re.compile(r"these particulars.{0,120}(?:guidance|guide) only.*?(?=\.|$)", re.I),
    re.compile(r"money laundering regulations.*?(?=\.|$)", re.I),
    re.compile(r"we are legally required.*?(?=\.|$)", re.I),
    re.compile(r"referral fee[s]?.*?(?=\.|$)", re.I),
    re.compile(r"epc rating[:\s]*[a-g]\b", re.I),
)

_WHITESPACE = re.compile(r"[ \t ]+")
_NEWLINES = re.compile(r"\s*\n\s*")


def clean_text(text: str | None) -> str:
    """Collapse whitespace and normalise unicode in a short field."""
    if not text:
        return ""
    normalised = unicodedata.normalize("NFKC", str(text))
    normalised = normalised.replace("\r", " ").replace("\n", " ")
    return _WHITESPACE.sub(" ", normalised).strip()


def clean_address(address: str | None) -> str:
    """Tidy an address for display.

    Portals embed line breaks inside addresses ("Lodge Lane,\\nDinnington,\\nSheffield"),
    which look broken in the workbook and the map popups.
    """
    cleaned = clean_text(address)
    # Collapse the ", ," and trailing commas left by removing line breaks.
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    cleaned = re.sub(r"(,\s*)+", ", ", cleaned)
    return cleaned.strip(" ,")


def clean_description(text: str | None, max_chars: int = 4000) -> str:
    """Strip HTML, remove agent boilerplate, and truncate."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(separator=" ")

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    for pattern in _BOILERPLATE:
        text = pattern.sub(" ", text)

    text = _WHITESPACE.sub(" ", text)
    text = _NEWLINES.sub("\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > max_chars:
        # Cut at a sentence boundary where possible so the LLM never sees a
        # description that stops mid-word.
        cut = text[:max_chars]
        last_stop = cut.rfind(". ")
        text = (cut[: last_stop + 1] if last_stop > max_chars * 0.6 else cut).strip() + " ..."

    return text


def content_hash(record) -> str:
    """Stable hash of the fields that should trigger a re-score when changed.

    Deliberately excludes last_seen, image counts, and anything else that
    churns without changing how good a match the property is.
    """
    parts = [
        str(record.price or ""),
        str(record.price_qualifier or ""),
        str(record.bedrooms or ""),
        str(record.bathrooms or ""),
        str(record.property_type or ""),
        str(record.tenure or ""),
        str(record.floor_area_sqft or ""),
        str(record.listing_status or ""),
        clean_text(record.display_address).lower(),
        clean_text(record.description).lower(),
        "|".join(sorted(clean_text(f).lower() for f in (record.key_features or []))),
    ]
    return hashlib.sha1("␟".join(parts).encode("utf-8")).hexdigest()
