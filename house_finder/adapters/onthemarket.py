"""OnTheMarket adapter - deliberately not implemented.

OnTheMarket actively blocks automated access and its listings are largely a
subset of what Rightmove already carries, so the coverage gained would not
justify fighting the bot protection.

Recorded here rather than silently omitted so the decision is visible.
"""

from __future__ import annotations

from house_finder.adapters.base import Adapter, PropertyRecord, RawListing

_REASON = (
    "OnTheMarket actively blocks automated access and mostly duplicates "
    "Rightmove's stock. See adapters/onthemarket.py."
)


class OnTheMarketAdapter(Adapter):
    name = "onthemarket"

    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        raise NotImplementedError(_REASON)

    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        raise NotImplementedError(_REASON)

    def healthcheck(self) -> tuple[bool, str | None]:
        return False, _REASON
