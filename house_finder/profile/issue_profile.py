"""Build config/profile.json from a GitHub issue form submission.

Every field the search needs is already a form field, so this is a plain
deterministic mapping - no LLM call, nothing to pay for, nothing to go wrong
at 3am in a scheduled workflow.
"""

from __future__ import annotations

import json
import logging
import re

from house_finder import PROFILE_PATH, load_profile

logger = logging.getLogger(__name__)

# GitHub renders issue forms as "### Label\n\nvalue\n\n### Next label..."
_SECTION = re.compile(r"^###\s+(.+?)\s*$", re.M)

_NO_RESPONSE = {"_no response_", "_none_", "none", "n/a", "-", ""}

_TYPE_ALIASES = {
    "detached": "detached",
    "semi-detached": "semi_detached",
    "semi detached": "semi_detached",
    "terraced": "terraced",
    "terrace": "terraced",
    "flat": "flat",
    "apartment": "flat",
    "flat / apartment": "flat",
    "bungalow": "bungalow",
    "land": "land",
    "land / plot": "land",
    "plot": "land",
}


def parse_issue_body(body: str) -> dict[str, str]:
    """Split an issue body into {lowercased heading: value}."""
    sections: dict[str, str] = {}
    matches = list(_SECTION.finditer(body or ""))
    for index, match in enumerate(matches):
        heading = match.group(1).strip().lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[heading] = body[start:end].strip()
    return sections


def _value(sections: dict[str, str], *names: str) -> str:
    for name in names:
        raw = sections.get(name.lower())
        if raw is None:
            continue
        cleaned = raw.strip()
        if cleaned.lower() in _NO_RESPONSE:
            continue
        return cleaned
    return ""


def _list(sections: dict[str, str], *names: str) -> list[str]:
    """Parse a textarea into a list, accepting newline or comma separation."""
    raw = _value(sections, *names)
    if not raw:
        return []
    parts = raw.split("\n") if "\n" in raw else raw.split(",")
    out = []
    for part in parts:
        cleaned = part.strip().lstrip("-*").strip()
        if cleaned and cleaned.lower() not in _NO_RESPONSE:
            out.append(cleaned)
    return out


def _int(sections: dict[str, str], *names: str) -> int | None:
    raw = _value(sections, *names)
    if not raw:
        return None
    match = re.search(r"-?\d[\d,]*", raw.replace("£", ""))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _bool(sections: dict[str, str], *names: str) -> bool | None:
    raw = _value(sections, *names).lower()
    if not raw:
        return None
    if raw.startswith(("yes", "true", "on")) or "[x]" in raw:
        return True
    if raw.startswith(("no", "false", "off")):
        return False
    return None


def _checkbox_list(sections: dict[str, str], *names: str) -> list[str]:
    """Ticked options from a checkboxes field ("- [x] Detached")."""
    raw = _value(sections, *names)
    return [
        m.group(1).strip().lower()
        for m in re.finditer(r"^\s*-\s*\[[xX]\]\s*(.+?)\s*$", raw, re.M)
    ]


def _property_types(sections: dict[str, str], *names: str) -> list[str]:
    values = _checkbox_list(sections, *names) or [
        v.lower() for v in _list(sections, *names)
    ]
    mapped = []
    for value in values:
        canonical = _TYPE_ALIASES.get(value.strip().lower())
        if canonical and canonical not in mapped:
            mapped.append(canonical)
    return mapped


def _areas(sections: dict[str, str], area_field: str, radius_field: str) -> list[dict]:
    places = _list(sections, area_field)
    radius = _int(sections, radius_field) or 3
    return [
        {"label": place, "postcode_or_place": place, "radius_miles": radius}
        for place in places
    ]


def build_profile_from_issue(
    body: str, write: bool = True, base_profile: dict | None = None
) -> dict:
    """Turn an issue body into a profile, merged over `base_profile`.

    Merging means a blank form field leaves the existing setting alone rather
    than wiping it, so someone can resubmit the form to change one thing.
    """
    sections = parse_issue_body(body)
    if not sections:
        raise ValueError(
            "No form fields found in the issue body. The issue must be created "
            "from the 'House search setup' issue template."
        )

    if base_profile is None:
        try:
            base_profile = load_profile()
        except (FileNotFoundError, json.JSONDecodeError):
            base_profile = {}
    profile = dict(base_profile)

    name = _value(sections, "your name", "name")
    if name:
        profile["name"] = name

    buy_areas = _areas(sections, "buy - areas to search", "buy - search radius")
    rent_areas = _areas(sections, "rent - areas to search", "rent - search radius")

    # Which modes run is decided purely by which sections were filled in -
    # supplying areas is what says "search for this".
    modes = []
    if buy_areas:
        modes.append("buy")
    if rent_areas:
        modes.append("rent")
    if modes:
        profile["modes_enabled"] = modes

    if buy_areas:
        buy = dict(profile.get("buy") or {})
        buy["search_areas"] = buy_areas
        buy["price"] = {
            "min": _int(sections, "buy - minimum price") or 0,
            "max": _int(sections, "buy - maximum price") or 0,
        }
        if (beds := _int(sections, "buy - minimum bedrooms")) is not None:
            buy["bedrooms_min"] = beds
        if (baths := _int(sections, "buy - minimum bathrooms")) is not None:
            buy["bathrooms_min"] = baths
        if types := _property_types(sections, "buy - property types"):
            buy["property_types"] = types
        if musts := _list(sections, "buy - must-haves"):
            buy["must_haves"] = musts
        if nices := _list(sections, "buy - nice-to-haves"):
            buy["nice_to_haves"] = nices
        if freetext := _value(sections, "buy - what are you looking for"):
            buy["preferences_freetext"] = freetext

        exclusions = dict(buy.get("exclusions") or {})
        ruled_out = _checkbox_list(sections, "buy - rule out")
        if ruled_out:
            exclusions["no_retirement_homes"] = any("retirement" in r for r in ruled_out)
            exclusions["no_shared_ownership"] = any("shared ownership" in r for r in ruled_out)
            exclusions["no_park_homes"] = any("park" in r or "mobile" in r for r in ruled_out)
            exclusions["no_auction"] = any("auction" in r for r in ruled_out)
            exclusions["no_new_build"] = any("new build" in r for r in ruled_out)
        if keywords := _list(sections, "buy - words that rule a property out"):
            exclusions["keyword_excludes"] = keywords
        if (lease := _int(sections, "buy - minimum lease years")) is not None:
            exclusions["no_leasehold_under_years"] = lease
        buy["exclusions"] = exclusions
        profile["buy"] = buy

    if rent_areas:
        rent = dict(profile.get("rent") or {})
        rent["search_areas"] = rent_areas
        rent["price_pcm"] = {
            "min": _int(sections, "rent - minimum rent") or 0,
            "max": _int(sections, "rent - maximum rent") or 0,
        }
        if (beds := _int(sections, "rent - minimum bedrooms")) is not None:
            rent["bedrooms_min"] = beds
        if types := _property_types(sections, "rent - property types"):
            rent["property_types"] = types
        if musts := _list(sections, "rent - must-haves"):
            rent["must_haves"] = musts
        if nices := _list(sections, "rent - nice-to-haves"):
            rent["nice_to_haves"] = nices
        if freetext := _value(sections, "rent - what are you looking for"):
            rent["preferences_freetext"] = freetext

        exclusions = dict(rent.get("exclusions") or {})
        ruled_out = _checkbox_list(sections, "rent - rule out")
        if ruled_out:
            exclusions["no_house_share"] = any("share" in r for r in ruled_out)
            exclusions["no_student_only"] = any("student" in r for r in ruled_out)
        if keywords := _list(sections, "rent - words that rule a property out"):
            exclusions["keyword_excludes"] = keywords
        rent["exclusions"] = exclusions
        profile["rent"] = rent

    if not buy_areas and not rent_areas:
        raise ValueError(
            "The form did not include any search areas. Fill in at least "
            "'Buy - areas to search' or 'Rent - areas to search'."
        )

    if write:
        PROFILE_PATH.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        logger.info("issue_profile: wrote %s", PROFILE_PATH)

    return profile
