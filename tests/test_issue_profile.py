"""Parsing a GitHub issue form submission into search criteria."""

from __future__ import annotations

import pytest

from house_finder.profile.issue_profile import build_profile_from_issue, parse_issue_body

ISSUE_BODY = """### Your name

Dave

### Buy - areas to search

S10
Hathersage

### Buy - search radius

5

### Buy - minimum price

150000

### Buy - maximum price

300000

### Buy - minimum bedrooms

3

### Buy - minimum bathrooms

_No response_

### Buy - property types

- [x] Detached
- [x] Semi-detached
- [ ] Flat / apartment
- [x] Bungalow

### Buy - must-haves

garden
off-street parking

### Buy - nice-to-haves

garage
no chain

### Buy - rule out

- [x] Retirement homes
- [x] Shared ownership
- [ ] Park or mobile homes
- [x] Auction properties
- [ ] New builds

### Buy - minimum lease years

90

### Buy - words that rule a property out

cash buyers only
japanese knotweed

### Buy - what are you looking for

A quiet family home with room to extend. Not on a main road.

### Rent - areas to search

_No response_
"""


def test_parses_sections():
    sections = parse_issue_body(ISSUE_BODY)
    assert sections["your name"] == "Dave"
    assert "S10" in sections["buy - areas to search"]


def test_builds_buy_criteria():
    profile = build_profile_from_issue(ISSUE_BODY, write=False, base_profile={})

    assert profile["name"] == "Dave"
    assert profile["modes_enabled"] == ["buy"]

    buy = profile["buy"]
    assert [a["postcode_or_place"] for a in buy["search_areas"]] == ["S10", "Hathersage"]
    assert all(a["radius_miles"] == 5 for a in buy["search_areas"])
    assert buy["price"] == {"min": 150000, "max": 300000}
    assert buy["bedrooms_min"] == 3
    assert buy["must_haves"] == ["garden", "off-street parking"]
    assert buy["nice_to_haves"] == ["garage", "no chain"]
    assert "quiet family home" in buy["preferences_freetext"]


def test_maps_ticked_property_types():
    buy = build_profile_from_issue(ISSUE_BODY, write=False, base_profile={})["buy"]
    assert set(buy["property_types"]) == {"detached", "semi_detached", "bungalow"}
    assert "flat" not in buy["property_types"]


def test_maps_ticked_exclusions():
    profile = build_profile_from_issue(ISSUE_BODY, write=False, base_profile={})
    exclusions = profile["buy"]["exclusions"]
    assert exclusions["no_retirement_homes"] is True
    assert exclusions["no_shared_ownership"] is True
    assert exclusions["no_auction"] is True
    assert exclusions["no_park_homes"] is False
    assert exclusions["no_new_build"] is False
    assert exclusions["no_leasehold_under_years"] == 90
    assert "cash buyers only" in exclusions["keyword_excludes"]


def test_no_response_is_treated_as_unset():
    buy = build_profile_from_issue(ISSUE_BODY, write=False, base_profile={})["buy"]
    assert buy.get("bathrooms_min") is None


def test_rent_section_left_blank_is_skipped():
    profile = build_profile_from_issue(ISSUE_BODY, write=False, base_profile={})
    assert "rent" not in profile["modes_enabled"]


def test_prices_with_symbols_and_commas():
    body = ISSUE_BODY.replace("150000", "£150,000").replace("300000", "£300,000")
    buy = build_profile_from_issue(body, write=False, base_profile={})["buy"]
    assert buy["price"] == {"min": 150000, "max": 300000}


def test_comma_separated_lists_are_accepted():
    body = ISSUE_BODY.replace("garden\noff-street parking", "garden, off-street parking")
    buy = build_profile_from_issue(body, write=False, base_profile={})["buy"]
    assert buy["must_haves"] == ["garden", "off-street parking"]


def test_rent_only_submission():
    body = """### Rent - areas to search

S1

### Rent - search radius

3

### Rent - maximum rent

1100

### Rent - minimum bedrooms

2

### Rent - rule out

- [x] House shares and rooms
- [x] Student-only lets
"""
    profile = build_profile_from_issue(body, write=False, base_profile={})
    assert "rent" in profile["modes_enabled"]
    assert profile["rent"]["price_pcm"]["max"] == 1100
    assert profile["rent"]["exclusions"]["no_house_share"] is True


def test_empty_body_is_rejected():
    with pytest.raises(ValueError, match="No form fields"):
        build_profile_from_issue("just some text", write=False)


def test_form_with_no_areas_is_rejected():
    with pytest.raises(ValueError, match="search areas"):
        build_profile_from_issue("### Your name\n\nDave\n", write=False)

