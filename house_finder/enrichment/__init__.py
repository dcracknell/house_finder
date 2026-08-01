"""Enrichment: public-record data the listing itself does not tell you.

Sources run cheapest-first (free government APIs before anything paid), and
results are cached in SQLite so re-running does not re-query the same postcode.
Enrichment is always best-effort: a source being down never fails the run, it
just leaves those fields empty.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

from house_finder.enrichment import broadband, crime, epc, flood, land_registry, propertydata
from house_finder.util.secrets import resolve_enabled

logger = logging.getLogger(__name__)

# How long each source's answer stays usable. Sold prices and infrastructure
# change slowly; flood warnings are live and must not be cached for long.
_CACHE_DAYS = {
    "land_registry": 30,
    "crime": 30,
    "epc": 180,
    "broadband": 180,
    "flood": 1,
    "propertydata": 30,
}

# The fields each source owns, used to restore a cached result onto a record.
_SOURCE_FIELDS = {
    "land_registry": (
        "local_sold_avg_price",
        "local_sold_sample_size",
        "price_vs_local_pct",
    ),
    "crime": ("crime_incidents_nearby",),
    "flood": ("flood_warnings_nearby",),
    "epc": ("epc_current", "epc_potential", "epc_rating"),
    "broadband": ("broadband_max_mbps",),
    "propertydata": ("local_sold_avg_price", "price_vs_local_pct"),
}


def _cache_key(source: str, record) -> str | None:
    """Cache by location, not by property - neighbours share the same answer."""
    location = record.postcode or record.outcode
    if not location:
        return None
    if source == "land_registry":
        # Sold comparables are filtered by property type, so type is part of
        # the key or a flat would inherit a house's comparables.
        return f"{source}|{location}|{record.property_type}"
    if source == "epc":
        # EPC is per building, so it cannot be shared across a postcode.
        return f"{source}|{location}|{(record.display_address or '').lower()[:60]}"
    return f"{source}|{location}"


def _read_cache(conn: sqlite3.Connection, key: str, max_age_days: int) -> dict | None:
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM enrichment_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        fetched = datetime.fromisoformat(row["fetched_at"]).date()
    except (ValueError, TypeError):
        return None
    if fetched < date.today() - timedelta(days=max_age_days):
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


def _write_cache(conn: sqlite3.Connection, key: str, provider: str, payload: dict) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO enrichment_cache "
            "(cache_key, provider, payload, fetched_at) VALUES (?, ?, ?, ?)",
            (key, provider, json.dumps(payload), datetime.now(UTC).isoformat()),
        )
    except sqlite3.Error as exc:
        logger.debug("enrichment: could not cache %s: %s", key, exc)


def _snapshot(record, fields: tuple[str, ...]) -> dict:
    return {f: getattr(record, f, None) for f in fields}


def _restore(record, values: dict) -> bool:
    applied = False
    for field, value in values.items():
        if value is not None:
            setattr(record, field, value)
            applied = True
    return applied


def enabled_sources(sources_config: dict) -> dict[str, dict]:
    """Resolve which enrichment sources are on, honouring "auto" flags."""
    config = (sources_config or {}).get("enrichment") or {}
    resolved = {}

    checks = {
        "land_registry": (),
        "crime": (),
        "flood": (),
        "epc": ("EPC_API_EMAIL", "EPC_API_KEY"),
        "broadband": ("OFCOM_API_KEY",),
        "propertydata": ("PROPERTYDATA_API_KEY",),
    }
    for name, secret_names in checks.items():
        block = config.get(name) or {}
        if resolve_enabled(block.get("enabled", False), *secret_names):
            resolved[name] = block
    return resolved


def enrich_records(
    records: list,
    sources_config: dict,
    settings: dict,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
) -> int:
    """Enrich records in place. Returns how many gained at least one field.

    `limit` caps how many properties are enriched in one run (enrichment is
    several HTTP calls each). Highest-scoring first, so the budget is spent on
    the properties actually worth knowing more about.
    """
    active = enabled_sources(sources_config)
    if not active or not records:
        return 0

    targets = sorted(records, key=lambda r: (r.fit_score is None, -(r.fit_score or 0)))
    if limit:
        targets = targets[:limit]

    logger.info(
        "enrichment: %d sources active (%s) for %d properties",
        len(active), ", ".join(sorted(active)), len(targets),
    )

    handlers = {
        "land_registry": lambda rec, cfg: land_registry.enrich(rec, cfg),
        "crime": lambda rec, cfg: crime.enrich(rec, cfg),
        "flood": lambda rec, cfg: flood.enrich(rec, cfg),
        "epc": lambda rec, cfg: epc.enrich(rec, cfg),
        "broadband": lambda rec, cfg: broadband.enrich(rec, cfg),
        "propertydata": lambda rec, cfg: propertydata.enrich(rec, cfg, settings),
    }

    enriched_count = 0
    credit_cap_hit = False

    for record in targets:
        touched = False
        for name, config in active.items():
            if name == "propertydata" and credit_cap_hit:
                continue

            fields = _SOURCE_FIELDS[name]
            key = _cache_key(name, record)

            if conn is not None and key:
                cached = _read_cache(conn, key, _CACHE_DAYS.get(name, 30))
                if cached is not None:
                    if _restore(record, cached):
                        touched = True
                    continue

            try:
                changed = handlers[name](record, config)
            except propertydata.CreditCapReached as exc:
                logger.warning("enrichment: %s", exc)
                credit_cap_hit = True
                continue
            except Exception as exc:  # noqa: BLE001 - enrichment never fails a run
                logger.debug("enrichment: %s failed for %s: %s", name, record.url, exc)
                continue

            if changed:
                touched = True
                if conn is not None and key:
                    _write_cache(conn, key, name, _snapshot(record, fields))

        if touched:
            enriched_count += 1

    if conn is not None:
        conn.commit()

    logger.info("enrichment: added data to %d properties", enriched_count)
    return enriched_count


def healthcheck_all(sources_config: dict, settings: dict) -> dict[str, tuple[bool, str | None]]:
    """Check every configured enrichment source."""
    active = enabled_sources(sources_config)
    results = {}
    probes = {
        "land_registry": land_registry.healthcheck,
        "crime": crime.healthcheck,
        "flood": flood.healthcheck,
        "epc": epc.healthcheck,
        "broadband": broadband.healthcheck,
        "propertydata": lambda: propertydata.healthcheck(settings),
    }
    for name in active:
        try:
            results[name] = probes[name]()
        except Exception as exc:  # noqa: BLE001
            results[name] = (False, str(exc))
    return results
