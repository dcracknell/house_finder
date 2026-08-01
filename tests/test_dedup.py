"""Storage sync - the invariants that protect the user's own work."""

from __future__ import annotations

from datetime import date, timedelta

from house_finder.pipeline.dedup import load_stored, mark_stale, sync_record, sync_records


def test_insert_then_update(conn, record_factory):
    record = record_factory()
    assert sync_record(conn, record) == "new"
    conn.commit()
    assert sync_record(conn, record) == "updated"


def test_user_status_is_never_overwritten(conn, record_factory):
    record = record_factory()
    sync_record(conn, record)
    conn.execute(
        "UPDATE properties SET status = 'viewing_booked', notes = 'Rang the agent' "
        "WHERE property_id = ?",
        (record.property_id,),
    )
    conn.commit()

    sync_record(conn, record_factory(price=245000))
    conn.commit()

    row = conn.execute(
        "SELECT status, notes, price FROM properties WHERE property_id = ?",
        (record.property_id,),
    ).fetchone()
    assert row["status"] == "viewing_booked"
    assert row["notes"] == "Rang the agent"
    assert row["price"] == 245000, "listing data should still refresh"


def test_score_survives_a_refresh_that_did_not_rank(conn, record_factory):
    scored = record_factory(fit_score=8.5, fit_reason="Great match", freshly_ranked=True)
    sync_record(conn, scored)
    conn.commit()

    sync_record(conn, record_factory(fit_score=None, freshly_ranked=False))
    conn.commit()

    row = conn.execute(
        "SELECT fit_score, fit_reason FROM properties WHERE property_id = ?",
        (scored.property_id,),
    ).fetchone()
    assert row["fit_score"] == 8.5
    assert row["fit_reason"] == "Great match"


def test_a_fresh_score_does_replace_the_old_one(conn, record_factory):
    sync_record(conn, record_factory(fit_score=4.0, freshly_ranked=True))
    conn.commit()
    sync_record(conn, record_factory(fit_score=9.0, freshly_ranked=True))
    conn.commit()

    row = conn.execute("SELECT fit_score FROM properties").fetchone()
    assert row["fit_score"] == 9.0


def test_empty_description_does_not_wipe_a_stored_one(conn, record_factory):
    sync_record(conn, record_factory(description="A detailed description."))
    conn.commit()
    sync_record(conn, record_factory(description=""))
    conn.commit()

    row = conn.execute("SELECT description FROM properties").fetchone()
    assert row["description"] == "A detailed description."


def test_enrichment_is_not_cleared_by_a_run_that_skipped_it(conn, record_factory):
    sync_record(conn, record_factory(local_sold_avg_price=200000, crime_incidents_nearby=300))
    conn.commit()
    sync_record(conn, record_factory())  # no enrichment this time
    conn.commit()

    row = conn.execute(
        "SELECT local_sold_avg_price, crime_incidents_nearby FROM properties"
    ).fetchone()
    assert row["local_sold_avg_price"] == 200000
    assert row["crime_incidents_nearby"] == 300


def test_first_seen_is_preserved(conn, record_factory):
    original = date.today() - timedelta(days=10)
    sync_record(conn, record_factory(first_seen=original))
    conn.commit()
    sync_record(conn, record_factory(first_seen=date.today()))
    conn.commit()

    row = conn.execute("SELECT first_seen FROM properties").fetchone()
    assert row["first_seen"] == original.isoformat()


def test_sold_stc_is_reflected_for_untouched_rows(conn, record_factory):
    sync_record(conn, record_factory())
    conn.commit()
    sync_record(conn, record_factory(listing_status="sold_stc"))
    conn.commit()

    row = conn.execute("SELECT status FROM properties").fetchone()
    assert row["status"] == "sold_stc"


def test_sold_stc_does_not_override_a_user_decision(conn, record_factory):
    sync_record(conn, record_factory())
    conn.execute("UPDATE properties SET status = 'offer_made'")
    conn.commit()

    sync_record(conn, record_factory(listing_status="sold_stc"))
    conn.commit()

    row = conn.execute("SELECT status FROM properties").fetchone()
    assert row["status"] == "offer_made"


def test_mark_stale_only_touches_untouched_rows(conn, record_factory):
    old = date.today() - timedelta(days=40)
    sync_record(conn, record_factory(property_id="a", url="http://a", last_seen=old))
    sync_record(conn, record_factory(property_id="b", url="http://b", last_seen=old))
    conn.execute("UPDATE properties SET status = 'interested' WHERE property_id = 'b'")
    conn.commit()

    mark_stale(conn, stale_days=21)

    rows = {r["property_id"]: r["status"] for r in conn.execute(
        "SELECT property_id, status FROM properties"
    )}
    assert rows["a"] == "withdrawn"
    assert rows["b"] == "interested"


def test_sync_records_counts(conn, record_factory):
    records = [
        record_factory(property_id="x", url="http://x"),
        record_factory(property_id="y", url="http://y"),
    ]
    counts = sync_records(conn, records)
    assert counts["new"] == 2

    counts = sync_records(conn, records)
    assert counts["updated"] == 2


def test_load_stored_returns_rows_by_id(conn, record_factory):
    record = record_factory()
    sync_record(conn, record)
    conn.commit()

    stored = load_stored(conn, [record.property_id, "missing"])
    assert record.property_id in stored
    assert "missing" not in stored


def test_full_text_search_index_is_populated(conn, record_factory):
    sync_record(conn, record_factory(description="Wonderful conservatory and orchard."))
    conn.commit()

    rows = conn.execute(
        "SELECT p.property_id FROM properties p JOIN properties_fts f ON f.rowid = p.rowid "
        "WHERE properties_fts MATCH 'orchard'"
    ).fetchall()
    assert len(rows) == 1
