"""Auction listings adapter - not implemented, and here is what was checked.

Auction stock (repossessions, probate sales, renovation projects) is genuinely
worth having: much of it never appears on Rightmove. The problem is access.

What was investigated (August 2026):

- AuctionIQ (auctioniq.co.uk) aggregates 100+ UK auctioneers and is free to
  browse, but the site is a client-side single-page app: the initial HTML is a
  4KB shell with no listing data in it. Reading it would mean calling their
  private, undocumented backend API - which is a different and weaker position
  than reading a public page, and would break without warning. Not done.
- EIG (eigpropertyauctions.co.uk) has the best coverage in the UK but is a
  paid subscription service with no public API.
- Individual auction houses mostly publish their catalogue as a PDF, which is
  not worth parsing per-auctioneer.

The practical route that IS supported: many individual auctioneers publish
schema.org structured data on their own site. Add them under `custom_pages:`
in config/sources.yaml and adapters/agent_page.py will read them - the same
mechanism used for estate agents, reading each firm's own published data.

If a public auction API appears, implement fetch()/normalise() here and enable
`portals.auctions` in config/sources.yaml.
"""

from __future__ import annotations

from house_finder.adapters.base import Adapter, PropertyRecord, RawListing

_REASON = (
    "No usable public auction feed: AuctionIQ is a client-side app with no "
    "server-rendered data, and EIG is subscription-only. Add individual "
    "auctioneers under custom_pages: in config/sources.yaml instead. "
    "See adapters/auctions.py."
)


class AuctionAdapter(Adapter):
    name = "auctions"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def fetch(self, areas: list[dict], criteria: dict, listing_type: str) -> list[RawListing]:
        raise NotImplementedError(_REASON)

    def normalise(self, raw: RawListing) -> PropertyRecord | None:
        raise NotImplementedError(_REASON)

    def healthcheck(self) -> tuple[bool, str | None]:
        return False, _REASON
