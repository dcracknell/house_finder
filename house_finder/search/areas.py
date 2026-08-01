"""Resolving configured search areas to Rightmove location identifiers.

House search is filter-driven, not keyword-driven: the "query" is an area plus
a set of numeric constraints. This module turns a plain postcode or place name
from profile.json into the identifier Rightmove's search needs, and caches the
result (places do not move).
"""

from __future__ import annotations

import json
import logging

from house_finder import PROJECT_ROOT
from house_finder.util import http

logger = logging.getLogger(__name__)

TYPEAHEAD_URL = "https://los.rightmove.co.uk/typeahead"

_CACHE_PATH = PROJECT_ROOT / "data" / "cache" / "rightmove_locations.json"

# Rightmove only accepts these radius values; anything else is snapped down to
# the nearest allowed one so a configured 4 miles never silently becomes 40.
ALLOWED_RADII = (0.0, 0.25, 0.5, 1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0)

# Location types worth searching, best first. STATION/POSTCODE matches are
# more precise than a whole REGION when the user typed a postcode.
_TYPE_PRIORITY = ("POSTCODE", "OUTCODE", "REGION", "STATION")


class AreaResolutionError(RuntimeError):
    """A configured search area could not be resolved to a location."""


def snap_radius(miles: float | int | None) -> float:
    """Snap a requested radius to the nearest allowed value at or below it."""
    if miles is None:
        return 3.0
    try:
        value = float(miles)
    except (TypeError, ValueError):
        return 3.0
    if value <= 0:
        return 0.0
    allowed_below = [r for r in ALLOWED_RADII if r <= value]
    return max(allowed_below) if allowed_below else ALLOWED_RADII[0]


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("areas: could not write location cache: %s", exc)


def _score_match(match: dict, query: str) -> tuple:
    """Rank typeahead matches: exact display name first, then type priority."""
    display = (match.get("displayName") or "").strip().lower()
    type_index = (
        _TYPE_PRIORITY.index(match.get("type"))
        if match.get("type") in _TYPE_PRIORITY
        else len(_TYPE_PRIORITY)
    )
    return (0 if display == query.strip().lower() else 1, type_index, len(display))


def resolve_location(place: str, *, delay_seconds: float = 1.0) -> str:
    """Return a Rightmove locationIdentifier such as "REGION^1195".

    Raises AreaResolutionError when the place cannot be matched, so a typo in
    profile.json fails loudly instead of silently searching the wrong town.
    """
    key = place.strip().upper()
    if not key:
        raise AreaResolutionError("Search area is empty - set postcode_or_place in profile.json")

    cache = _load_cache()
    if key in cache:
        return cache[key]

    http.polite_delay("los.rightmove.co.uk", delay_seconds)
    resp = http.get(
        TYPEAHEAD_URL,
        params={"query": key, "limit": 10, "exclude": ""},
        # This endpoint content-negotiates and answers in XML unless JSON is
        # asked for explicitly - the session's default Accept is HTML.
        headers={
            "Referer": "https://www.rightmove.co.uk/",
            "Accept": "application/json",
        },
    )
    try:
        matches = resp.json().get("matches") or []
    except ValueError as exc:
        raise AreaResolutionError(
            f"Rightmove typeahead returned invalid JSON for {place!r}"
        ) from exc

    usable = [m for m in matches if m.get("id") and m.get("type")]
    if not usable:
        raise AreaResolutionError(
            f"Rightmove has no location matching {place!r}. "
            "Check the spelling in config/profile.json."
        )

    best = sorted(usable, key=lambda m: _score_match(m, key))[0]
    identifier = f"{best['type']}^{best['id']}"
    logger.info(
        "areas: resolved %r to %s (%s)", place, identifier, best.get("displayName")
    )

    cache[key] = identifier
    _save_cache(cache)
    return identifier


def iter_areas(criteria: dict) -> list[dict]:
    """Return the normalised search areas from a criteria block.

    Each area is {label, postcode_or_place, radius_miles}. This is the whole
    replacement for the source project's LLM query generation: a buyer has a
    short, fixed list of places they will actually live, so there is nothing
    to generate or rotate.
    """
    areas = criteria.get("search_areas") or []
    normalised = []
    for i, area in enumerate(areas):
        if isinstance(area, str):
            area = {"postcode_or_place": area}
        place = (area.get("postcode_or_place") or "").strip()
        if not place:
            logger.warning("areas: skipping area %d with no postcode_or_place", i)
            continue
        normalised.append(
            {
                "label": area.get("label") or place,
                "postcode_or_place": place,
                "radius_miles": snap_radius(area.get("radius_miles")),
            }
        )
    return normalised
