"""Spend tracking and hard caps for paid API calls.

Every LLM call must go through api_call_wrapper() so that:
  - token usage and cost land in data/quota.jsonl
  - a daily soft cap logs a warning
  - 2x the soft cap raises QuotaExceededError and stops further spend

PropertyData credits are tracked separately (credits, not tokens) by
record_credit_usage() / credits_used_this_month().
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from house_finder import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_LOG = PROJECT_ROOT / "data" / "quota.jsonl"


class QuotaExceededError(RuntimeError):
    """Raised when spend passes the hard cap and calls must stop."""


def _log_path(settings: dict | None) -> Path:
    if not settings:
        return DEFAULT_QUOTA_LOG
    configured = (settings.get("paths") or {}).get("quota_log")
    if not configured:
        return DEFAULT_QUOTA_LOG
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("quota: could not read %s: %s", path, exc)
    return entries


def _append_entry(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("quota: could not write %s: %s", path, exc)


def spend_today_gbp(settings: dict | None = None, today: date | None = None) -> float:
    """Total GBP spent on LLM calls today."""
    today = today or date.today()
    stamp = today.isoformat()
    return sum(
        float(e.get("cost_gbp", 0.0))
        for e in _read_entries(_log_path(settings))
        if str(e.get("ts", "")).startswith(stamp) and e.get("kind") != "credits"
    )


def credits_used_this_month(settings: dict | None = None, today: date | None = None) -> int:
    """Total PropertyData credits used in the current calendar month."""
    today = today or date.today()
    prefix = today.strftime("%Y-%m")
    return sum(
        int(e.get("credits", 0))
        for e in _read_entries(_log_path(settings))
        if e.get("kind") == "credits" and str(e.get("ts", "")).startswith(prefix)
    )


def record_credit_usage(
    provider: str, credits: int, settings: dict | None = None, note: str | None = None
) -> None:
    """Record non-token API usage billed in credits."""
    _append_entry(
        _log_path(settings),
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": "credits",
            "provider": provider,
            "credits": credits,
            "note": note,
        },
    )


def _rates(settings: dict, model_key: str) -> dict:
    return ((settings.get("models") or {}).get(model_key)) or {}


def estimate_cost_gbp(
    settings: dict,
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Cost in GBP for a call, using the rates in settings.yaml."""
    rates = _rates(settings, model_key)
    inp = float(rates.get("input_gbp_per_million", 0.0))
    out = float(rates.get("output_gbp_per_million", 0.0))
    cached = float(rates.get("cached_input_gbp_per_million", inp))
    return (
        input_tokens * inp / 1_000_000
        + output_tokens * out / 1_000_000
        + cached_input_tokens * cached / 1_000_000
    )


def check_quota(settings: dict, today: date | None = None) -> None:
    """Raise QuotaExceededError if today's spend is past the hard cap."""
    cap = float(settings.get("quota_soft_cap_gbp", 0) or 0)
    if cap <= 0:
        return
    spent = spend_today_gbp(settings, today)
    if spent >= cap * 2:
        raise QuotaExceededError(
            f"Daily spend £{spent:.2f} is at or past the hard cap "
            f"(2x £{cap:.2f}). No further API calls will be made today."
        )
    if spent >= cap:
        logger.warning(
            "quota: today's spend £%.2f has passed the soft cap of £%.2f "
            "(hard stop at £%.2f)",
            spent, cap, cap * 2,
        )


def api_call_wrapper(
    settings: dict,
    model_key: str,
    call: Callable[[], Any],
    *,
    label: str = "",
    usage_from: Callable[[Any], tuple[int, int, int]] | None = None,
) -> Any:
    """Run a paid API call, enforcing the cap and logging what it cost.

    `usage_from` maps the response to (input_tokens, output_tokens,
    cached_input_tokens). Defaults to reading Anthropic's `usage` block.
    """
    check_quota(settings)

    response = call()

    if usage_from is None:
        usage_from = _anthropic_usage
    try:
        in_tok, out_tok, cached_tok = usage_from(response)
    except Exception:  # noqa: BLE001 - never fail a good call over accounting
        in_tok = out_tok = cached_tok = 0

    cost = estimate_cost_gbp(settings, model_key, in_tok, out_tok, cached_tok)
    _append_entry(
        _log_path(settings),
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": model_key,
            "label": label,
            "model": _rates(settings, model_key).get("model"),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached_input_tokens": cached_tok,
            "cost_gbp": round(cost, 6),
        },
    )
    logger.debug(
        "quota: %s %s in=%d out=%d cost=£%.4f", model_key, label, in_tok, out_tok, cost
    )
    return response


def _anthropic_usage(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )
