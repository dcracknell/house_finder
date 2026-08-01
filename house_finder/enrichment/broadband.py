"""Ofcom broadband availability - free tier, needs registration.

Max available speed at an address. Register at https://api.ofcom.org.uk/ and
set OFCOM_API_KEY in .env. Entirely optional: leave the key unset and the rest
of the pipeline is unaffected.
"""

from __future__ import annotations

import logging
import os

from house_finder.util import http
from house_finder.util.secrets import looks_configured_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://api-proxy.ofcom.org.uk/broadband/coverage"


def _headers() -> dict | None:
    key = os.environ.get("OFCOM_API_KEY")
    if not looks_configured_secret(key):
        return None
    return {"Ocp-Apim-Subscription-Key": key, "Accept": "application/json"}


def _max_speed(payload: dict) -> float | None:
    """Best available download speed across the addresses in the response."""
    availability = payload.get("Availability") or payload.get("availability") or []
    speeds = []
    for entry in availability:
        for field in (
            "MaxBbPredictedDown",
            "MaxSfbbPredictedDown",
            "MaxUfbbPredictedDown",
            "maxBbPredictedDown",
        ):
            value = entry.get(field)
            try:
                if value is not None:
                    speeds.append(float(value))
            except (TypeError, ValueError):
                continue
    return max(speeds) if speeds else None


def enrich(record, config: dict) -> bool:
    """Attach the maximum available broadband speed for the postcode."""
    headers = _headers()
    if headers is None:
        return False
    if not record.postcode:
        return False

    resp = http.get_once(
        f"{BASE_URL}/{record.postcode.replace(' ', '')}", headers=headers, timeout=25
    )
    if resp is None:
        return False
    try:
        payload = resp.json()
    except ValueError:
        return False

    speed = _max_speed(payload)
    if speed is None:
        return False

    record.broadband_max_mbps = round(speed, 1)
    logger.debug("broadband: %s - %.0f Mbps", record.display_address, speed)
    return True


def healthcheck() -> tuple[bool, str | None]:
    headers = _headers()
    if headers is None:
        return False, "OFCOM_API_KEY not configured (optional)"
    resp = http.get_once(f"{BASE_URL}/S12HE", headers=headers, timeout=20)
    if resp is None:
        return False, "Ofcom API unreachable or key rejected"
    return True, None
