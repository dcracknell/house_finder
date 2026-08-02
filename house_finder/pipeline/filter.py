"""Hard filters applied before ranking, to enforce criteria and save spend.

Everything here is a definite yes/no from the buyer's stated criteria. Soft
judgement ("is this a nice street?") belongs to the ranker. Anything genuinely
unknown is kept - a missing field is not evidence of a bad match, and the cost
of keeping one extra listing is a fraction of a penny.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

from house_finder.adapters.base import SERVER_SIDE_LOCATION_SOURCES
from house_finder.util.geocode import distance_miles

logger = logging.getLogger(__name__)

# Text signals for exclusions the portal filters cannot express.
_RETIREMENT = re.compile(
    r"\b(retirement (?:home|living|apartment|property|village)|over[\s-]?(?:55|60)s?|"
    r"assisted living|sheltered (?:housing|accommodation))\b",
    re.I,
)
_SHARED_OWNERSHIP = re.compile(
    r"\b(shared ownership|shared equity|part buy(?:\s*/\s*|\s+)part rent)\b", re.I
)
_PARK_HOME = re.compile(r"\b(park home|mobile home|static caravan|lodge park)\b", re.I)
_HOUSE_SHARE = re.compile(r"\b(house share|flat share|room in a|room to rent|hmo)\b", re.I)
_STUDENT_ONLY = re.compile(r"\b(students? only|student accommodation|student let)\b", re.I)
_AUCTION = re.compile(r"\b(for sale by (?:modern |traditional )?auction|auction only)\b", re.I)

# Features agents reliably advertise, so "must have" can be judged from the
# listing text. This is the one place a missing mention counts against a
# property: nobody sells a house with a garden without saying so.
_MUST_HAVE_PATTERNS = {
    "garden": re.compile(r"\b(garden|patio|yard|outdoor space)\b", re.I),
    "parking": re.compile(
        r"\b(parking|garage|driveway|car ?port|off[\s-]?street)\b", re.I
    ),
    "chain_free": re.compile(r"\b(no (?:onward )?chain|chain[\s-]?free)\b", re.I),
}

# "999 year lease", "left on the lease: 87 years", "87 years remaining"
_LEASE_YEARS = (
    re.compile(r"(\d{2,4})\s*(?:\+)?\s*years?\s+(?:remaining|left|unexpired)", re.I),
    re.compile(r"lease[^.]{0,40}?(\d{2,4})\s*years", re.I),
    re.compile(r"(\d{2,4})\s*year\s+lease", re.I),
)


def _listing_text(record) -> str:
    """All searchable text for one listing, lowercased."""
    return " ".join(
        [
            record.title or "",
            record.display_address or "",
            record.description or "",
            " ".join(record.key_features or []),
            record.property_subtype_raw or "",
        ]
    ).lower()


def extract_lease_years(text: str) -> int | None:
    """Best-effort remaining lease term from listing text."""
    for pattern in _LEASE_YEARS:
        m = pattern.search(text)
        if m:
            try:
                years = int(m.group(1))
            except (TypeError, ValueError):
                continue
            # Leases run to 999 years; anything larger is a misparse.
            if 1 <= years <= 1200:
                return years
    return None


def build_keyword_pattern(terms: list[str]) -> re.Pattern | None:
    """Word-boundary regex over user-supplied terms, or None if empty."""
    cleaned = [re.escape(t.strip()) for t in (terms or []) if t and t.strip()]
    if not cleaned:
        return None
    return re.compile(r"(?:^|\W)(?:" + "|".join(cleaned) + r")(?:\W|$)", re.I)


def _cooldown_urls(conn: sqlite3.Connection, cooldown_days: int, today: date) -> set[str]:
    """property_ids the user rejected recently.

    A rejected property that comes back under a new listing (chain collapse,
    new agent) should stay out of the way for a while rather than reappearing
    at the top of the workbook.
    """
    if cooldown_days <= 0:
        return set()
    cutoff = (today - timedelta(days=cooldown_days)).isoformat()
    try:
        rows = conn.execute(
            "SELECT property_id FROM properties WHERE status = 'rejected' AND last_seen >= ?",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {row["property_id"] for row in rows}


def apply_filters(
    records: list,
    criteria: dict,
    conn: sqlite3.Connection | None = None,
    *,
    listing_type: str = "sale",
    home_coords: tuple[float, float] | None = None,
    cooldown_days: int = 90,
    today: date | None = None,
) -> list:
    """Return the records that pass every configured hard filter."""
    today = today or date.today()
    exclusions = criteria.get("exclusions") or {}

    price_key = "price_pcm" if listing_type == "rent" else "price"
    price_range = criteria.get(price_key) or {}
    price_min = price_range.get("min") or 0
    price_max = price_range.get("max") or 0

    bedrooms_min = criteria.get("bedrooms_min")
    bedrooms_max = criteria.get("bedrooms_max")
    bathrooms_min = criteria.get("bathrooms_min")
    bathrooms_max = criteria.get("bathrooms_max")
    allowed_types = set(criteria.get("property_types") or [])
    allowed_tenures = set(criteria.get("tenure_types") or [])
    max_days = criteria.get("max_days_since_listed")
    min_lease = exclusions.get("no_leasehold_under_years")
    size_min = criteria.get("min_size_sqft")
    size_max = criteria.get("max_size_sqft")
    must_have_features = [
        f for f in (criteria.get("must_have_features") or []) if f in _MUST_HAVE_PATTERNS
    ]

    keyword_pattern = build_keyword_pattern(exclusions.get("keyword_excludes") or [])
    include_pattern = build_keyword_pattern(criteria.get("keyword_includes") or [])
    cooldown = _cooldown_urls(conn, cooldown_days, today) if conn is not None else set()

    # Radius is enforced server-side by the portal, so this only catches
    # sources that cannot filter by location themselves (agent pages).
    radius_areas = [
        a for a in (criteria.get("search_areas") or []) if a.get("radius_miles")
    ]
    max_radius = max((a["radius_miles"] for a in radius_areas), default=None)

    kept = []
    drops: Counter[str] = Counter()

    for record in records:
        text = _listing_text(record)

        if record.price is not None:
            if price_max and record.price > price_max:
                drops["over_budget"] += 1
                continue
            if price_min and record.price < price_min:
                drops["under_price_floor"] += 1
                continue
        elif price_max:
            # POA with a budget set: keep it, the ranker can weigh it. Only
            # drop when there is no price AND no description to judge from.
            if not record.description:
                drops["no_price_no_detail"] += 1
                continue

        if bedrooms_min is not None and record.bedrooms is not None:
            if record.bedrooms < bedrooms_min:
                drops["too_few_bedrooms"] += 1
                continue
        if bedrooms_max is not None and record.bedrooms is not None:
            if record.bedrooms > bedrooms_max:
                drops["too_many_bedrooms"] += 1
                continue
        if bathrooms_min is not None and record.bathrooms is not None:
            if record.bathrooms < bathrooms_min:
                drops["too_few_bathrooms"] += 1
                continue
        if bathrooms_max is not None and record.bathrooms is not None:
            if record.bathrooms > bathrooms_max:
                drops["too_many_bathrooms"] += 1
                continue

        # Floor area is absent from most listings, so only judge the ones
        # that state it rather than discarding everything unmeasured.
        if record.floor_area_sqft is not None:
            if size_min and record.floor_area_sqft < size_min:
                drops["too_small"] += 1
                continue
            if size_max and record.floor_area_sqft > size_max:
                drops["too_large"] += 1
                continue

        # Rightmove applies tenure server-side, so this only bites on sources
        # that cannot. Unknown tenure is kept: most listings never state it.
        if allowed_tenures and record.tenure and record.tenure != "unknown":
            if record.tenure not in allowed_tenures:
                drops["wrong_tenure"] += 1
                continue

        if allowed_types and record.property_type not in allowed_types:
            # 'other' means we could not classify it, not that it is wrong.
            if record.property_type != "other":
                drops["wrong_property_type"] += 1
                continue

        if max_days and record.first_listed_date:
            if (today - record.first_listed_date).days > max_days:
                drops["listed_too_long_ago"] += 1
                continue

        if listing_type == "sale":
            if not criteria.get("include_sold_stc", False) and record.listing_status in {
                "sold_stc",
                "under_offer",
            }:
                drops["sold_stc"] += 1
                continue
            if exclusions.get("no_auction") and (record.auction or _AUCTION.search(text)):
                drops["auction"] += 1
                continue
            if exclusions.get("no_retirement_homes") and _RETIREMENT.search(text):
                drops["retirement_home"] += 1
                continue
            if exclusions.get("no_shared_ownership") and _SHARED_OWNERSHIP.search(text):
                drops["shared_ownership"] += 1
                continue
            if exclusions.get("no_park_homes") and _PARK_HOME.search(text):
                drops["park_home"] += 1
                continue
            if min_lease and record.tenure == "leasehold":
                years = record.leasehold_years_remaining or extract_lease_years(text)
                if years is not None:
                    record.leasehold_years_remaining = years
                    if years < min_lease:
                        drops["lease_too_short"] += 1
                        continue
                # Unknown lease length is kept deliberately: most listings omit
                # it, and dropping them would discard most flats sight-unseen.
        else:
            if not criteria.get("include_let_agreed", False) and record.listing_status == (
                "let_agreed"
            ):
                drops["let_agreed"] += 1
                continue
            if exclusions.get("no_house_share") and (
                record.property_type == "house_share" or _HOUSE_SHARE.search(text)
            ):
                drops["house_share"] += 1
                continue
            if exclusions.get("no_student_only") and _STUDENT_ONLY.search(text):
                drops["student_only"] += 1
                continue

        if keyword_pattern is not None and keyword_pattern.search(text):
            drops["excluded_keyword"] += 1
            continue

        if include_pattern is not None and not include_pattern.search(text):
            drops["missing_required_keyword"] += 1
            continue

        missing_feature = next(
            (f for f in must_have_features if not _MUST_HAVE_PATTERNS[f].search(text)),
            None,
        )
        if missing_feature:
            drops[f"missing_{missing_feature}"] += 1
            continue

        if record.property_id in cooldown:
            drops["previously_rejected"] += 1
            continue

        # Only sources that cannot filter by location themselves (an estate
        # agent's own page lists everything they have, anywhere) get a local
        # distance check. Portals already applied the radius server-side.
        if (
            max_radius
            and home_coords
            and record.lat is not None
            and record.lon is not None
            and record.source.split(":")[0] not in SERVER_SIDE_LOCATION_SOURCES
        ):
            distance = distance_miles(home_coords[0], home_coords[1], record.lat, record.lon)
            if distance > max_radius * 1.5:
                drops["outside_radius"] += 1
                continue

        kept.append(record)

    if drops:
        logger.info(
            "filter: dropped %d/%d (%s)",
            sum(drops.values()),
            len(records),
            ", ".join(f"{reason}: {n}" for reason, n in drops.most_common()),
        )
    return kept
