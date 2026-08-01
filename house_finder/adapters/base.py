"""Adapter ABC and the shared property data model."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Raw response from an adapter - shape varies per source.
RawListing = dict[str, Any]

# Canonical property types. Adapters map their own vocabulary onto these so
# profile.json can use one consistent set of names.
PROPERTY_TYPES = (
    "detached",
    "semi_detached",
    "terraced",
    "flat",
    "bungalow",
    "land",
    "house_share",
    "other",
)

# Sources that apply the search radius themselves, server-side. Their own
# definition of an area is authoritative, so re-checking distance locally
# against a single geocoded centre point would wrongly discard valid results
# (a portal "region" is not a circle).
SERVER_SIDE_LOCATION_SOURCES = frozenset({"rightmove"})

# Workbook/DB status values. 'new' is assigned on first sight; every other
# value is user-owned and the pipeline must never overwrite it.
STATUSES = (
    "new",
    "interested",
    "viewing_booked",
    "viewed",
    "offer_made",
    "offer_accepted",
    "rejected",
    "sold_stc",
    "withdrawn",
)


@dataclass
class PropertyRecord:
    """Normalised property listing - the common schema every adapter produces."""

    # --- Identity ---
    property_id: str  # sha1(source + canonical_url) - see make_property_id()
    source: str  # e.g. "rightmove", "agent_page:some-local-agent"
    listing_type: str  # "sale" | "rent"

    # --- Listing ---
    title: str  # e.g. "3 bedroom detached house"
    display_address: str
    url: str
    property_type: str  # one of PROPERTY_TYPES
    property_subtype_raw: str | None = None  # source's own wording
    postcode: str | None = None
    outcode: str | None = None
    lat: float | None = None
    lon: float | None = None

    bedrooms: int | None = None
    bathrooms: int | None = None
    floor_area_sqft: float | None = None
    tenure: str | None = None  # freehold|leasehold|share_of_freehold|unknown
    leasehold_years_remaining: int | None = None
    epc_rating: str | None = None
    furnished: str | None = None  # rent only
    description: str = ""
    key_features: list[str] = field(default_factory=list)
    agent_name: str | None = None
    image_url: str | None = None
    image_count: int = 0

    # --- Price ---
    price: int | None = None  # sale: asking price. rent: pcm.
    price_qualifier: str | None = None  # "Guide Price", "Offers in Region of", "pcm"...
    price_reduced: bool = False
    auction: bool = False

    # --- Dates / status ---
    first_listed_date: date | None = None
    last_update_date: date | None = None
    let_available_date: date | None = None  # rent only
    listing_status: str = "available"  # available|sold_stc|let_agreed|under_offer

    # --- Ranking (filled in later stages) ---
    fit_score: float | None = None
    fit_reason: str | None = None
    fit_confidence: float | None = None
    matched_criteria: list[str] = field(default_factory=list)
    ranker_version: str | None = None
    # True when this run produced a score worth persisting for an existing row.
    # Not stored in the DB.
    freshly_ranked: bool = False

    # --- Enrichment (public-record data the listing itself does not tell you) ---
    local_sold_avg_price: int | None = None
    local_sold_sample_size: int | None = None
    price_vs_local_pct: float | None = None  # +12.0 => 12% above local comparables
    crime_incidents_nearby: int | None = None
    flood_warnings_nearby: int | None = None
    epc_current: int | None = None  # efficiency score 1-100
    epc_potential: int | None = None
    broadband_max_mbps: float | None = None

    # --- Provenance ---
    matched_area: str | None = None  # which configured search area found this
    first_seen: date = field(default_factory=date.today)
    last_seen: date = field(default_factory=date.today)
    content_hash: str | None = None

    @staticmethod
    def make_property_id(source: str, url: str) -> str:
        """Compute a stable property_id.

        Deliberately keyed on source + canonical URL only. Address text and
        price both change while a listing is live (agents re-word addresses,
        prices get reduced); the URL does not. Changing this needs a migration.
        """
        return hashlib.sha1(f"{source.lower()}|{url.lower()}".encode()).hexdigest()

    @property
    def is_rent(self) -> bool:
        return self.listing_type == "rent"


class Adapter(ABC):
    """Base class for all property source adapters."""

    name: str

    @abstractmethod
    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        """Fetch raw listings for the given search areas and criteria."""
        raise NotImplementedError

    @abstractmethod
    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        """Convert a raw response into a PropertyRecord, or None to skip it."""
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str | None]:
        """Return (ok, error_message). Overridden by adapters with a cheap probe."""
        try:
            self.fetch(
                [{"label": "healthcheck", "postcode_or_place": "Sheffield", "radius_miles": 1}],
                {"price": {"min": 0, "max": 10_000_000}},
                "sale",
            )
            return True, None
        except Exception as exc:  # noqa: BLE001 - healthcheck reports any failure
            return False, str(exc)
