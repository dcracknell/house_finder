"""Environment Agency flood warnings - free, Open Government Licence, no key.

Reports flood warnings and alerts currently in force near a property. This is
live warning data, not a long-term flood-risk rating: an area with no active
warning today can still be in a flood zone. The ranker is told to read it that
way, and the README says so too.
"""

from __future__ import annotations

import logging

from house_finder.util import http

logger = logging.getLogger(__name__)

BASE_URL = "https://environment.data.gov.uk/flood-monitoring/id/floods"


def _warnings_near(lat: float, lon: float, radius_km: float = 5.0) -> int | None:
    resp = http.get_once(
        BASE_URL,
        params={"lat": f"{lat:.5f}", "long": f"{lon:.5f}", "dist": radius_km},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp is None:
        return None
    try:
        return len(resp.json().get("items") or [])
    except (ValueError, AttributeError):
        return None


def enrich(record, config: dict) -> bool:
    """Attach a count of flood warnings currently in force nearby."""
    if record.lat is None or record.lon is None:
        return False

    count = _warnings_near(record.lat, record.lon, float(config.get("radius_km", 5.0)))
    if count is None:
        return False

    record.flood_warnings_nearby = count
    if count:
        logger.debug(
            "flood: %s - %d active warning(s) nearby", record.display_address, count
        )
    return True


def healthcheck() -> tuple[bool, str | None]:
    resp = http.get_once(BASE_URL, params={"_limit": 1}, timeout=20)
    if resp is None:
        return False, "Environment Agency flood API unreachable"
    try:
        resp.json()
    except ValueError:
        return False, "Environment Agency flood API returned invalid JSON"
    return True, None
