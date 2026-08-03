"""Generate the interactive map dashboard.

Every property is serialised into a JSON blob embedded in a single static HTML
file - no server, no build step, no backend. Open the file (or sync it to a
phone) and the map, filters and sorting all work offline apart from the map
tiles themselves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
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


def _repo_slug() -> str:
    """The "owner/repo" this checkout came from, so the map can link back to GitHub.

    The dashboard gets opened from GitHub Pages, from htmlpreview, from a phone
    and straight off disk, so the page itself cannot work this out from its own
    address - it guesses wrong and the header links land on a 404. Bake the
    answer in instead: Actions sets GITHUB_REPOSITORY, and locally the origin
    remote knows. An empty string is fine and leaves the links pointing at the
    local `house-finder ui` editor.
    """
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if slug:
        return slug

    try:
        remote = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

    match = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", remote)
    return match.group(1) if match else ""


# Characters that must never appear raw inside a <script> block. json.dumps
# leaves them alone, so a listing description containing "</script>" - text an
# estate agent writes, not us - would otherwise break out of the tag and be
# parsed as markup by the browser.
_SCRIPT_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\\u2028": "\\u2028",
    "\\u2029": "\\u2029",
}


def json_for_script(value) -> str:
    """Serialise `value` as JSON that is safe to embed in an HTML <script>."""
    text = json.dumps(value, separators=(",", ":"))
    for char, replacement in _SCRIPT_ESCAPES.items():
        text = text.replace(char, replacement)
    return text


def _safe_url(url: str | None) -> str:
    """Drop any listing URL that is not a plain http(s) link.

    Listing URLs are third-party data: a "javascript:" URL picked up from a
    scraped page would otherwise become a clickable link in the dashboard.
    """
    text = (url or "").strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    if text:
        logger.debug("dashboard: dropped non-http listing url %r", text[:80])
    return ""


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
        "url": _safe_url(row["url"]),
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
        properties_json=json_for_script(properties),
        centre_json=json_for_script(centre),
        stats=stats,
        unmapped=unmapped,
        generated_at=date.today().isoformat(),
        profile_name=(profile or {}).get("name") or "",
        repo_slug=_repo_slug(),
    )

    target.write_text(html, encoding="utf-8")
    logger.info(
        "dashboard: wrote %s (%d properties, %d on the map)",
        target, len(properties), len(mapped),
    )
    return target
