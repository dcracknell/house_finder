"""HM Land Registry Price Paid Data - free, official, no API key.

Gives what a listing never tells you: what nearby properties actually SOLD for.
Used to flag asking prices that sit well above local reality.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date

from house_finder.util import http
from house_finder.util.dates import parse_date

logger = logging.getLogger(__name__)

BASE_URL = "http://landregistry.data.gov.uk/data/ppi/transaction-record.json"

# Land Registry property type codes -> our canonical types.
_TYPE_MAP = {
    "detached": "detached",
    "semi-detached": "semi_detached",
    "terraced": "terraced",
    "flat-maisonette": "flat",
}


def _property_type(item: dict) -> str | None:
    """Map a transaction's property type onto our canonical vocabulary.

    The API nests this as {"_about": ".../def/common/terraced", "prefLabel":
    [{"_value": "terraced"}]}. The URI suffix is the most stable part.
    """
    node = item.get("propertyType")
    if not isinstance(node, dict):
        return None
    about = str(node.get("_about") or "")
    if about:
        return _TYPE_MAP.get(about.rsplit("/", 1)[-1].lower())

    labels = node.get("prefLabel") or node.get("label") or []
    if isinstance(labels, list) and labels:
        first = labels[0]
        value = first.get("_value") if isinstance(first, dict) else first
        return _TYPE_MAP.get(str(value or "").lower())
    return None


def _sold_prices(outcode_or_postcode: str, lookback_years: int, limit: int = 100) -> list[dict]:
    """Fetch recent sold transactions for a postcode."""
    cutoff = date(date.today().year - lookback_years, date.today().month, 1)
    resp = http.get_once(
        BASE_URL,
        params={
            "propertyAddress.postcode": outcode_or_postcode.upper(),
            "_pageSize": limit,
            "_sort": "-transactionDate",
        },
        headers={"Accept": "application/json"},
        timeout=25,
    )
    if resp is None:
        return []
    try:
        items = (resp.json().get("result") or {}).get("items") or []
    except ValueError:
        return []

    sales = []
    for item in items:
        try:
            amount = int(item.get("pricePaid"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue

        # Dates arrive as "Fri, 11 Jul 2025" rather than ISO.
        sold_on = parse_date(item.get("transactionDate"))
        if sold_on is None or sold_on < cutoff:
            continue

        sales.append(
            {"price": amount, "date": sold_on, "property_type": _property_type(item)}
        )
    return sales


def enrich(record, config: dict) -> bool:
    """Attach local sold-price context to a record. Returns True if enriched.

    Rentals are skipped - sold prices say nothing useful about rent levels.
    """
    if record.listing_type != "sale":
        return False
    if not record.postcode and not record.outcode:
        return False
    if not record.price:
        return False

    lookback = int(config.get("lookback_years", 3))
    min_comparables = int(config.get("min_comparables", 3))

    # This API only matches a FULL postcode - an outcode ("S12") returns
    # nothing, so there is no point trying one.
    if not record.postcode:
        return False

    sales = _sold_prices(record.postcode, lookback)

    # A quiet street may have too few recent sales. Widening the window helps,
    # but only a little: comparing today's asking price against a sale from a
    # decade ago says more about house-price inflation than about this
    # property, so the window is capped rather than opened indefinitely.
    if len(sales) < min_comparables:
        widened = min(lookback * 2, 6)
        if widened > lookback:
            sales = _sold_prices(record.postcode, widened)

    if len(sales) < min_comparables:
        return False

    # Same-type comparables when there are enough of them; a flat's price says
    # little about a detached house on the same street.
    same_type = [s for s in sales if s["property_type"] == record.property_type]
    comparables = same_type if len(same_type) >= min_comparables else sales

    prices = [s["price"] for s in comparables]
    median = int(statistics.median(prices))

    record.local_sold_avg_price = median
    record.local_sold_sample_size = len(prices)
    record.price_vs_local_pct = round((record.price - median) / median * 100, 1)

    logger.debug(
        "land_registry: %s - asking £%s vs local median £%s (%+.0f%%, n=%d)",
        record.display_address, f"{record.price:,}", f"{median:,}",
        record.price_vs_local_pct, len(prices),
    )
    return True


def healthcheck() -> tuple[bool, str | None]:
    resp = http.get_once(
        BASE_URL,
        params={"propertyAddress.postcode": "S1 2HE", "_pageSize": 1},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp is None:
        return False, "Land Registry API unreachable"
    try:
        resp.json()
    except ValueError:
        return False, "Land Registry API returned invalid JSON"
    return True, None
