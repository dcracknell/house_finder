"""Generate the interactive map dashboard.

Every property is serialised into a JSON blob embedded in a single static HTML
file - no server, no build step, no backend. Open the file (or sync it to a
phone) and the map, filters and sorting all work offline apart from the map
tiles themselves.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from house_finder import PROJECT_ROOT
from house_finder.util.price import format_price

logger = logging.getLogger(__name__)

TEMPLATE_DIR = PROJECT_ROOT / "templates"


def dashboard_path(settings: dict) -> Path:
    configured = (settings.get("paths") or {}).get("dashboard_html", "data/dashboard.html")
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _row_to_property(row: sqlite3.Row) -> dict:
    """Compact representation - this is embedded in the page, so keep it small."""
    try:
        matched = json.loads(row["matched_criteria"] or "[]")
    except (json.JSONDecodeError, TypeError):
        matched = []

    return {
        "id": row["property_id"],
        "t": row["listing_type"],
        "title": row["title"] or "",
        "addr": row["display_address"] or "",
        "price": row["price"],
        "priceText": format_price(row["price"], row["listing_type"]),
        "qual": row["price_qualifier"] or "",
        "beds": row["bedrooms"],
        "baths": row["bathrooms"],
        "type": row["property_type"] or "",
        "sqft": round(row["floor_area_sqft"]) if row["floor_area_sqft"] else None,
        "tenure": row["tenure"] or "",
        "score": row["fit_score"],
        "why": row["fit_reason"] or "",
        "matched": matched,
        "lat": row["lat"],
        "lon": row["lon"],
        "url": row["url"],
        "agent": row["agent_name"] or "",
        "status": row["status"] or "new",
        "listed": row["first_listed_date"] or "",
        "reduced": bool(row["price_reduced"]),
        "img": row["image_url"] or "",
        "vsLocal": row["price_vs_local_pct"],
        "soldAvg": row["local_sold_avg_price"],
        "epc": row["epc_current"],
        "crime": row["crime_incidents_nearby"],
        "flood": row["flood_warnings_nearby"],
        "bband": row["broadband_max_mbps"],
    }


def generate(
    conn: sqlite3.Connection, settings: dict, profile: dict | None = None, path: Path | None = None
) -> Path:
    """Render the dashboard and return where it landed."""
    target = path or dashboard_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT * FROM properties
        WHERE status NOT IN ('withdrawn', 'rejected')
        ORDER BY fit_score DESC NULLS LAST, last_seen DESC
        """
    ).fetchall()

    properties = [_row_to_property(row) for row in rows]
    mapped = [p for p in properties if p["lat"] is not None and p["lon"] is not None]
    unmapped = [p for p in properties if p["lat"] is None or p["lon"] is None]

    if mapped:
        centre = [
            sum(p["lat"] for p in mapped) / len(mapped),
            sum(p["lon"] for p in mapped) / len(mapped),
        ]
    else:
        centre = [53.3811, -1.4701]  # Sheffield, a sane default over the UK

    stats = {
        "total": len(properties),
        "sale": sum(1 for p in properties if p["t"] == "sale"),
        "rent": sum(1 for p in properties if p["t"] == "rent"),
        "strong": sum(1 for p in properties if (p["score"] or 0) >= 8),
        "unmapped": len(unmapped),
    }

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("dashboard.html.j2")

    html = template.render(
        properties_json=json.dumps(properties, separators=(",", ":")),
        centre_json=json.dumps(centre),
        stats=stats,
        unmapped=unmapped,
        generated_at=date.today().isoformat(),
        profile_name=(profile or {}).get("name") or "",
    )

    target.write_text(html, encoding="utf-8")
    logger.info(
        "dashboard: wrote %s (%d properties, %d on the map)",
        target, len(properties), len(mapped),
    )
    return target
