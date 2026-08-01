"""EPC register lookups - free, but needs a self-service key.

A listing shows one EPC letter, if that. The register gives the underlying
numbers: current efficiency, the potential after improvements, and the gap
between them - which is really a proxy for "how much work does this need".

Register at https://epc.opendatacommunities.org/ and set EPC_API_EMAIL and
EPC_API_KEY in .env.
"""

from __future__ import annotations

import base64
import logging
import os

from house_finder.util import http
from house_finder.util.secrets import looks_configured_secret

logger = logging.getLogger(__name__)

BASE_URL = "https://epc.opendatacommunities.org/api/v1/domestic/search"

_RATING_BANDS = (
    (92, "A"), (81, "B"), (69, "C"), (55, "D"), (39, "E"), (21, "F"), (0, "G"),
)


def _auth_header() -> dict | None:
    email = os.environ.get("EPC_API_EMAIL")
    key = os.environ.get("EPC_API_KEY")
    if not email or not looks_configured_secret(key):
        return None
    token = base64.b64encode(f"{email}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def band_for_score(score: int | None) -> str | None:
    """Convert an efficiency score (1-100) to its A-G band."""
    if score is None:
        return None
    for threshold, band in _RATING_BANDS:
        if score >= threshold:
            return band
    return "G"


def _lookup(postcode: str, address_hint: str, headers: dict) -> dict | None:
    resp = http.get_once(
        BASE_URL,
        params={"postcode": postcode, "size": 25},
        headers=headers,
        timeout=25,
    )
    if resp is None:
        return None
    try:
        rows = (resp.json().get("rows") or [])
    except (ValueError, AttributeError):
        return None
    if not rows:
        return None

    # Match the street/house number where possible; the register holds every
    # certificate in the postcode and the wrong one is worse than none.
    hint_tokens = {t for t in (address_hint or "").lower().split() if len(t) > 2}
    best, best_overlap = None, 0
    for row in rows:
        address = str(row.get("address") or "").lower()
        overlap = len({t for t in address.split() if len(t) > 2} & hint_tokens)
        if overlap > best_overlap:
            best, best_overlap = row, overlap

    # Require real agreement, not one shared word like "road".
    return best if best_overlap >= 2 else None


def enrich(record, config: dict) -> bool:
    """Attach EPC current/potential scores to a record."""
    headers = _auth_header()
    if headers is None:
        return False
    if not record.postcode:
        return False

    row = _lookup(record.postcode, record.display_address, headers)
    if not row:
        return False

    try:
        current = int(row.get("current-energy-efficiency"))
    except (TypeError, ValueError):
        return False
    try:
        potential = int(row.get("potential-energy-efficiency"))
    except (TypeError, ValueError):
        potential = None

    record.epc_current = current
    record.epc_potential = potential
    record.epc_rating = record.epc_rating or (
        str(row.get("current-energy-rating") or "").upper() or band_for_score(current)
    )
    logger.debug(
        "epc: %s - current %s, potential %s",
        record.display_address, current, potential,
    )
    return True


def healthcheck() -> tuple[bool, str | None]:
    headers = _auth_header()
    if headers is None:
        return False, "EPC_API_EMAIL / EPC_API_KEY not configured (optional)"
    resp = http.get_once(BASE_URL, params={"postcode": "S1 2HE", "size": 1}, headers=headers)
    if resp is None:
        return False, "EPC register unreachable or credentials rejected"
    return True, None
