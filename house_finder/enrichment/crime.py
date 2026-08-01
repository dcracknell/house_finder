"""Street-level crime from data.police.uk - free, no key, no registration.

Answers a question no listing ever does: what is actually reported around this
address. Results are counts of reported incidents, not a safety verdict; small
differences between areas are noise, and the ranker is told to treat it as
context rather than a hard signal.

The API's radius is fixed at roughly one mile around the given point, so a
city-centre property will always look worse than a village one simply because
a mile of city contains more of everything. Compare like with like.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from house_finder.util import http

logger = logging.getLogger(__name__)

BASE_URL = "https://data.police.uk/api/crimes-street/all-crime"


def _month_stamps(months_back: int) -> list[str]:
    """Recent YYYY-MM stamps. The API lags ~2 months, so start there."""
    stamps = []
    cursor = date.today().replace(day=1) - timedelta(days=60)
    for _ in range(max(1, months_back)):
        stamps.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    return stamps


def _crimes_for_month(lat: float, lon: float, month: str) -> int | None:
    resp = http.get_once(
        BASE_URL,
        params={"lat": f"{lat:.5f}", "lng": f"{lon:.5f}", "date": month},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp is None:
        return None
    try:
        return len(resp.json() or [])
    except ValueError:
        return None


def enrich(record, config: dict, *, sample_months: int = 3) -> bool:
    """Attach a nearby-crime count, extrapolated to 12 months.

    Only `sample_months` requests are made per property rather than 12 - the
    API is one call per month and the estimate is only ever used as rough
    context.
    """
    if record.lat is None or record.lon is None:
        return False

    months_back = int(config.get("months_back", 12))
    stamps = _month_stamps(months_back)[:sample_months]

    counts = [c for m in stamps if (c := _crimes_for_month(record.lat, record.lon, m)) is not None]
    if not counts:
        return False

    monthly_average = sum(counts) / len(counts)
    record.crime_incidents_nearby = int(round(monthly_average * months_back))
    logger.debug(
        "crime: %s - ~%d incidents/%d months",
        record.display_address, record.crime_incidents_nearby, months_back,
    )
    return True


def healthcheck() -> tuple[bool, str | None]:
    month = _month_stamps(1)[0]
    resp = http.get_once(
        BASE_URL,
        params={"lat": "53.38", "lng": "-1.47", "date": month},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp is None:
        return False, "data.police.uk unreachable"
    try:
        resp.json()
    except ValueError:
        return False, "data.police.uk returned invalid JSON"
    return True, None
