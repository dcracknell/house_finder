"""Price and floor-area parsing."""

from __future__ import annotations

import pytest

from house_finder.util.price import (
    format_price,
    parse_floor_area_sqft,
    parse_price,
    parse_rent_pcm,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("£350,000", 350000),
        ("Guide Price £350,000", 350000),
        ("Offers in Region of £250,000", 250000),
        ("Offers Over £199,950", 199950),
        ("£1,250,000", 1250000),
        ("Fixed Price £180,000", 180000),
    ],
)
def test_parses_sale_prices(text, expected):
    assert parse_price(text)[0] == expected


def test_price_range_takes_the_lower_figure():
    """A guide range must not push a property out of budget on its top end."""
    assert parse_price("Guide Price £350,000-£375,000")[0] == 350000


def test_poa_has_no_amount():
    amount, qualifier = parse_price("POA")
    assert amount is None
    assert qualifier == "POA"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Guide Price £300,000", "Guide Price"),
        ("Offers Over £300,000", "Offers Over"),
        ("Offers in the region of £300,000", "Offers in Region of"),
    ],
)
def test_extracts_qualifiers(text, expected):
    assert parse_price(text)[1] == expected


def test_monthly_rent():
    assert parse_price("£1,200 pcm")[0] == 1200


def test_weekly_rent_is_converted_to_monthly():
    amount, _ = parse_price("£150 pw")
    assert 640 <= amount <= 660


def test_ignores_small_fee_amounts():
    assert parse_price("£50 admin fee")[0] is None


def test_parse_rent_pcm_normalises_frequencies():
    assert parse_rent_pcm(1000, "monthly") == 1000
    assert parse_rent_pcm(230, "weekly") == 997
    assert parse_rent_pcm(12000, "annually") == 1000
    assert parse_rent_pcm(None, "monthly") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1,104 sq. ft.", 1104.0),
        ("850 sqft", 850.0),
        ("2,000 square feet", 2000.0),
    ],
)
def test_parses_square_feet(text, expected):
    assert parse_floor_area_sqft(text) == expected


def test_converts_square_metres_to_feet():
    result = parse_floor_area_sqft("100 sq m")
    assert 1070 <= result <= 1080


def test_no_floor_area():
    assert parse_floor_area_sqft("") is None
    assert parse_floor_area_sqft("a nice house") is None


def test_format_price():
    assert format_price(350000) == "£350,000"
    assert format_price(1200, "rent") == "£1,200 pcm"
    assert format_price(None) == "POA"
