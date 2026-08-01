"""Zoopla adapter - deliberately not implemented.

Zoopla retired its public listings API; live listing data now requires a
commercial data agreement. There is no free or personal-use route, so rather
than ship a fragile scraper this module exists to record the decision.

If you obtain a commercial Zoopla feed, implement fetch()/normalise() here and
flip `portals.zoopla.enabled` in config/sources.yaml.
"""

from __future__ import annotations

from house_finder.adapters.base import Adapter, PropertyRecord, RawListing

_REASON = (
    "Zoopla has no public listings API (retired) and access requires a "
    "commercial agreement. See adapters/zoopla.py."
)


class ZooplaAdapter(Adapter):
    name = "zoopla"

    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        raise NotImplementedError(_REASON)

    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        raise NotImplementedError(_REASON)

    def healthcheck(self) -> tuple[bool, str | None]:
        return False, _REASON
