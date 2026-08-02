"""CLI entry point. Run `house-finder --help` for the full list of commands."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from house_finder import (
    PROFILE_PATH,
    PROJECT_ROOT,
    load_profile,
    load_settings,
    load_sources,
)

logger = logging.getLogger(__name__)


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, str(level).upper(), logging.INFO),
    )


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass


def _load_all() -> tuple[dict, dict, dict]:
    """Load settings, profile and sources, failing clearly if any are missing."""
    try:
        settings = load_settings()
        profile = load_profile()
        sources = load_sources()
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Missing config file: {exc.filename}. "
            "The config/ directory should contain profile.json, settings.yaml, "
            "sources.yaml and ranker.yaml."
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.ClickException(f"A config file is not valid: {exc}") from exc
    return settings, profile, sources


def _active_modes(profile: dict) -> list[str]:
    modes = [m for m in (profile.get("modes_enabled") or ["buy"]) if m in {"buy", "rent"}]
    return modes or ["buy"]


def _criteria_for(profile: dict, mode: str) -> dict:
    """The criteria block for a mode, with shared keys folded in."""
    criteria = dict(profile.get(mode) or {})
    criteria.setdefault(
        "rejected_property_cooldown_days", profile.get("rejected_property_cooldown_days", 90)
    )
    return criteria


def _listing_type(mode: str) -> str:
    return "rent" if mode == "rent" else "sale"


def _check_profile_configured(profile: dict) -> None:
    placeholder = "REPLACE_ME"
    if str(profile.get("name", "")).strip() == placeholder:
        click.echo(
            click.style(
                "Note: config/profile.json still has placeholder values. "
                "Run `house-finder ui` to set your real search criteria.",
                fg="yellow",
            ),
            err=True,
        )


@click.group()
@click.version_option(package_name="house-finder")
def main() -> None:
    """UK house-finder - search, score, track and map properties.

    Run `house-finder COMMAND --help` for details on any command.
    """


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@main.command()
@click.option("--dry-run", is_flag=True, help="Fetch and score, but save nothing.")
@click.option("--mode", type=click.Choice(["buy", "rent"]), help="Only run this mode.")
@click.option("--source", help="Only run this source, e.g. rightmove.")
@click.option("--no-rank", is_flag=True, help="Skip LLM scoring (no API spend).")
@click.option("--no-enrich", is_flag=True, help="Skip enrichment lookups.")
@click.option(
    "--enrich-limit",
    type=int,
    default=60,
    show_default=True,
    help="Max properties to enrich per run (highest-scoring first).",
)
@click.option(
    "--area",
    "area_overrides",
    multiple=True,
    help="Search this postcode or place instead of the ones in your profile. "
    "Repeatable, and nothing is saved - your own criteria are left alone.",
)
@click.option(
    "--radius",
    "area_radius",
    type=float,
    help="Radius in miles for --area. Snapped to the nearest radius Rightmove allows.",
)
def run(
    dry_run: bool,
    mode: str | None,
    source: str | None,
    no_rank: bool,
    no_enrich: bool,
    enrich_limit: int,
    area_overrides: tuple[str, ...],
    area_radius: float | None,
) -> None:
    """Run the full pipeline: fetch, filter, score, save, export, map."""
    _load_env()
    settings, profile, sources = _load_all()
    _configure_logging(settings.get("log_level", "INFO"))
    _check_profile_configured(profile)

    if area_overrides:
        where = ", ".join(area_overrides)
        radius_text = f" within {area_radius:g} miles" if area_radius is not None else ""
        click.echo(
            f"One-off search of {where}{radius_text}. "
            "Your saved search areas are unchanged."
        )

    run_mode = str(settings.get("mode", "passive")).lower()
    if run_mode == "paused":
        click.echo("Pipeline is paused (mode: paused in config/settings.yaml). Nothing to do.")
        return

    dry_run = dry_run or bool(settings.get("dry_run"))

    from house_finder import enrichment
    from house_finder.output import dashboard, email_digest, workbook_export, workbook_import
    from house_finder.pipeline import dedup, normalise, rank
    from house_finder.pipeline import filter as filter_module
    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    run_id = db.start_run(conn)

    totals = {"fetched": 0, "kept": 0, "new": 0, "ranked": 0}

    try:
        if not dry_run:
            workbook_import.import_edits(conn, settings)

        modes = [mode] if mode else _active_modes(profile)
        all_records = []

        for current_mode in modes:
            listing_type = _listing_type(current_mode)
            criteria = _criteria_for(profile, current_mode)
            if area_overrides:
                criteria["search_areas"] = [
                    {
                        "label": place,
                        "postcode_or_place": place,
                        "radius_miles": area_radius,
                    }
                    for place in area_overrides
                ]
            areas = _resolve_areas(criteria)
            if not areas:
                logger.warning("run: no search areas configured for %s", current_mode)
                continue

            click.echo(f"\n=== {current_mode.upper()} ===")
            adapters = _build_adapters(sources, only=source)
            if not adapters:
                raise click.ClickException(
                    "No listing sources are enabled. Check portals: in config/sources.yaml."
                )

            raw_records = []
            for adapter in adapters:
                try:
                    raw = adapter.fetch(areas, criteria, listing_type)
                except NotImplementedError as exc:
                    logger.info("run: %s is not implemented (%s)", adapter.name, exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                    logger.error("run: %s failed: %s", adapter.name, exc)
                    continue

                for item in raw:
                    try:
                        record = adapter.normalise(item)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("run: could not normalise a %s listing: %s", adapter.name, exc)
                        continue
                    if record is not None:
                        raw_records.append(record)

            totals["fetched"] += len(raw_records)
            records = normalise.normalise_all(raw_records)
            click.echo(f"  fetched {len(records)} listings")

            home = _home_coords(areas)
            kept = filter_module.apply_filters(
                records,
                criteria,
                conn,
                listing_type=listing_type,
                home_coords=home,
                cooldown_days=int(criteria.get("rejected_property_cooldown_days", 90)),
            )
            totals["kept"] += len(kept)
            click.echo(f"  {len(kept)} passed your filters")

            if kept and not no_enrich:
                enrichment.enrich_records(
                    kept, sources, settings, conn, limit=enrich_limit
                )

            if kept and not no_rank:
                stored = dedup.load_stored(conn, [r.property_id for r in kept])
                ranked = rank.rank_records(
                    kept,
                    criteria,
                    settings,
                    listing_type=listing_type,
                    stored_by_id=stored,
                )
                totals["ranked"] += len(ranked)
                click.echo(f"  scored {len(ranked)} new or changed listings")

            all_records.extend(kept)

        if dry_run:
            click.echo("\n--dry-run: nothing saved. Top matches this run:\n")
            for record in sorted(
                all_records, key=lambda r: -(r.fit_score or 0)
            )[:15]:
                score = f"{record.fit_score:.1f}" if record.fit_score is not None else " ? "
                price = f"£{record.price:,}" if record.price else "POA"
                click.echo(f"  {score}  {price:>10}  {record.display_address[:48]}")
                if record.fit_reason:
                    click.echo(f"          {record.fit_reason[:90]}")
            db.finish_run(conn, run_id, status="dry_run", **_run_counts(totals))
            return

        counts = dedup.sync_records(conn, all_records)
        totals["new"] = counts.get("new", 0)
        dedup.mark_stale(conn, int(settings.get("stale_listing_days", 21)))

        xlsx = workbook_export.export(conn, settings)
        html = dashboard.generate(conn, settings, profile)
        db.backup(settings, conn)

        email_digest.send_digest(conn, settings)

        db.finish_run(conn, run_id, status="ok", **_run_counts(totals))

        click.echo(
            f"\nDone. {totals['new']} new, {totals['kept']} tracked."
            f"\n  workbook:  {xlsx}"
            f"\n  map:       {html}"
        )
    except Exception as exc:
        db.finish_run(conn, run_id, status="error", error=str(exc), **_run_counts(totals))
        raise
    finally:
        conn.close()


def _run_counts(totals: dict) -> dict:
    return {
        "listings_fetched": totals["fetched"],
        "listings_kept": totals["kept"],
        "listings_new": totals["new"],
        "listings_ranked": totals["ranked"],
    }


def _resolve_areas(criteria: dict) -> list[dict]:
    from house_finder.search.areas import iter_areas

    return iter_areas(criteria)


def _home_coords(areas: list[dict]) -> tuple[float, float] | None:
    """Coordinates of the first search area, used for radius sanity checks."""
    from house_finder.util.geocode import geocode

    if not areas:
        return None
    return geocode(areas[0]["postcode_or_place"])


def _build_adapters(sources: dict, only: str | None = None) -> list:
    """Instantiate every enabled listing source."""
    from house_finder.adapters.agent_page import AgentPageAdapter
    from house_finder.adapters.auctions import AuctionAdapter
    from house_finder.adapters.rightmove import RightmoveAdapter

    portals = (sources.get("portals") or {})
    adapters = []

    rightmove_config = portals.get("rightmove") or {}
    if rightmove_config.get("enabled", True):
        adapters.append(RightmoveAdapter(rightmove_config))

    auction_config = portals.get("auctions") or {}
    if auction_config.get("enabled", False):
        adapters.append(AuctionAdapter(auction_config))

    for page in sources.get("custom_pages") or []:
        if page.get("url"):
            adapters.append(
                AgentPageAdapter(
                    page.get("agent_name") or page["url"],
                    page["url"],
                    delay_seconds=float(page.get("request_delay_seconds", 2.0)),
                )
            )

    if only:
        adapters = [a for a in adapters if a.name == only or a.name.startswith(f"{only}:")]
    return adapters


# ---------------------------------------------------------------------------
# export / dashboard
# ---------------------------------------------------------------------------


@main.command()
def export() -> None:
    """Rebuild houses.xlsx and the map from the database, without fetching."""
    _load_env()
    settings, profile, _ = _load_all()
    _configure_logging(settings.get("log_level", "INFO"))

    from house_finder.output import dashboard, workbook_export, workbook_import
    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    try:
        workbook_import.import_edits(conn, settings)
        xlsx = workbook_export.export(conn, settings)
        html = dashboard.generate(conn, settings, profile)
        click.echo(f"workbook: {xlsx}\nmap:      {html}")
    finally:
        conn.close()


@main.command()
@click.option("--open", "open_browser", is_flag=True, help="Open the map afterwards.")
def map_view(open_browser: bool) -> None:
    """Regenerate just the interactive map."""
    _load_env()
    settings, profile, _ = _load_all()
    _configure_logging(settings.get("log_level", "INFO"))

    from house_finder.output import dashboard
    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    try:
        path = dashboard.generate(conn, settings, profile)
        click.echo(str(path))
        if open_browser:
            import webbrowser

            webbrowser.open(path.as_uri())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# search / list
# ---------------------------------------------------------------------------


@main.command()
@click.argument("query")
@click.option("--limit", default=20, show_default=True)
def search(query: str, limit: int) -> None:
    """Full-text search over everything stored."""
    _load_env()
    settings, _, _ = _load_all()
    _configure_logging(settings.get("log_level", "WARNING"))

    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    try:
        rows = conn.execute(
            """
            SELECT p.* FROM properties p
            JOIN properties_fts f ON f.rowid = p.rowid
            WHERE properties_fts MATCH ?
            ORDER BY p.fit_score DESC NULLS LAST
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - FTS syntax errors are user input errors
        raise click.ClickException(f"Search failed: {exc}") from exc
    finally:
        pass

    if not rows:
        click.echo("No matches.")
        conn.close()
        return

    for row in rows:
        score = f"{row['fit_score']:.1f}" if row["fit_score"] is not None else " ? "
        price = f"£{row['price']:,}" if row["price"] else "POA"
        click.echo(f"{score}  {price:>10}  {row['display_address'][:50]}")
        click.echo(f"       {row['url']}")
    conn.close()


@main.command("list")
@click.option("--status", help="Filter by status, e.g. interested.")
@click.option("--min-score", type=float, default=0.0, show_default=True)
@click.option("--type", "listing_type", type=click.Choice(["sale", "rent"]))
@click.option("--limit", default=25, show_default=True)
def list_properties(
    status: str | None, min_score: float, listing_type: str | None, limit: int
) -> None:
    """List tracked properties, best match first."""
    _load_env()
    settings, _, _ = _load_all()
    _configure_logging(settings.get("log_level", "WARNING"))

    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    try:
        clauses = ["(fit_score IS NULL OR fit_score >= ?)"]
        params: list = [min_score]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if listing_type:
            clauses.append("listing_type = ?")
            params.append(listing_type)
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT * FROM properties WHERE {' AND '.join(clauses)}
            ORDER BY fit_score DESC NULLS LAST LIMIT ?
            """,  # noqa: S608 - clauses are built from fixed strings
            params,
        ).fetchall()

        if not rows:
            click.echo("Nothing matches.")
            return
        for row in rows:
            score = f"{row['fit_score']:.1f}" if row["fit_score"] is not None else " ? "
            price = f"£{row['price']:,}" if row["price"] else "POA"
            click.echo(
                f"{score}  {price:>10}  {(row['status'] or ''):<15} "
                f"{row['display_address'][:44]}"
            )
            if row["fit_reason"]:
                click.echo(f"                            {row['fit_reason'][:80]}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@main.command()
def health() -> None:
    """Check every configured source is reachable and still parseable."""
    _load_env()
    settings, _, sources = _load_all()
    _configure_logging(settings.get("log_level", "WARNING"))

    from house_finder import enrichment

    click.echo("Listing sources:")
    failures = 0
    for adapter in _build_adapters(sources):
        ok, error = adapter.healthcheck()
        mark = click.style("OK  ", fg="green") if ok else click.style("FAIL", fg="red")
        click.echo(f"  {mark} {adapter.name}")
        if not ok:
            failures += 1
            click.echo(f"       {error}")

    click.echo("\nEnrichment sources:")
    results = enrichment.healthcheck_all(sources, settings)
    if not results:
        click.echo("  (none enabled)")
    for name, (ok, error) in sorted(results.items()):
        mark = click.style("OK  ", fg="green") if ok else click.style("WARN", fg="yellow")
        click.echo(f"  {mark} {name}")
        if not ok:
            click.echo(f"       {error}")

    click.echo("\nDisabled / not implemented:")
    for name in ("zoopla", "onthemarket"):
        if not ((sources.get("portals") or {}).get(name) or {}).get("enabled"):
            click.echo(f"  --   {name} (see adapters/{name}.py for why)")

    if failures:
        click.echo(
            click.style(
                f"\n{failures} listing source(s) failed. If Rightmove is failing, its "
                "page structure may have changed - see adapters/rightmove.py.",
                fg="red",
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------


@main.command()
def costs() -> None:
    """Show what the pipeline has spent recently."""
    _load_env()
    settings, _, _ = _load_all()

    from house_finder.util.quota import (
        _log_path,
        _read_entries,
        credits_used_this_month,
        spend_today_gbp,
    )

    entries = _read_entries(_log_path(settings))
    if not entries:
        click.echo("No API spend recorded yet.")
        return

    by_day: dict[str, float] = {}
    for entry in entries:
        if entry.get("kind") == "credits":
            continue
        day = str(entry.get("ts", ""))[:10]
        by_day[day] = by_day.get(day, 0.0) + float(entry.get("cost_gbp", 0.0))

    cap = float(settings.get("quota_soft_cap_gbp", 0) or 0)
    click.echo("Daily LLM spend:")
    for day in sorted(by_day)[-14:]:
        click.echo(f"  {day}  £{by_day[day]:.4f}")
    click.echo(f"\nToday: £{spend_today_gbp(settings):.4f}" + (f" (cap £{cap:.2f})" if cap else ""))

    credits = credits_used_this_month(settings)
    if credits:
        click.echo(f"PropertyData credits this month: {credits}")


@main.command()
@click.option("--port", default=8765, show_default=True)
@click.option("--no-browser", is_flag=True, help="Do not open a browser window.")
def ui(port: int, no_browser: bool) -> None:
    """Open the preferences editor to change your search criteria."""
    _load_env()
    settings, _, _ = _load_all()
    _configure_logging(settings.get("log_level", "INFO"))

    from house_finder.ui import serve

    serve(port=port, open_browser=not no_browser)


@main.command()
@click.argument("issue_body_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def profile_from_issue(issue_body_path: Path) -> None:
    """Build config/profile.json from a GitHub issue form submission."""
    _load_env()
    _configure_logging()

    from house_finder.profile.issue_profile import build_profile_from_issue

    try:
        body = issue_body_path.read_text(encoding="utf-8")
        profile = build_profile_from_issue(body)
    except Exception as exc:  # noqa: BLE001 - surfaced to the workflow log
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Updated {PROFILE_PATH}")
    for mode in _active_modes(profile):
        criteria = profile.get(mode) or {}
        areas = ", ".join(a.get("label", "?") for a in criteria.get("search_areas") or [])
        price = criteria.get("price_pcm" if mode == "rent" else "price") or {}
        click.echo(
            f"  {mode}: {areas or 'no areas'} | "
            f"£{price.get('min', 0):,}-£{price.get('max', 0):,} | "
            f"{criteria.get('bedrooms_min', 0)}+ beds"
        )


@main.command()
def migrate() -> None:
    """Apply any pending database migrations."""
    _load_env()
    settings, _, _ = _load_all()
    _configure_logging(settings.get("log_level", "INFO"))

    from house_finder.storage import db

    conn = db.connect(settings)
    try:
        applied = db.migrate(conn)
        click.echo(
            f"Applied migrations: {applied}" if applied else "Database is already up to date."
        )
    finally:
        conn.close()


@main.command()
def stats() -> None:
    """Summarise what is being tracked."""
    _load_env()
    settings, _, _ = _load_all()
    _configure_logging(settings.get("log_level", "WARNING"))

    from house_finder.storage import db

    conn = db.open_and_migrate(settings)
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM properties").fetchone()["n"]
        if not total:
            click.echo("Nothing tracked yet. Run `house-finder run`.")
            return

        click.echo(f"Tracked properties: {total}\n")
        click.echo("By status:")
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM properties GROUP BY status ORDER BY n DESC"
        ):
            click.echo(f"  {row['status'] or 'unknown':<16} {row['n']}")

        click.echo("\nBy type:")
        for row in conn.execute(
            "SELECT listing_type, COUNT(*) AS n FROM properties GROUP BY listing_type"
        ):
            click.echo(f"  {row['listing_type']:<16} {row['n']}")

        row = conn.execute(
            "SELECT COUNT(*) AS n FROM properties WHERE fit_score >= 8"
        ).fetchone()
        click.echo(f"\nScoring 8+: {row['n']}")

        last = conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if last:
            click.echo(
                f"\nLast run: {last['started_at'][:16]} ({last['status']}) - "
                f"{last['listings_fetched']} fetched, {last['listings_new']} new"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
