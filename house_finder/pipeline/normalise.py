"""Post-adapter normalisation applied to every record, whatever its source.

Adapters map their own vocabulary onto PropertyRecord; this module then does
the work that must be identical everywhere: text cleanup, backfilling missing
coordinates, deriving the outcode, and computing the content hash.
"""

from __future__ import annotations

import logging

from house_finder.adapters.base import PropertyRecord
from house_finder.pipeline.listing_clean import (
    clean_address,
    clean_description,
    clean_text,
    content_hash,
)
from house_finder.util.geocode import extract_postcode, geocode, reverse_postcodes

logger = logging.getLogger(__name__)


def normalise_record(record: PropertyRecord, *, allow_geocode: bool = True) -> PropertyRecord:
    """Clean and complete a single record in place, returning it."""
    record.display_address = clean_address(record.display_address)
    record.title = clean_text(record.title)
    record.agent_name = clean_text(record.agent_name) or None
    record.description = clean_description(record.description)
    record.key_features = [
        cleaned for f in (record.key_features or []) if (cleaned := clean_text(f))
    ]

    if not record.postcode or not record.outcode:
        postcode, outcode = extract_postcode(record.display_address)
        record.postcode = record.postcode or postcode
        record.outcode = record.outcode or outcode

    # Most sources supply coordinates; geocoding is the fallback that keeps a
    # listing on the map and inside radius filtering.
    if allow_geocode and (record.lat is None or record.lon is None):
        target = record.postcode or record.outcode or record.display_address
        if target:
            coords = geocode(target)
            if coords:
                record.lat, record.lon = coords
            else:
                logger.debug("normalise: could not geocode %r", target)

    if record.listing_type not in {"sale", "rent"}:
        record.listing_type = "sale"

    record.content_hash = content_hash(record)
    return record


def backfill_postcodes(records: list[PropertyRecord]) -> int:
    """Recover full postcodes from coordinates, in bulk. Returns how many.

    Portals publish only the outward code for most listings, but the public
    record APIs (Land Registry, EPC, Ofcom) need a full postcode. Without this
    step those sources only work on the minority of listings that happen to
    include one.
    """
    needing = [
        r for r in records if not r.postcode and r.lat is not None and r.lon is not None
    ]
    if not needing:
        return 0

    found = reverse_postcodes([(r.lat, r.lon) for r in needing])
    filled = 0
    for record, postcode in zip(needing, found):
        if not postcode:
            continue
        record.postcode = postcode
        if not record.outcode:
            record.outcode = postcode.split(" ")[0]
        filled += 1

    logger.info(
        "normalise: recovered %d full postcodes from coordinates (of %d missing)",
        filled, len(needing),
    )
    return filled


def normalise_all(
    records: list[PropertyRecord],
    *,
    allow_geocode: bool = True,
    fill_postcodes: bool = True,
) -> list[PropertyRecord]:
    """Normalise a batch, dropping any record that fails, and de-duplicate.

    The same property can appear twice in one run when search areas overlap;
    keeping the first occurrence means the record carries the area that found
    it first, which matches how the areas are ordered in the profile.
    """
    seen: set[str] = set()
    out: list[PropertyRecord] = []
    for record in records:
        try:
            normalised = normalise_record(record, allow_geocode=allow_geocode)
        except Exception as exc:  # noqa: BLE001 - one bad listing must not stop the run
            logger.warning("normalise: skipping %s: %s", getattr(record, "url", "?"), exc)
            continue
        if normalised.property_id in seen:
            continue
        seen.add(normalised.property_id)
        out.append(normalised)

    if fill_postcodes:
        if backfill_postcodes(out):
            # The postcode is part of the content hash, so it must be recomputed.
            for record in out:
                record.content_hash = content_hash(record)

    return out
