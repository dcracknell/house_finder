"""Rightmove adapter: query building and normalisation of a real response."""

from __future__ import annotations

import pytest

from house_finder.adapters.rightmove import RightmoveAdapter


@pytest.fixture
def adapter():
    return RightmoveAdapter({"request_delay_seconds": 0, "max_results_per_area": 24})


def _listings(payload):
    return payload["props"]["pageProps"]["searchResults"]["properties"]


def test_normalises_a_real_listing(adapter, rightmove_payload):
    raw = _listings(rightmove_payload)[0]
    raw["_listing_type"] = "sale"
    raw["_area_label"] = "Sheffield"

    record = adapter.normalise(raw)

    assert record is not None
    assert record.source == "rightmove"
    assert record.listing_type == "sale"
    assert record.price and record.price > 0
    assert record.bedrooms and record.bedrooms > 0
    assert record.url.startswith("https://www.rightmove.co.uk/properties/")
    assert record.lat is not None and record.lon is not None
    assert record.matched_area == "Sheffield"


def test_every_fixture_listing_normalises(adapter, rightmove_payload):
    for raw in _listings(rightmove_payload):
        raw["_listing_type"] = "sale"
        record = adapter.normalise(raw)
        assert record is not None, "a real listing failed to normalise"
        assert record.property_type != "", record.property_subtype_raw


def test_property_id_is_stable_and_unique(adapter, rightmove_payload):
    listings = _listings(rightmove_payload)
    for raw in listings:
        raw["_listing_type"] = "sale"

    first = [adapter.normalise(dict(r)).property_id for r in listings]
    second = [adapter.normalise(dict(r)).property_id for r in listings]

    assert first == second, "property_id must not change between runs"
    assert len(set(first)) == len(first), "each listing needs a distinct id"


def test_url_fragment_is_stripped(adapter, rightmove_payload):
    """The '#/?channel=' fragment must not reach the id, or ids would churn."""
    raw = dict(_listings(rightmove_payload)[0])
    raw["propertyUrl"] = "/properties/12345#/?channel=RES_BUY"
    raw["_listing_type"] = "sale"

    record = adapter.normalise(raw)
    assert record.url == "https://www.rightmove.co.uk/properties/12345"
    assert "#" not in record.url


def test_subtype_mapping(adapter):
    assert adapter._map_property_type("Detached") == "detached"
    assert adapter._map_property_type("Semi-Detached") == "semi_detached"
    assert adapter._map_property_type("End of Terrace") == "terraced"
    assert adapter._map_property_type("Apartment") == "flat"
    assert adapter._map_property_type("Detached Bungalow") == "bungalow"
    assert adapter._map_property_type(None) == "other"


def test_tenure_mapping(adapter):
    assert adapter._map_tenure("FREEHOLD") == "freehold"
    assert adapter._map_tenure("LEASEHOLD") == "leasehold"
    assert adapter._map_tenure("SHARE_OF_FREEHOLD") == "share_of_freehold"
    assert adapter._map_tenure(None) is None


def test_buy_params_include_filters(adapter):
    params = adapter._build_params(
        "REGION^1195",
        {"radius_miles": 5.0},
        {
            "price": {"min": 150000, "max": 300000},
            "bedrooms_min": 3,
            "property_types": ["detached", "semi_detached"],
            "exclusions": {"no_retirement_homes": True, "no_shared_ownership": True},
            "include_sold_stc": False,
        },
        "sale",
    )

    assert params["channel"] == "BUY"
    assert params["minPrice"] == 150000
    assert params["maxPrice"] == 300000
    assert params["minBedrooms"] == 3
    assert "detached" in params["propertyTypes"]
    assert "retirement" in params["dontShow"]
    assert "sharedOwnership" in params["dontShow"]
    assert params["includeSSTC"] == "false"


def test_rent_params_use_the_rent_channel_and_pcm_budget(adapter):
    params = adapter._build_params(
        "REGION^1195",
        {"radius_miles": 3.0},
        {
            "price_pcm": {"min": 500, "max": 1200},
            "bedrooms_min": 2,
            "furnished": "furnished",
            "include_let_agreed": False,
        },
        "rent",
    )

    assert params["channel"] == "RENT"
    assert params["maxPrice"] == 1200
    assert params["furnishTypes"] == "furnished"
    assert params["includeLetAgreed"] == "false"


def test_rent_normalises_weekly_prices_to_monthly(adapter):
    raw = {
        "propertyUrl": "/properties/999",
        "displayAddress": "1 Test Street, S1 1AA",
        "price": {"amount": 200, "frequency": "weekly", "displayPrices": [{}]},
        "bedrooms": 2,
        "propertySubType": "Flat",
        "_listing_type": "rent",
    }
    record = adapter.normalise(raw)
    # 200/week is about 867 pcm, not 200.
    assert 850 <= record.price <= 880


def test_missing_url_is_skipped(adapter):
    assert adapter.normalise({"displayAddress": "Nowhere"}) is None


def test_extract_next_data_handles_missing_script(adapter):
    assert adapter._extract_next_data("<html><body>no data</body></html>") is None
