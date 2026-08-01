"""PropertyData API - PAID, optional, credit-capped.

Note what this is NOT: PropertyData has no general listings search, so it can
never be a source of properties. It sells analytics (local price statistics,
flood risk, council tax, demographics) that layer onto properties found
elsewhere.

Everything it offers overlaps with the free sources already wired up, so it
stays off unless PROPERTYDATA_API_KEY is set, and only the single most useful
endpoint is enabled by default. Every call is counted against
`monthly_credit_cap` in config/sources.yaml.
"""

from __future__ import annotations

import logging
import os

from house_finder.util import http
from house_finder.util.quota import credits_used_this_month, record_credit_usage
from house_finder.util.secrets import looks_configured_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://api.propertydata.co.uk"


class CreditCapReached(RuntimeError):
    """Monthly PropertyData credit budget is spent."""


def _api_key() -> str | None:
    key = os.environ.get("PROPERTYDATA_API_KEY")
    return key if looks_configured_secret(key) else None


def _call(endpoint: str, params: dict, settings: dict, cap: int) -> dict | None:
    key = _api_key()
    if key is None:
        return None

    if cap > 0 and credits_used_this_month(settings) >= cap:
        raise CreditCapReached(
            f"PropertyData monthly credit cap ({cap}) reached; skipping further calls"
        )

    resp = http.get_once(
        f"{BASE_URL}/{endpoint}",
        params={"key": key, **params},
        headers={"Accept": "application/json"},
        timeout=25,
    )
    if resp is None:
        return None

    try:
        payload = resp.json()
    except ValueError:
        return None

    record_credit_usage("propertydata", 1, settings, note=endpoint)

    if payload.get("status") != "success":
        logger.debug(
            "propertydata: %s returned %s (%s)",
            endpoint, payload.get("status"), payload.get("message"),
        )
        return None
    return payload


def enrich(record, config: dict, settings: dict) -> bool:
    """Add PropertyData signals. Returns True if anything was attached."""
    if _api_key() is None:
        return False
    if not record.postcode and not record.outcode:
        return False

    endpoints = config.get("endpoints") or {}
    cap = int(config.get("monthly_credit_cap", 0))
    postcode = record.postcode or record.outcode
    enriched = False

    if endpoints.get("prices"):
        # Only fill this in if the free Land Registry lookup came back empty -
        # never pay for a number already obtained for free.
        if record.local_sold_avg_price is None and record.price:
            payload = _call("prices", {"postcode": postcode}, settings, cap)
            data = (payload or {}).get("data") or {}
            average = data.get("average") or (data.get("long_let") or {}).get("average")
            try:
                average = int(average)
            except (TypeError, ValueError):
                average = None
            if average:
                record.local_sold_avg_price = average
                record.price_vs_local_pct = round(
                    (record.price - average) / average * 100, 1
                )
                enriched = True

    if endpoints.get("flood_risk") and record.flood_warnings_nearby is None:
        payload = _call("flood-risk", {"postcode": postcode}, settings, cap)
        risk = ((payload or {}).get("data") or {}).get("flood_risk")
        if risk:
            logger.debug("propertydata: %s flood risk %s", postcode, risk)
            enriched = True

    return enriched


def healthcheck(settings: dict | None = None) -> tuple[bool, str | None]:
    if _api_key() is None:
        return False, "PROPERTYDATA_API_KEY not configured (optional, paid)"
    payload = _call("prices", {"postcode": "S1"}, settings or {}, 0)
    if payload is None:
        return False, "PropertyData unreachable or key rejected"
    return True, None
