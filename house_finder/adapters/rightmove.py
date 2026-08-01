"""Rightmove adapter - the primary source of live listings.

Rightmove publishes no search API. Its search results pages are server-rendered
Next.js, which embeds the full result set as JSON in a <script id="__NEXT_DATA__">
tag, so one plain HTTP GET per page returns structured data with no browser and
no HTML parsing. Cards carry coordinates, tenure, floor area and key features,
so no per-listing detail request is needed.

See README "Legal and reliability note" - this is scraping, and the politeness
settings in config/sources.yaml exist to keep it obviously personal-scale.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from house_finder.adapters.base import Adapter, PropertyRecord, RawListing
from house_finder.search.areas import resolve_location
from house_finder.util import http
from house_finder.util.dates import parse_date
from house_finder.util.geocode import extract_postcode
from house_finder.util.price import parse_floor_area_sqft, parse_rent_pcm

logger = logging.getLogger(__name__)

BASE_BUY = "https://www.rightmove.co.uk/property-for-sale/find.html"
BASE_RENT = "https://www.rightmove.co.uk/property-to-rent/find.html"
LISTING_BASE = "https://www.rightmove.co.uk"

RESULTS_PER_PAGE = 24

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)

# Rightmove's propertySubType wording -> our canonical types.
_SUBTYPE_MAP = {
    "detached": "detached",
    "detached house": "detached",
    "detached bungalow": "bungalow",
    "semi-detached": "semi_detached",
    "semi-detached house": "semi_detached",
    "semi-detached bungalow": "bungalow",
    "terraced": "terraced",
    "terraced house": "terraced",
    "end of terrace": "terraced",
    "end terrace house": "terraced",
    "mews": "terraced",
    "town house": "terraced",
    "link detached house": "detached",
    "flat": "flat",
    "apartment": "flat",
    "ground flat": "flat",
    "maisonette": "flat",
    "penthouse": "flat",
    "duplex": "flat",
    "studio": "flat",
    "bungalow": "bungalow",
    "land": "land",
    "plot": "land",
    "house share": "house_share",
    "flat share": "house_share",
    "room": "house_share",
}

# Our canonical types -> Rightmove's propertyTypes query values, so the filter
# is applied server-side and we never download stock we would only discard.
_TYPE_TO_QUERY = {
    "detached": "detached",
    "semi_detached": "semi-detached",
    "terraced": "terraced",
    "flat": "flat",
    "bungalow": "bungalow",
    "land": "land",
}

# Rightmove dontShow tokens.
_DONTSHOW = {
    "no_retirement_homes": "retirement",
    "no_shared_ownership": "sharedOwnership",
    "no_new_build": "newHome",
}


class RightmoveAdapter(Adapter):
    name = "rightmove"

    def __init__(self, config: dict | None = None):
        config = config or {}
        self.delay = float(config.get("request_delay_seconds", 1.5))
        self.max_results_per_area = int(config.get("max_results_per_area", 120))
        self.sort_type = int(config.get("sort_type", 6))  # 6 = newest first

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        """Fetch raw listing cards for every configured area."""
        raw: list[RawListing] = []
        for area in areas:
            try:
                location_id = resolve_location(
                    area["postcode_or_place"], delay_seconds=self.delay
                )
            except Exception as exc:  # noqa: BLE001 - one bad area must not kill the run
                logger.error(
                    "rightmove: could not resolve area %r: %s",
                    area.get("postcode_or_place"), exc,
                )
                continue

            found = self._fetch_area(location_id, area, criteria, listing_type)
            logger.info(
                "rightmove: %s - %d listings for %s",
                area["label"], len(found), listing_type,
            )
            raw.extend(found)
        return raw

    def _fetch_area(
        self, location_id: str, area: dict, criteria: dict, listing_type: str
    ) -> list[RawListing]:
        base = BASE_RENT if listing_type == "rent" else BASE_BUY
        params = self._build_params(location_id, area, criteria, listing_type)

        collected: list[RawListing] = []
        index = 0
        total = None

        while len(collected) < self.max_results_per_area:
            params["index"] = index
            http.polite_delay("www.rightmove.co.uk", self.delay)
            resp = http.get(base, params=params)
            payload = self._extract_next_data(resp.text)
            if payload is None:
                logger.error(
                    "rightmove: no __NEXT_DATA__ in response for %s - the page "
                    "structure may have changed. Run `house-finder health`.",
                    area["label"],
                )
                break

            results = (
                payload.get("props", {}).get("pageProps", {}).get("searchResults") or {}
            )
            properties = results.get("properties") or []
            if total is None:
                total = self._parse_result_count(results.get("resultCount"))
                if total is not None:
                    logger.debug(
                        "rightmove: %s has %d total results", area["label"], total
                    )

            if not properties:
                break

            for prop in properties:
                prop["_area_label"] = area["label"]
                prop["_listing_type"] = listing_type
                collected.append(prop)

            index += RESULTS_PER_PAGE
            if total is not None and index >= total:
                break
            # Rightmove caps paging at 1000 results.
            if index >= 1000:
                break

        return collected[: self.max_results_per_area]

    def _build_params(
        self, location_id: str, area: dict, criteria: dict, listing_type: str
    ) -> dict:
        """Translate profile criteria into Rightmove query parameters.

        Everything expressible server-side is pushed server-side: it is both
        politer (fewer pages fetched) and cheaper (nothing downloaded only to
        be discarded by the local filter).
        """
        params: dict = {
            "locationIdentifier": location_id,
            "radius": area.get("radius_miles", 3.0),
            "sortType": self.sort_type,
            "index": 0,
            "channel": "RENT" if listing_type == "rent" else "BUY",
        }

        price = criteria.get("price_pcm" if listing_type == "rent" else "price") or {}
        if price.get("min"):
            params["minPrice"] = int(price["min"])
        if price.get("max"):
            params["maxPrice"] = int(price["max"])

        if criteria.get("bedrooms_min") is not None:
            params["minBedrooms"] = int(criteria["bedrooms_min"])
        if criteria.get("bedrooms_max") is not None:
            params["maxBedrooms"] = int(criteria["bedrooms_max"])

        types = [
            _TYPE_TO_QUERY[t]
            for t in (criteria.get("property_types") or [])
            if t in _TYPE_TO_QUERY
        ]
        if types:
            params["propertyTypes"] = ",".join(sorted(set(types)))

        max_days = criteria.get("max_days_since_listed")
        if max_days:
            # Rightmove only accepts these buckets; round up to the nearest.
            for bucket in (1, 3, 7, 14):
                if max_days <= bucket:
                    params["maxDaysSinceAdded"] = bucket
                    break

        exclusions = criteria.get("exclusions") or {}
        dont_show = [token for key, token in _DONTSHOW.items() if exclusions.get(key)]
        if dont_show:
            params["dontShow"] = ",".join(dont_show)

        if listing_type == "rent":
            furnished = (criteria.get("furnished") or "any").lower()
            if furnished in {"furnished", "unfurnished", "partFurnished", "part_furnished"}:
                params["furnishTypes"] = (
                    "partFurnished" if furnished.startswith("part") else furnished
                )
            if not criteria.get("include_let_agreed", False):
                params["includeLetAgreed"] = "false"
        else:
            if not criteria.get("include_sold_stc", False):
                params["includeSSTC"] = "false"

        return params

    @staticmethod
    def _extract_next_data(html: str) -> dict | None:
        match = _NEXT_DATA.search(html)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            logger.error("rightmove: __NEXT_DATA__ was not valid JSON: %s", exc)
            return None

    @staticmethod
    def _parse_result_count(value) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Normalise
    # ------------------------------------------------------------------

    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        listing_type = raw.get("_listing_type") or (
            "rent" if str(raw.get("transactionType", "")).lower() == "rent" else "sale"
        )

        property_url = raw.get("propertyUrl") or ""
        if not property_url:
            return None
        # Strip Rightmove's "#/?channel=..." fragment so the URL - which the
        # property_id is derived from - stays stable across channels and runs.
        clean_url = property_url.split("#")[0]
        url = f"{LISTING_BASE}{clean_url}" if clean_url.startswith("/") else clean_url

        address = (raw.get("displayAddress") or "").strip()
        postcode, outcode = extract_postcode(address)

        location = raw.get("location") or {}
        lat = location.get("latitude")
        lon = location.get("longitude")

        price_block = raw.get("price") or {}
        amount = price_block.get("amount")
        display_prices = price_block.get("displayPrices") or []
        qualifier = None
        if display_prices:
            qualifier = (display_prices[0].get("displayPriceQualifier") or "").strip() or None

        if listing_type == "rent":
            price = parse_rent_pcm(amount, price_block.get("frequency"))
            qualifier = qualifier or "pcm"
        else:
            price = int(amount) if amount else None

        key_features = [
            f.get("description", "").strip()
            for f in (raw.get("keyFeatures") or [])
            if isinstance(f, dict) and f.get("description")
        ]

        listing_update = raw.get("listingUpdate") or {}
        update_reason = (listing_update.get("listingUpdateReason") or "").lower()

        display_status = (raw.get("displayStatus") or "").strip().lower()
        listing_status = "available"
        if "sold" in display_status or "stc" in display_status:
            listing_status = "sold_stc"
        elif "let agreed" in display_status:
            listing_status = "let_agreed"
        elif "under offer" in display_status:
            listing_status = "under_offer"

        tenure_block = raw.get("tenure") or {}
        tenure = self._map_tenure(tenure_block.get("tenureType"))

        customer = raw.get("customer") or {}
        images = (raw.get("propertyImages") or {}).get("images") or raw.get("images") or []
        image_url = None
        if images and isinstance(images[0], dict):
            image_url = images[0].get("srcUrl") or images[0].get("url")

        record = PropertyRecord(
            property_id=PropertyRecord.make_property_id(self.name, url),
            source=self.name,
            listing_type=listing_type,
            title=(raw.get("propertyTypeFullDescription") or "").strip()
            or f"{raw.get('bedrooms') or '?'} bed {raw.get('propertySubType') or 'property'}",
            display_address=address,
            url=url,
            property_type=self._map_property_type(raw.get("propertySubType")),
            property_subtype_raw=raw.get("propertySubType"),
            postcode=postcode,
            outcode=outcode,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            bedrooms=self._as_int(raw.get("bedrooms")),
            bathrooms=self._as_int(raw.get("bathrooms")),
            floor_area_sqft=parse_floor_area_sqft(raw.get("displaySize")),
            tenure=tenure,
            description=(raw.get("summary") or "").strip(),
            key_features=key_features,
            agent_name=(
                customer.get("branchDisplayName") or customer.get("brandTradingName")
            ),
            image_url=image_url,
            image_count=self._as_int(raw.get("numberOfImages")) or 0,
            price=price,
            price_qualifier=qualifier,
            price_reduced=update_reason == "price_reduced",
            auction=bool(raw.get("auction")),
            first_listed_date=parse_date(raw.get("firstVisibleDate")),
            last_update_date=parse_date(listing_update.get("listingUpdateDate")),
            let_available_date=parse_date(raw.get("letAvailableDate")),
            listing_status=listing_status,
            matched_area=raw.get("_area_label"),
            first_seen=date.today(),
            last_seen=date.today(),
        )
        return record

    @staticmethod
    def _as_int(value) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _map_property_type(subtype: str | None) -> str:
        if not subtype:
            return "other"
        key = subtype.strip().lower()
        if key in _SUBTYPE_MAP:
            return _SUBTYPE_MAP[key]
        # Fall back to substring matching - Rightmove has a long tail of
        # subtypes ("Character Property", "Barn Conversion", ...).
        for needle, canonical in _SUBTYPE_MAP.items():
            if needle in key:
                return canonical
        return "other"

    @staticmethod
    def _map_tenure(tenure_type: str | None) -> str | None:
        if not tenure_type:
            return None
        key = tenure_type.strip().lower()
        if "freehold" in key and "share" in key:
            return "share_of_freehold"
        if "freehold" in key:
            return "freehold"
        if "leasehold" in key:
            return "leasehold"
        if key in {"na", "not_specified", "notspecified"}:
            return None
        return "unknown"

    # ------------------------------------------------------------------

    def healthcheck(self) -> tuple[bool, str | None]:
        """Cheap probe: one small search that must yield parseable results."""
        try:
            location_id = resolve_location("Sheffield", delay_seconds=self.delay)
            http.polite_delay("www.rightmove.co.uk", self.delay)
            resp = http.get(
                BASE_BUY,
                params={
                    "locationIdentifier": location_id,
                    "radius": 1.0,
                    "sortType": 6,
                    "index": 0,
                    "channel": "BUY",
                },
            )
            payload = self._extract_next_data(resp.text)
            if payload is None:
                return False, (
                    "__NEXT_DATA__ not found - Rightmove's page structure has "
                    "likely changed and adapters/rightmove.py needs updating"
                )
            results = payload.get("props", {}).get("pageProps", {}).get("searchResults")
            if not results or not results.get("properties"):
                return False, "search returned no properties - structure may have changed"
            return True, None
        except Exception as exc:  # noqa: BLE001 - healthcheck reports any failure
            return False, str(exc)
