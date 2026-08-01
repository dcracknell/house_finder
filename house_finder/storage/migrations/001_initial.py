"""Initial schema: properties, full-text index, run log, api call log."""

from __future__ import annotations

import sqlite3

VERSION = 1
NAME = "initial"


def up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS properties (
            property_id             TEXT PRIMARY KEY,
            source                  TEXT NOT NULL,
            listing_type            TEXT NOT NULL,   -- sale | rent

            title                   TEXT,
            display_address         TEXT,
            url                     TEXT NOT NULL,
            property_type           TEXT,
            property_subtype_raw    TEXT,
            postcode                TEXT,
            outcode                 TEXT,
            lat                     REAL,
            lon                     REAL,

            bedrooms                INTEGER,
            bathrooms               INTEGER,
            floor_area_sqft         REAL,
            tenure                  TEXT,
            leasehold_years_remaining INTEGER,
            epc_rating              TEXT,
            furnished               TEXT,
            description             TEXT,
            key_features            TEXT,            -- JSON array
            agent_name              TEXT,
            image_url               TEXT,
            image_count             INTEGER DEFAULT 0,

            price                   INTEGER,
            price_qualifier         TEXT,
            price_reduced           INTEGER DEFAULT 0,
            auction                 INTEGER DEFAULT 0,

            first_listed_date       TEXT,
            last_update_date        TEXT,
            let_available_date      TEXT,
            listing_status          TEXT DEFAULT 'available',

            fit_score               REAL,
            fit_reason              TEXT,
            fit_confidence          REAL,
            matched_criteria        TEXT,            -- JSON array
            ranker_version          TEXT,

            local_sold_avg_price    INTEGER,
            local_sold_sample_size  INTEGER,
            price_vs_local_pct      REAL,
            crime_incidents_nearby  INTEGER,
            flood_warnings_nearby   INTEGER,
            epc_current             INTEGER,
            epc_potential           INTEGER,
            broadband_max_mbps      REAL,

            matched_area            TEXT,
            first_seen              TEXT NOT NULL,
            last_seen               TEXT NOT NULL,
            content_hash            TEXT,

            -- User-owned. The pipeline must never overwrite these on an
            -- existing row; they round-trip through houses.xlsx.
            status                  TEXT DEFAULT 'new',
            notes                   TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_properties_type    ON properties(listing_type);
        CREATE INDEX IF NOT EXISTS idx_properties_score   ON properties(fit_score DESC);
        CREATE INDEX IF NOT EXISTS idx_properties_status  ON properties(status);
        CREATE INDEX IF NOT EXISTS idx_properties_seen    ON properties(last_seen);
        CREATE INDEX IF NOT EXISTS idx_properties_outcode ON properties(outcode);

        -- Full-text search over the descriptive columns.
        CREATE VIRTUAL TABLE IF NOT EXISTS properties_fts USING fts5(
            property_id UNINDEXED,
            title,
            display_address,
            description,
            key_features,
            agent_name,
            content='properties',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS properties_ai AFTER INSERT ON properties BEGIN
            INSERT INTO properties_fts(rowid, property_id, title, display_address,
                                       description, key_features, agent_name)
            VALUES (new.rowid, new.property_id, new.title, new.display_address,
                    new.description, new.key_features, new.agent_name);
        END;

        CREATE TRIGGER IF NOT EXISTS properties_ad AFTER DELETE ON properties BEGIN
            INSERT INTO properties_fts(properties_fts, rowid, property_id, title,
                                       display_address, description, key_features, agent_name)
            VALUES ('delete', old.rowid, old.property_id, old.title,
                    old.display_address, old.description, old.key_features, old.agent_name);
        END;

        CREATE TRIGGER IF NOT EXISTS properties_au AFTER UPDATE ON properties BEGIN
            INSERT INTO properties_fts(properties_fts, rowid, property_id, title,
                                       display_address, description, key_features, agent_name)
            VALUES ('delete', old.rowid, old.property_id, old.title,
                    old.display_address, old.description, old.key_features, old.agent_name);
            INSERT INTO properties_fts(rowid, property_id, title, display_address,
                                       description, key_features, agent_name)
            VALUES (new.rowid, new.property_id, new.title, new.display_address,
                    new.description, new.key_features, new.agent_name);
        END;

        CREATE TABLE IF NOT EXISTS runs (
            run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            listings_fetched INTEGER DEFAULT 0,
            listings_kept   INTEGER DEFAULT 0,
            listings_new    INTEGER DEFAULT 0,
            listings_ranked INTEGER DEFAULT 0,
            cost_gbp        REAL DEFAULT 0.0,
            status          TEXT DEFAULT 'running',
            error           TEXT
        );

        CREATE TABLE IF NOT EXISTS enrichment_cache (
            cache_key   TEXT PRIMARY KEY,
            provider    TEXT NOT NULL,
            payload     TEXT,
            fetched_at  TEXT NOT NULL
        );
        """
    )
