"""Shared test fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from house_finder.adapters.base import PropertyRecord
from house_finder.storage import db

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn():
    """An in-memory database with the schema applied."""
    connection = db.connect(path=":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def rightmove_payload() -> dict:
    """A real Rightmove search response, captured from the live site."""
    with open(FIXTURE_DIR / "rightmove_search_buy.json", encoding="utf-8") as fh:
        return json.load(fh)


def make_record(**overrides) -> PropertyRecord:
    """A valid PropertyRecord with sensible defaults, for filter/rank tests."""
    defaults = {
        "property_id": "test-id",
        "source": "rightmove",
        "listing_type": "sale",
        "title": "3 bedroom semi-detached house",
        "display_address": "12 Example Road, Sheffield, S10 1AA",
        "url": "https://www.rightmove.co.uk/properties/1",
        "property_type": "semi_detached",
        "postcode": "S10 1AA",
        "outcode": "S10",
        "lat": 53.38,
        "lon": -1.49,
        "bedrooms": 3,
        "bathrooms": 1,
        "price": 250000,
        "description": "A well presented family home with a large garden and driveway.",
        "key_features": ["Large garden", "Off-street parking"],
        "first_listed_date": date.today(),
        "first_seen": date.today(),
        "last_seen": date.today(),
        "content_hash": "hash-1",
    }
    defaults.update(overrides)
    return PropertyRecord(**defaults)


@pytest.fixture
def record_factory():
    return make_record
