"""Postcode/place geocoding, distance maths, and UK postcode extraction.

Most listings already carry coordinates, so this is a fallback rather than a
hot path. Uses postcodes.io (free, no key, generous limits) for postcodes and
Nominatim for place names, with a local cache in data/cache/geocode/.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

from house_finder import PROJECT_ROOT
from house_finder.util import http

logger = logging.getLogger(__name__)

_CACHE_DIR: Path = PROJECT_ROOT / "data" / "cache" / "geocode"

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_POSTCODES_IO = "https://api.postcodes.io/postcodes"

EARTH_RADIUS_MILES = 3958.8

# Full UK postcode, and the outward code on its own.
_FULL_POSTCODE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I
)
_OUTCODE_ONLY = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b(?!\s*\d[A-Z]{2})", re.I)


def extract_postcode(text: str | None) -> tuple[str | None, str | None]:
    """Return (full_postcode, outcode) found in free text.

    Portal addresses are often truncated to the outcode ("Church View, S26"),
    so the outcode is returned separately and is frequently all there is.
    """
    if not text:
        return None, None
    m = _FULL_POSTCODE.search(text)
    if m:
        outward, inward = m.group(1).upper(), m.group(2).upper()
        return f"{outward} {inward}", outward
    m = _OUTCODE_ONLY.search(text)
    if m:
        candidate = m.group(1).upper()
        # Require a digit so ordinary words ("Bank", "Hall") are not mistaken
        # for outcodes.
        if any(ch.isdigit() for ch in candidate):
            return None, candidate
    return None, None


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in key).strip()[:80]
    return _CACHE_DIR / f"{safe}.json"


def _read_cache(key: str) -> tuple[float, float] | None | str:
    path = _cache_path(key)
    if not path.exists():
        return "MISS"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "MISS"
    if data is None:
        return None
    return float(data["lat"]), float(data["lon"])


def _write_cache(key: str, value: tuple[float, float] | None) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = None if value is None else {"lat": value[0], "lon": value[1]}
    try:
        _cache_path(key).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.debug("geocode: could not write cache for %r: %s", key, exc)


def geocode(location: str) -> tuple[float, float] | None:
    """Return (lat, lon) for a UK postcode or place name, or None.

    Results are cached permanently - places do not move. Transient failures are
    NOT cached, so the next run retries them.
    """
    if not location or not location.strip():
        return None
    key = location.strip().upper()

    cached = _read_cache(key)
    if cached != "MISS":
        return cached

    postcode, outcode = extract_postcode(key)
    result: tuple[float, float] | None = None

    lookup = postcode or outcode
    if lookup:
        result = _geocode_postcode(lookup)

    if result is None:
        result = _geocode_place(location)

    # Only cache a definitive "not found"; _geocode_* return None for both
    # "no result" and "request failed", so re-checking next run is the safe
    # behaviour for anything we could not resolve.
    if result is not None:
        _write_cache(key, result)
    return result


def _geocode_postcode(postcode: str) -> tuple[float, float] | None:
    compact = postcode.replace(" ", "")
    is_outcode = not _FULL_POSTCODE.match(postcode)
    url = (
        f"{_POSTCODES_IO}/{compact}"
        if not is_outcode
        else f"https://api.postcodes.io/outcodes/{compact}"
    )
    resp = http.get_once(url, timeout=15)
    if resp is None:
        return None
    try:
        payload = resp.json().get("result") or {}
        lat, lon = payload.get("latitude"), payload.get("longitude")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except (ValueError, AttributeError):
        return None


def _geocode_place(place: str) -> tuple[float, float] | None:
    http.polite_delay("nominatim.openstreetmap.org", 1.1)  # Nominatim policy: 1 req/sec
    resp = http.get_once(
        _NOMINATIM_URL,
        params={"q": f"{place}, UK", "format": "json", "limit": 1, "countrycodes": "gb"},
        headers={"User-Agent": "house-finder/0.1 (personal property search)"},
        timeout=15,
    )
    if resp is None:
        return None
    try:
        results = resp.json()
    except ValueError:
        return None
    if not results:
        return None
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None


def reverse_postcodes(
    points: list[tuple[float, float]], chunk_size: int = 90
) -> list[str | None]:
    """Look up the nearest full postcode for each (lat, lon), in bulk.

    Portals usually publish only the outward code ("Sheffield, S12"), but most
    public-record APIs are keyed on a full postcode. Coordinates are always
    present, so this recovers the missing precision in one request per 90
    properties rather than one per property.

    Returns a list aligned with `points`, with None where nothing was found.
    """
    results: list[str | None] = []
    session = http.get_session()

    for start in range(0, len(points), chunk_size):
        chunk = points[start : start + chunk_size]
        payload = {
            "geolocations": [
                {"longitude": lon, "latitude": lat, "limit": 1, "radius": 500}
                for lat, lon in chunk
            ]
        }
        try:
            resp = session.post(_POSTCODES_IO, json=payload, timeout=30)
            resp.raise_for_status()
            entries = resp.json().get("result") or []
        except Exception as exc:  # noqa: BLE001 - enrichment detail, never fatal
            logger.debug("geocode: bulk reverse lookup failed: %s", exc)
            results.extend([None] * len(chunk))
            continue

        for entry in entries:
            matches = (entry or {}).get("result") or []
            results.append(matches[0].get("postcode") if matches else None)

        # A short response would silently misalign every later result.
        while len(results) < start + len(chunk):
            results.append(None)

    return results


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
