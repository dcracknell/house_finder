"""Generic estate-agent page adapter (schema.org JSON-LD).

Independent agents often list a property on their own site before it reaches
the portals - sometimes days earlier, sometimes never. Many publish structured
JSON-LD for Google, which is a documented, machine-readable format intended to
be read by software.

Add agents under `custom_pages:` in config/sources.yaml. Coverage varies by
site; when an agent publishes no structured data, nothing is returned rather
than guessing from HTML.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from house_finder.adapters.base import Adapter, PropertyRecord, RawListing
from house_finder.util import http
from house_finder.util.dates import parse_date
from house_finder.util.geocode import extract_postcode
from house_finder.util.price import parse_floor_area_sqft, parse_price

logger = logging.getLogger(__name__)

# schema.org types that represent a property listing.
_LISTING_TYPES = {
    "realestatelisting",
    "singlefamilyresidence",
    "apartment",
    "house",
    "residence",
    "accommodation",
    "product",
    "offer",
}

_BED_RE = re.compile(r"(\d+)\s*(?:bed|bedroom)", re.I)
_BATH_RE = re.compile(r"(\d+)\s*(?:bath|bathroom)", re.I)


class AgentPageAdapter(Adapter):
    """Reads one configured estate-agent page."""

    def __init__(self, agent_name: str, url: str, delay_seconds: float = 2.0):
        self.agent_name = agent_name
        self.url = url
        self.delay = delay_seconds
        slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
        self.name = f"agent_page:{slug}"

    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.url)
        http.polite_delay(host, self.delay)
        resp = http.get_once(self.url)
        if resp is None:
            logger.warning("agent_page: could not fetch %s", self.url)
            return []

        blocks = self._extract_jsonld(resp.text)
        listings = []
        for block in blocks:
            for node in self._walk(block):
                if self._is_listing(node):
                    node["_listing_type"] = listing_type
                    node["_agent_name"] = self.agent_name
                    node["_source_url"] = self.url
                    listings.append(node)

        logger.info("agent_page: %s - %d listings", self.agent_name, len(listings))
        return listings

    @staticmethod
    def _extract_jsonld(html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = tag.string or tag.get_text() or ""
            if not text.strip():
                continue
            try:
                blocks.append(json.loads(text))
            except json.JSONDecodeError:
                continue
        return blocks

    @staticmethod
    def _walk(node, depth: int = 0):
        """Yield every dict in a nested JSON-LD structure."""
        if depth > 6:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from AgentPageAdapter._walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                yield from AgentPageAdapter._walk(item, depth + 1)

    @staticmethod
    def _is_listing(node: dict) -> bool:
        node_type = node.get("@type")
        if isinstance(node_type, list):
            types = {str(t).lower() for t in node_type}
        else:
            types = {str(node_type).lower()} if node_type else set()
        if not types & _LISTING_TYPES:
            return False
        # A bare Product/Offer is only a listing if it looks like property.
        if types & {"product", "offer"} and not (
            node.get("address") or node.get("numberOfRooms") or node.get("floorSize")
        ):
            return False
        return bool(node.get("name") or node.get("address"))

    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        url = raw.get("url") or raw.get("@id") or raw.get("_source_url")
        if not url:
            return None
        if url.startswith("/"):
            base = re.match(r"^(https?://[^/]+)", raw.get("_source_url", "") or "")
            if base:
                url = base.group(1) + url

        address = self._format_address(raw.get("address"))
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        postcode, outcode = extract_postcode(address or description)

        price, qualifier = self._extract_price(raw)
        beds = self._extract_int(raw.get("numberOfBedrooms")) or self._search_int(
            _BED_RE, f"{name} {description}"
        )
        baths = self._extract_int(raw.get("numberOfBathroomsTotal")) or self._search_int(
            _BATH_RE, f"{name} {description}"
        )

        geo = raw.get("geo") or {}
        lat = geo.get("latitude")
        lon = geo.get("longitude")

        floor_size = raw.get("floorSize")
        size_text = None
        if isinstance(floor_size, dict):
            unit = floor_size.get("unitCode") or floor_size.get("unitText") or ""
            size_text = f"{floor_size.get('value', '')} {unit}"
        elif floor_size:
            size_text = str(floor_size)

        listing_type = raw.get("_listing_type", "sale")

        return PropertyRecord(
            property_id=PropertyRecord.make_property_id(self.name, url),
            source=self.name,
            listing_type=listing_type,
            title=name or address or "Property",
            display_address=address or name,
            url=url,
            property_type=self._map_type(f"{name} {raw.get('@type', '')}"),
            property_subtype_raw=str(raw.get("@type") or ""),
            postcode=postcode,
            outcode=outcode,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            bedrooms=beds,
            bathrooms=baths,
            floor_area_sqft=parse_floor_area_sqft(size_text),
            description=description,
            agent_name=raw.get("_agent_name"),
            price=price,
            price_qualifier=qualifier,
            first_listed_date=parse_date(raw.get("datePosted") or raw.get("datePublished")),
            matched_area=raw.get("_agent_name"),
            first_seen=date.today(),
            last_seen=date.today(),
        )

    @staticmethod
    def _format_address(address) -> str:
        if not address:
            return ""
        if isinstance(address, str):
            return address.strip()
        if isinstance(address, dict):
            parts = [
                address.get("streetAddress"),
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("postalCode"),
            ]
            return ", ".join(str(p).strip() for p in parts if p)
        return ""

    @staticmethod
    def _extract_price(raw: dict) -> tuple[int | None, str | None]:
        offers = raw.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            value = offers.get("price") or offers.get("lowPrice")
            if value is not None:
                try:
                    return int(float(str(value).replace(",", "").replace("£", ""))), None
                except (TypeError, ValueError):
                    pass
        for field in ("price", "name", "description"):
            value = raw.get(field)
            if isinstance(value, str) and "£" in value:
                amount, qualifier = parse_price(value)
                if amount:
                    return amount, qualifier
        return None, None

    @staticmethod
    def _extract_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get("value")
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _search_int(pattern: re.Pattern, text: str) -> int | None:
        m = pattern.search(text or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _map_type(text: str) -> str:
        lowered = (text or "").lower()
        for needle, canonical in (
            ("semi-detached", "semi_detached"),
            ("semi detached", "semi_detached"),
            ("detached", "detached"),
            ("terrace", "terraced"),
            ("bungalow", "bungalow"),
            ("apartment", "flat"),
            ("flat", "flat"),
            ("maisonette", "flat"),
            ("land", "land"),
            ("plot", "land"),
        ):
            if needle in lowered:
                return canonical
        return "other"

    def healthcheck(self) -> tuple[bool, str | None]:
        resp = http.get_once(self.url)
        if resp is None:
            return False, f"could not fetch {self.url}"
        if not self._extract_jsonld(resp.text):
            return False, f"{self.url} publishes no JSON-LD structured data"
        return True, None
