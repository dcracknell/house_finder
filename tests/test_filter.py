"""Hard filters: the rules that enforce the user's stated criteria."""

from __future__ import annotations

from datetime import date, timedelta

from house_finder.pipeline.filter import (
    apply_enrichment_filters,
    apply_filters,
    extract_lease_years,
)

BASE = {
    "price": {"min": 100000, "max": 300000},
    "bedrooms_min": 2,
    "property_types": ["detached", "semi_detached", "terraced"],
    "exclusions": {},
}


def test_keeps_a_matching_property(conn, record_factory):
    kept = apply_filters([record_factory()], BASE, conn)
    assert len(kept) == 1


def test_drops_over_budget(conn, record_factory):
    kept = apply_filters([record_factory(price=450000)], BASE, conn)
    assert kept == []


def test_drops_too_few_bedrooms(conn, record_factory):
    kept = apply_filters([record_factory(bedrooms=1)], BASE, conn)
    assert kept == []


def test_drops_wrong_property_type(conn, record_factory):
    kept = apply_filters([record_factory(property_type="flat")], BASE, conn)
    assert kept == []


def test_keeps_unclassified_property_type(conn, record_factory):
    """'other' means we could not classify it, not that it is wrong."""
    kept = apply_filters([record_factory(property_type="other")], BASE, conn)
    assert len(kept) == 1


def test_keeps_property_with_unknown_bedrooms(conn, record_factory):
    """A missing field is not evidence of a bad match."""
    kept = apply_filters([record_factory(bedrooms=None)], BASE, conn)
    assert len(kept) == 1


def test_keeps_poa_when_it_has_a_description(conn, record_factory):
    kept = apply_filters([record_factory(price=None)], BASE, conn)
    assert len(kept) == 1


def test_drops_poa_with_no_detail_at_all(conn, record_factory):
    kept = apply_filters([record_factory(price=None, description="")], BASE, conn)
    assert kept == []


def test_drops_listings_older_than_configured(conn, record_factory):
    criteria = {**BASE, "max_days_since_listed": 30}
    old = record_factory(first_listed_date=date.today() - timedelta(days=90))
    assert apply_filters([old], criteria, conn) == []


def test_drops_sold_stc_by_default(conn, record_factory):
    kept = apply_filters([record_factory(listing_status="sold_stc")], BASE, conn)
    assert kept == []


def test_keeps_sold_stc_when_requested(conn, record_factory):
    criteria = {**BASE, "include_sold_stc": True}
    kept = apply_filters([record_factory(listing_status="sold_stc")], criteria, conn)
    assert len(kept) == 1


def test_drops_retirement_homes_when_excluded(conn, record_factory):
    criteria = {**BASE, "exclusions": {"no_retirement_homes": True}}
    record = record_factory(description="A retirement apartment for the over 60s.")
    assert apply_filters([record], criteria, conn) == []


def test_drops_shared_ownership_when_excluded(conn, record_factory):
    criteria = {**BASE, "exclusions": {"no_shared_ownership": True}}
    record = record_factory(description="Available on a shared ownership basis.")
    assert apply_filters([record], criteria, conn) == []


def test_drops_auction_when_excluded(conn, record_factory):
    criteria = {**BASE, "exclusions": {"no_auction": True}}
    assert apply_filters([record_factory(auction=True)], criteria, conn) == []


def test_drops_user_excluded_keywords(conn, record_factory):
    criteria = {**BASE, "exclusions": {"keyword_excludes": ["cash buyers only"]}}
    record = record_factory(description="Sold as seen, cash buyers only please.")
    assert apply_filters([record], criteria, conn) == []


def test_keyword_exclusion_respects_word_boundaries(conn, record_factory):
    """'flat' must not knock out 'flat lawn' in a house description."""
    criteria = {**BASE, "exclusions": {"keyword_excludes": ["studio"]}}
    record = record_factory(description="The garden has a flat lawn and a studious feel.")
    assert len(apply_filters([record], criteria, conn)) == 1


def test_drops_short_lease_when_configured(conn, record_factory):
    criteria = {**BASE, "exclusions": {"no_leasehold_under_years": 90}}
    record = record_factory(
        tenure="leasehold", description="Leasehold with 62 years remaining on the lease."
    )
    assert apply_filters([record], criteria, conn) == []


def test_keeps_long_lease(conn, record_factory):
    criteria = {**BASE, "exclusions": {"no_leasehold_under_years": 90}}
    record = record_factory(
        tenure="leasehold", description="Leasehold with 999 years remaining."
    )
    assert len(apply_filters([record], criteria, conn)) == 1


def test_keeps_leasehold_with_unstated_lease_length(conn, record_factory):
    """Most listings omit the lease term; dropping them would lose most flats."""
    criteria = {**BASE, "exclusions": {"no_leasehold_under_years": 90}}
    record = record_factory(tenure="leasehold", description="A lovely apartment.")
    assert len(apply_filters([record], criteria, conn)) == 1


def test_extract_lease_years():
    assert extract_lease_years("125 years remaining on the lease") == 125
    assert extract_lease_years("with 999 year lease") == 999
    assert extract_lease_years("no lease information") is None


def test_rent_drops_house_shares(conn, record_factory):
    criteria = {
        "price_pcm": {"min": 0, "max": 1200},
        "bedrooms_min": 1,
        "exclusions": {"no_house_share": True},
    }
    record = record_factory(listing_type="rent", price=600, property_type="house_share")
    assert apply_filters([record], criteria, conn, listing_type="rent") == []


def test_rent_uses_the_pcm_budget(conn, record_factory):
    criteria = {"price_pcm": {"min": 0, "max": 800}, "exclusions": {}}
    record = record_factory(listing_type="rent", price=1500)
    assert apply_filters([record], criteria, conn, listing_type="rent") == []


def test_previously_rejected_property_is_suppressed(conn, record_factory):
    record = record_factory()
    conn.execute(
        "INSERT INTO properties (property_id, source, listing_type, url, status, "
        "first_seen, last_seen) VALUES (?, 'rightmove', 'sale', ?, 'rejected', ?, ?)",
        (record.property_id, record.url, date.today().isoformat(), date.today().isoformat()),
    )
    conn.commit()
    assert apply_filters([record], BASE, conn, cooldown_days=90) == []


def test_portal_results_are_not_re_filtered_by_radius(conn, record_factory):
    """Rightmove applies the radius server-side; re-checking wrongly drops results."""
    criteria = {**BASE, "search_areas": [{"radius_miles": 1}]}
    far_away = record_factory(source="rightmove", lat=51.5, lon=-0.12)  # London
    kept = apply_filters([far_away], criteria, conn, home_coords=(53.38, -1.47))
    assert len(kept) == 1


def test_agent_pages_are_filtered_by_radius(conn, record_factory):
    """An agent's own page lists everything they have, so distance still applies."""
    criteria = {**BASE, "search_areas": [{"radius_miles": 1}]}
    far_away = record_factory(source="agent_page:someone", lat=51.5, lon=-0.12)
    assert apply_filters([far_away], criteria, conn, home_coords=(53.38, -1.47)) == []


# --- Listing metadata filters ---


def test_drops_outcode_not_on_the_include_list(conn, record_factory):
    criteria = {**BASE, "outcode_includes": ["S11", "S17"]}
    assert apply_filters([record_factory()], criteria, conn) == []


def test_keeps_outcode_on_the_include_list(conn, record_factory):
    """User-typed outcodes are messy, so case and spacing are normalised."""
    criteria = {**BASE, "outcode_includes": ["s10 ", "S17"]}
    assert len(apply_filters([record_factory()], criteria, conn)) == 1


def test_drops_excluded_outcode(conn, record_factory):
    criteria = {**BASE, "exclusions": {"outcode_excludes": ["S10"]}}
    assert apply_filters([record_factory()], criteria, conn) == []


def test_keeps_property_with_unknown_outcode(conn, record_factory):
    criteria = {**BASE, "outcode_includes": ["S11"]}
    assert len(apply_filters([record_factory(outcode=None)], criteria, conn)) == 1


def test_drops_blocked_agent(conn, record_factory):
    criteria = {**BASE, "exclusions": {"agent_excludes": ["example estates"]}}
    record = record_factory(agent_name="Example Estates, Sheffield")
    assert apply_filters([record], criteria, conn) == []


def test_keeps_agent_that_is_not_blocked(conn, record_factory):
    criteria = {**BASE, "exclusions": {"agent_excludes": ["example estates"]}}
    record = record_factory(agent_name="Other Agents")
    assert len(apply_filters([record], criteria, conn)) == 1


def test_drops_listings_with_too_few_photos(conn, record_factory):
    criteria = {**BASE, "min_image_count": 3}
    assert apply_filters([record_factory(image_count=1)], criteria, conn) == []


def test_drops_property_over_the_price_per_sqft_ceiling(conn, record_factory):
    criteria = {**BASE, "max_price_per_sqft": 200}
    record = record_factory(price=250000, floor_area_sqft=500)
    assert apply_filters([record], criteria, conn) == []


def test_keeps_unmeasured_property_when_capping_price_per_sqft(conn, record_factory):
    """Most listings never state a floor area; they must not all be dropped."""
    criteria = {**BASE, "max_price_per_sqft": 200}
    record = record_factory(floor_area_sqft=None)
    assert len(apply_filters([record], criteria, conn)) == 1


# --- Enrichment filters, applied after public-record data is attached ---


def test_enrichment_filter_is_a_no_op_when_nothing_is_configured(record_factory):
    records = [record_factory()]
    assert apply_enrichment_filters(records, BASE) is records


def test_drops_epc_below_the_minimum(record_factory):
    criteria = {**BASE, "min_epc_rating": "C"}
    assert apply_enrichment_filters([record_factory(epc_current=40)], criteria) == []


def test_keeps_epc_exactly_at_the_minimum(record_factory):
    criteria = {**BASE, "min_epc_rating": "C"}
    assert len(apply_enrichment_filters([record_factory(epc_current=69)], criteria)) == 1


def test_falls_back_to_the_epc_letter_when_there_is_no_score(record_factory):
    criteria = {**BASE, "min_epc_rating": "C"}
    assert apply_enrichment_filters([record_factory(epc_rating="E")], criteria) == []


def test_keeps_property_with_no_epc_data(record_factory):
    """Enrichment is capped per run, so most records arrive with nothing."""
    criteria = {**BASE, "min_epc_rating": "C"}
    assert len(apply_enrichment_filters([record_factory()], criteria)) == 1


def test_drops_property_with_too_much_crime(record_factory):
    criteria = {**BASE, "max_crime_incidents": 10}
    record = record_factory(crime_incidents_nearby=40)
    assert apply_enrichment_filters([record], criteria) == []


def test_drops_property_with_slow_broadband(record_factory):
    criteria = {**BASE, "min_broadband_mbps": 100}
    record = record_factory(broadband_max_mbps=12.0)
    assert apply_enrichment_filters([record], criteria) == []


def test_drops_property_priced_above_local_comparables(record_factory):
    criteria = {**BASE, "max_price_vs_local_pct": 10}
    record = record_factory(price_vs_local_pct=25.0)
    assert apply_enrichment_filters([record], criteria) == []


def test_keeps_property_priced_below_local_comparables(record_factory):
    criteria = {**BASE, "max_price_vs_local_pct": 10}
    record = record_factory(price_vs_local_pct=-5.0)
    assert len(apply_enrichment_filters([record], criteria)) == 1


def test_drops_flood_risk_when_excluded(record_factory):
    criteria = {**BASE, "exclusions": {"no_flood_risk": True}}
    record = record_factory(flood_warnings_nearby=2)
    assert apply_enrichment_filters([record], criteria) == []


def test_keeps_property_with_no_flood_data(record_factory):
    criteria = {**BASE, "exclusions": {"no_flood_risk": True}}
    assert len(apply_enrichment_filters([record_factory()], criteria)) == 1
