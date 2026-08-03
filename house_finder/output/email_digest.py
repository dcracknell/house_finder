"""Email digest of new high-scoring properties."""

from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

from jinja2 import Environment, FileSystemLoader, select_autoescape

from house_finder import PROJECT_ROOT
from house_finder.util.price import format_price
from house_finder.util.secrets import looks_configured_secret

logger = logging.getLogger(__name__)

TEMPLATE_DIR = PROJECT_ROOT / "templates"


def _email_configured(email_config: dict) -> bool:
    return all(
        looks_configured_secret(email_config.get(k))
        for k in ("smtp_host", "smtp_user", "smtp_pass", "smtp_to")
    )


def _new_since(
    conn: sqlite3.Connection, since: date, min_score: float, limit: int = 40
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM properties
        WHERE notified_at IS NULL
          AND first_seen >= ?
          AND status = 'new'
          AND (fit_score IS NULL OR fit_score >= ?)
        ORDER BY fit_score DESC NULLS LAST, price ASC
        LIMIT ?
        """,
        (since.isoformat(), min_score, limit),
    ).fetchall()

    out = []
    for row in rows:
        try:
            matched = json.loads(row["matched_criteria"] or "[]")
        except (json.JSONDecodeError, TypeError):
            matched = []
        out.append(
            {
                "id": row["property_id"],
                "title": row["title"],
                "address": row["display_address"],
                "price": format_price(row["price"], row["listing_type"]),
                "qualifier": row["price_qualifier"] or "",
                "beds": row["bedrooms"],
                "baths": row["bathrooms"],
                "type": (row["property_type"] or "").replace("_", " "),
                "score": row["fit_score"],
                "why": row["fit_reason"] or "",
                "matched": matched,
                "url": row["url"],
                "image": row["image_url"],
                "listing_type": row["listing_type"],
                "vs_local": row["price_vs_local_pct"],
                "agent": row["agent_name"] or "",
            }
        )
    return out


def send_digest(
    conn: sqlite3.Connection, settings: dict, *, days: int = 14, force: bool = False
) -> bool:
    """Send the digest if there is anything to report. Returns True if sent."""
    mode = str(settings.get("mode", "passive")).lower()
    if mode != "active" and not force:
        logger.info("email: mode is '%s', not sending (set mode: active to enable)", mode)
        return False

    email_config = settings.get("email") or {}
    if not _email_configured(email_config):
        logger.info("email: SMTP not configured, skipping digest")
        return False

    min_score = float(email_config.get("min_fit_score", 7.0))
    properties = _new_since(conn, date.today() - timedelta(days=days), min_score)
    if not properties:
        logger.info("email: nothing new scoring %.1f+, not sending", min_score)
        return False

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("email.html.j2").render(
        properties=properties,
        generated_at=date.today().isoformat(),
        min_score=min_score,
        sale_count=sum(1 for p in properties if p["listing_type"] == "sale"),
        rent_count=sum(1 for p in properties if p["listing_type"] == "rent"),
    )

    noun = "property" if len(properties) == 1 else "properties"
    message = EmailMessage()
    message["Subject"] = f"{len(properties)} new {noun} to look at"
    message["From"] = email_config.get("smtp_from") or email_config["smtp_user"]
    message["To"] = email_config["smtp_to"]
    message.set_content(
        "\n\n".join(
            f"{p['price']} - {p['address']}\n"
            f"  {p['score'] if p['score'] is not None else '?'}/10 - {p['why']}\n"
            f"  {p['url']}"
            for p in properties
        )
    )
    message.add_alternative(html, subtype="html")

    host = email_config["smtp_host"]
    port = int(email_config.get("smtp_port", 587))
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if email_config.get("use_tls", True):
                smtp.starttls()
            smtp.login(email_config["smtp_user"], email_config["smtp_pass"])
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - a mail failure must not fail the run
        logger.error("email: could not send digest: %s", exc)
        return False

    _mark_notified(conn, [p["id"] for p in properties])
    logger.info("email: sent digest with %d properties", len(properties))
    return True


def _mark_notified(conn: sqlite3.Connection, property_ids: list[str]) -> None:
    """Record that these properties have been emailed.

    Without this the digest is purely "first seen recently", so the morning
    and afternoon runs mail the same listing twice, and anything first seen
    while SMTP was misconfigured is never mentioned at all.
    """
    if not property_ids:
        return
    stamp = datetime.now(UTC).isoformat()
    try:
        conn.executemany(
            "UPDATE properties SET notified_at = ? WHERE property_id = ?",
            [(stamp, property_id) for property_id in property_ids],
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("email: could not record what was emailed: %s", exc)
