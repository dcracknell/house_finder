"""Syncing records into SQLite.

Two invariants matter here and both exist to protect the user's own work:

1. `status` and `notes` are user-owned. A re-scrape must never overwrite them.
2. Ranking fields on an existing row are only updated when this run actually
   produced a new score (`freshly_ranked`), so an unranked refresh never wipes
   a good score.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

from house_finder.util.dates import iso

logger = logging.getLogger(__name__)

# Columns written on insert. Kept explicit so a schema change is a visible diff.
_INSERT_COLUMNS = (
    "property_id", "source", "listing_type", "title", "display_address", "url",
    "property_type", "property_subtype_raw", "postcode", "outcode", "lat", "lon",
    "bedrooms", "bathrooms", "floor_area_sqft", "tenure", "leasehold_years_remaining",
    "epc_rating", "furnished", "description", "key_features", "agent_name",
    "image_url", "image_count", "price", "price_qualifier", "price_reduced",
    "auction", "first_listed_date", "last_update_date", "let_available_date",
    "listing_status", "fit_score", "fit_reason", "fit_confidence",
    "matched_criteria", "ranker_version", "local_sold_avg_price",
    "local_sold_sample_size", "price_vs_local_pct", "crime_incidents_nearby",
    "flood_warnings_nearby", "epc_current", "epc_potential", "broadband_max_mbps",
    "matched_area", "first_seen", "last_seen", "content_hash", "status", "notes",
)


def _record_values(record) -> dict:
    return {
        "property_id": record.property_id,
        "source": record.source,
        "listing_type": record.listing_type,
        "title": record.title,
        "display_address": record.display_address,
        "url": record.url,
        "property_type": record.property_type,
        "property_subtype_raw": record.property_subtype_raw,
        "postcode": record.postcode,
        "outcode": record.outcode,
        "lat": record.lat,
        "lon": record.lon,
        "bedrooms": record.bedrooms,
        "bathrooms": record.bathrooms,
        "floor_area_sqft": record.floor_area_sqft,
        "tenure": record.tenure,
        "leasehold_years_remaining": record.leasehold_years_remaining,
        "epc_rating": record.epc_rating,
        "furnished": record.furnished,
        "description": record.description,
        "key_features": json.dumps(record.key_features or []),
        "agent_name": record.agent_name,
        "image_url": record.image_url,
        "image_count": record.image_count,
        "price": record.price,
        "price_qualifier": record.price_qualifier,
        "price_reduced": int(bool(record.price_reduced)),
        "auction": int(bool(record.auction)),
        "first_listed_date": iso(record.first_listed_date),
        "last_update_date": iso(record.last_update_date),
        "let_available_date": iso(record.let_available_date),
        "listing_status": record.listing_status,
        "fit_score": record.fit_score,
        "fit_reason": record.fit_reason,
        "fit_confidence": record.fit_confidence,
        "matched_criteria": json.dumps(record.matched_criteria or []),
        "ranker_version": record.ranker_version,
        "local_sold_avg_price": record.local_sold_avg_price,
        "local_sold_sample_size": record.local_sold_sample_size,
        "price_vs_local_pct": record.price_vs_local_pct,
        "crime_incidents_nearby": record.crime_incidents_nearby,
        "flood_warnings_nearby": record.flood_warnings_nearby,
        "epc_current": record.epc_current,
        "epc_potential": record.epc_potential,
        "broadband_max_mbps": record.broadband_max_mbps,
        "matched_area": record.matched_area,
        "first_seen": iso(record.first_seen),
        "last_seen": iso(record.last_seen),
        "content_hash": record.content_hash,
        "status": "new",
        "notes": "",
    }


def load_stored(conn: sqlite3.Connection, property_ids: list[str]) -> dict[str, dict]:
    """Fetch the stored rows for the given ids, keyed by property_id."""
    if not property_ids:
        return {}
    stored: dict[str, dict] = {}
    chunk_size = 900  # stay under SQLite's variable limit
    for start in range(0, len(property_ids), chunk_size):
        chunk = property_ids[start : start + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM properties WHERE property_id IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        for row in rows:
            stored[row["property_id"]] = dict(row)
    return stored


def sync_record(conn: sqlite3.Connection, record, stored: dict | None = None) -> str:
    """Insert or update one record. Returns "new", "updated", or "unchanged"."""
    if stored is None:
        row = conn.execute(
            "SELECT * FROM properties WHERE property_id = ?", (record.property_id,)
        ).fetchone()
        stored = dict(row) if row else None

    values = _record_values(record)

    if stored is None:
        columns = ",".join(_INSERT_COLUMNS)
        placeholders = ",".join(f":{c}" for c in _INSERT_COLUMNS)
        conn.execute(
            f"INSERT INTO properties ({columns}) VALUES ({placeholders})",  # noqa: S608
            values,
        )
        return "new"

    # Never touch status/notes, and keep the original first_seen.
    update = {
        k: v
        for k, v in values.items()
        if k not in {"property_id", "status", "notes", "first_seen"}
    }

    # An adapter that skipped detail text must not blank a stored description.
    if not record.description and stored.get("description"):
        update.pop("description", None)
    if not record.key_features and stored.get("key_features"):
        update.pop("key_features", None)

    # Ranking fields only move when this run actually produced a score.
    if not record.freshly_ranked:
        for field in (
            "fit_score",
            "fit_reason",
            "fit_confidence",
            "matched_criteria",
            "ranker_version",
        ):
            update.pop(field, None)

    # Same for enrichment: a run that skipped enrichment must not clear it.
    for field in (
        "local_sold_avg_price",
        "local_sold_sample_size",
        "price_vs_local_pct",
        "crime_incidents_nearby",
        "flood_warnings_nearby",
        "epc_current",
        "epc_potential",
        "broadband_max_mbps",
    ):
        if update.get(field) is None and stored.get(field) is not None:
            update.pop(field, None)

    # A user-set status is a decision about the property; only the pipeline's
    # own housekeeping statuses may be refreshed from the listing.
    if stored.get("status") in {"new", "sold_stc", "withdrawn"}:
        if record.listing_status in {"sold_stc", "under_offer"}:
            update["status"] = "sold_stc"
        elif stored.get("status") in {"sold_stc", "withdrawn"}:
            # It is back on the market.
            update["status"] = "new"

    assignments = ", ".join(f"{k} = :{k}" for k in update)
    update["property_id"] = record.property_id
    conn.execute(
        f"UPDATE properties SET {assignments} WHERE property_id = :property_id",  # noqa: S608
        update,
    )
    return "updated"


def sync_records(conn: sqlite3.Connection, records: list) -> dict[str, int]:
    """Sync a batch of records. Returns counts of new/updated rows."""
    stored_map = load_stored(conn, [r.property_id for r in records])
    counts = {"new": 0, "updated": 0}
    for record in records:
        try:
            outcome = sync_record(conn, record, stored_map.get(record.property_id))
            counts[outcome] = counts.get(outcome, 0) + 1
        except sqlite3.Error as exc:
            logger.error("dedup: failed to sync %s: %s", record.property_id, exc)
    conn.commit()
    logger.info("dedup: %d new, %d updated", counts["new"], counts["updated"])
    return counts


def mark_stale(conn: sqlite3.Connection, stale_days: int, today: date | None = None) -> int:
    """Mark listings not seen recently as withdrawn.

    Only touches rows still at 'new' - once the user has engaged with a
    property, its status is theirs.
    """
    if stale_days <= 0:
        return 0
    today = today or date.today()
    cutoff = (today - timedelta(days=stale_days)).isoformat()
    cursor = conn.execute(
        "UPDATE properties SET status = 'withdrawn' "
        "WHERE status = 'new' AND last_seen < ?",
        (cutoff,),
    )
    conn.commit()
    if cursor.rowcount:
        logger.info(
            "dedup: marked %d listings withdrawn (not seen in %d days)",
            cursor.rowcount, stale_days,
        )
    return cursor.rowcount
