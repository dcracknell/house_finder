"""Two-pass property scoring.

Pass 1 is a free keyword pre-score against must_haves / nice_to_haves. Pass 2
sends what survives to Claude for the judgement keywords cannot make ("is this
actually the quiet family home they described?").

Cost control is the point of the split, and every call goes through
util.quota.api_call_wrapper so spend is logged and capped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC

from house_finder import load_ranker_config
from house_finder.util.price import format_price
from house_finder.util.quota import (
    QuotaExceededError,
    api_call_wrapper,
    check_quota,
)
from house_finder.util.secrets import looks_configured_secret

logger = logging.getLogger(__name__)

MUST_HAVE_WEIGHT = 3.0
NICE_TO_HAVE_WEIGHT = 1.0


def _listing_text(record) -> str:
    return " ".join(
        [
            record.title or "",
            record.description or "",
            " ".join(record.key_features or []),
            record.display_address or "",
        ]
    ).lower()


def _term_pattern(term: str) -> re.Pattern:
    """Word-boundary matcher for one criterion.

    Word boundaries matter: "garage" must not be satisfied by "garages nearby"
    is fine, but a substring match would let "parking" match "no parking".
    """
    return re.compile(r"(?:^|\W)" + re.escape(term.strip().lower()) + r"(?:\W|$)", re.I)


def pre_score(record, criteria: dict) -> tuple[float, list[str]]:
    """Free keyword score plus the criteria that matched.

    Returns (score, matched_terms). Used to skip LLM calls for listings that
    match nothing at all, and as the fallback score when no API key is set.
    """
    text = _listing_text(record)
    matched: list[str] = []
    score = 0.0

    for term in criteria.get("must_haves") or []:
        if term and _term_pattern(term).search(text):
            score += MUST_HAVE_WEIGHT
            matched.append(term)

    for term in criteria.get("nice_to_haves") or []:
        if term and _term_pattern(term).search(text):
            score += NICE_TO_HAVE_WEIGHT
            matched.append(term)

    return score, matched


def _fallback_score(record, criteria: dict, raw_pre_score: float) -> tuple[float, str, float]:
    """Deterministic score used when no LLM is available.

    Keyword evidence only, so confidence is deliberately low and the score is
    capped below the "excellent match" band - this must never be mistaken for
    a real assessment.
    """
    must_haves = criteria.get("must_haves") or []
    nice = criteria.get("nice_to_haves") or []
    max_possible = len(must_haves) * MUST_HAVE_WEIGHT + len(nice) * NICE_TO_HAVE_WEIGHT

    if max_possible <= 0:
        return 5.0, "No criteria configured; keyword scoring unavailable.", 0.2

    ratio = min(raw_pre_score / max_possible, 1.0)
    score = round(2.0 + ratio * 5.0, 1)  # 2.0-7.0 band
    return score, "Keyword match only (no API key configured for full scoring).", 0.3


def _needs_ranking(record, stored: dict | None, ranker_version: str) -> bool:
    """True when this listing must be sent to the LLM.

    Skips anything already scored whose content and ranker version are both
    unchanged - the single biggest cost saving in the pipeline.
    """
    if stored is None:
        return True
    if stored.get("fit_score") is None:
        return True
    if stored.get("ranker_version") != ranker_version:
        return True
    if stored.get("content_hash") != record.content_hash:
        return True
    return False


def _build_job(record, index: int) -> dict:
    """Compact per-property payload for the prompt. Short keys save tokens."""
    job = {
        "i": index,
        "title": record.title,
        "address": record.display_address,
        "price": format_price(record.price, record.listing_type)
        if record.price
        else "unspecified",
        "property_type": record.property_type,
        "bedrooms": record.bedrooms,
        "bathrooms": record.bathrooms,
    }
    if record.tenure:
        job["tenure"] = record.tenure
        if record.leasehold_years_remaining:
            job["lease_years_remaining"] = record.leasehold_years_remaining
    if record.floor_area_sqft:
        job["size"] = f"{record.floor_area_sqft:.0f} sq ft"
    if record.price_qualifier:
        job["price_qualifier"] = record.price_qualifier
    if record.price_reduced:
        job["price_reduced"] = True
    if record.key_features:
        job["features"] = record.key_features[:8]
    job["desc"] = record.description or ""

    context = {}
    if record.price_vs_local_pct is not None:
        context["vs_local_sold_prices"] = f"{record.price_vs_local_pct:+.0f}%"
    if record.local_sold_avg_price:
        context["local_sold_average"] = f"£{record.local_sold_avg_price:,}"
    if record.crime_incidents_nearby is not None:
        context["crime_incidents_nearby_12mo"] = record.crime_incidents_nearby
    if record.flood_warnings_nearby:
        context["active_flood_warnings_nearby"] = record.flood_warnings_nearby
    if record.epc_current:
        context["epc_score_current"] = record.epc_current
        if record.epc_potential:
            context["epc_score_potential"] = record.epc_potential
    if record.broadband_max_mbps:
        context["max_broadband_mbps"] = record.broadband_max_mbps
    if context:
        job["local_context"] = context

    return job


def _truncate_for_tokens(job: dict, max_desc_tokens: int) -> dict:
    """Trim the description to roughly max_desc_tokens (~4 chars per token)."""
    limit = max(200, max_desc_tokens * 4)
    if len(job.get("desc", "")) > limit:
        job["desc"] = job["desc"][:limit].rsplit(" ", 1)[0] + " ..."
    return job


def _profile_for_prompt(criteria: dict) -> dict:
    """The criteria the model needs, without the noise it does not."""
    keys = (
        "price",
        "price_pcm",
        "bedrooms_min",
        "bedrooms_max",
        "bathrooms_min",
        "property_types",
        "must_haves",
        "nice_to_haves",
        "exclusions",
        "furnished",
        "min_tenancy_months",
        "preferences_freetext",
    )
    return {k: criteria[k] for k in keys if k in criteria and criteria[k] not in (None, [], {})}


def _parse_response(text: str, expected: int) -> list[dict]:
    """Parse the model's JSON array, tolerating stray prose or code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.S)
        if not match:
            logger.error("rank: could not parse model response: %s", cleaned[:200])
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.error("rank: model response was not valid JSON")
            return []

    if not isinstance(parsed, list):
        return []
    if len(parsed) != expected:
        logger.warning("rank: expected %d scores, got %d", expected, len(parsed))
    return [p for p in parsed if isinstance(p, dict)]


def _apply_scores(batch: list, results: list[dict], ranker_version: str) -> int:
    """Attach scores to records by echoed index, not by position."""
    by_index = {}
    for result in results:
        try:
            by_index[int(result["i"])] = result
        except (KeyError, TypeError, ValueError):
            continue

    applied = 0
    for offset, record in enumerate(batch):
        result = by_index.get(offset)
        if result is None:
            continue
        try:
            record.fit_score = round(float(result.get("s", 0)), 1)
        except (TypeError, ValueError):
            continue
        try:
            record.fit_confidence = round(float(result.get("c", 0.5)), 2)
        except (TypeError, ValueError):
            record.fit_confidence = 0.5
        record.fit_reason = str(result.get("r", "") or "").strip()[:300]
        keywords = result.get("k") or []
        if isinstance(keywords, list):
            record.matched_criteria = [str(k) for k in keywords[:5]]
        record.ranker_version = ranker_version
        record.freshly_ranked = True
        applied += 1
    return applied


def rank_records(
    records: list,
    criteria: dict,
    settings: dict,
    *,
    listing_type: str = "sale",
    stored_by_id: dict[str, dict] | None = None,
    ranker_config: dict | None = None,
) -> list:
    """Score records in place. Returns the ones that were actually ranked."""
    if not records:
        return []

    config = ranker_config or load_ranker_config()
    ranker_version = str(config.get("version", "v1"))
    threshold = float(config.get("pre_score_threshold", 0.0))
    stored_by_id = stored_by_id or {}

    # Pass 1 - free, always runs, and decides who is worth paying for.
    candidates = []
    for record in records:
        raw_score, matched = pre_score(record, criteria)
        record._pre_score = raw_score  # noqa: SLF001 - transient, not persisted
        if not record.matched_criteria:
            record.matched_criteria = matched

        if raw_score < threshold:
            logger.debug(
                "rank: %s below pre-score threshold (%.1f < %.1f)",
                record.title, raw_score, threshold,
            )
            continue
        if _needs_ranking(record, stored_by_id.get(record.property_id), ranker_version):
            candidates.append(record)

    skipped = len(records) - len(candidates)
    if skipped:
        logger.info("rank: %d listings already scored or pre-filtered, not re-sent", skipped)

    if not candidates:
        return []

    if not looks_configured_secret(os.environ.get("ANTHROPIC_API_KEY")):
        logger.warning(
            "rank: ANTHROPIC_API_KEY not set - falling back to keyword-only scores "
            "for %d listings",
            len(candidates),
        )
        for record in candidates:
            score, reason, confidence = _fallback_score(
                record, criteria, getattr(record, "_pre_score", 0.0)
            )
            record.fit_score = score
            record.fit_reason = reason
            record.fit_confidence = confidence
            record.ranker_version = f"{ranker_version}-keyword"
            record.freshly_ranked = True
        return candidates

    return _rank_with_claude(
        candidates, criteria, settings, config, listing_type, ranker_version
    )


def _rank_with_claude(
    records: list,
    criteria: dict,
    settings: dict,
    config: dict,
    listing_type: str,
    ranker_version: str,
) -> list:
    from anthropic import Anthropic

    model_config = (settings.get("models") or {}).get("rank") or {}
    model = model_config.get("model", "claude-haiku-4-5")
    batch_size = int(model_config.get("batch_size", 8))
    max_tokens = int(model_config.get("max_tokens_response", 1600))
    max_desc_tokens = int(model_config.get("max_description_tokens", 900))

    mode_context = (config.get("mode_context") or {}).get(listing_type, "")
    system_prompt = config["system_prompt_template"].format(
        scoring_rubric=config.get("scoring_rubric", ""),
        mode_context=mode_context,
        profile_json=json.dumps(_profile_for_prompt(criteria), indent=2),
    )

    client = Anthropic(max_retries=4)
    batches = [records[i : i + batch_size] for i in range(0, len(records), batch_size)]

    use_batch_api = bool(model_config.get("use_batch_api", True)) and len(batches) >= 2
    ranked: list = []

    if use_batch_api:
        try:
            ranked = _rank_via_batch_api(
                client, batches, settings, config, system_prompt, model,
                max_tokens, max_desc_tokens, ranker_version,
            )
            if ranked:
                return ranked
        except QuotaExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to sync on any batch failure
            logger.warning("rank: Batches API failed (%s); falling back to sync calls", exc)

    for batch_index, batch in enumerate(batches, start=1):
        jobs = [
            _truncate_for_tokens(_build_job(r, i), max_desc_tokens)
            for i, r in enumerate(batch)
        ]
        user_prompt = config["user_prompt_template"].format(
            n=len(jobs), properties_json=json.dumps(jobs, ensure_ascii=False)
        )
        try:
            check_quota(settings)
        except QuotaExceededError as exc:
            logger.error("rank: %s", exc)
            break

        try:
            response = api_call_wrapper(
                settings,
                "rank",
                lambda: client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                ),
                label=f"batch {batch_index}/{len(batches)}",
            )
        except QuotaExceededError as exc:
            logger.error("rank: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
            logger.error("rank: batch %d failed: %s", batch_index, exc)
            continue

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        results = _parse_response(text, len(jobs))
        applied = _apply_scores(batch, results, ranker_version)
        ranked.extend(r for r in batch if r.freshly_ranked)
        logger.info("rank: batch %d/%d scored %d listings", batch_index, len(batches), applied)

    return ranked


def _rank_via_batch_api(
    client,
    batches: list[list],
    settings: dict,
    config: dict,
    system_prompt: str,
    model: str,
    max_tokens: int,
    max_desc_tokens: int,
    ranker_version: str,
) -> list:
    """Score via the Message Batches API - same tokens at half the price."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    check_quota(settings)

    requests = []
    for batch_index, batch in enumerate(batches):
        jobs = [
            _truncate_for_tokens(_build_job(r, i), max_desc_tokens)
            for i, r in enumerate(batch)
        ]
        user_prompt = config["user_prompt_template"].format(
            n=len(jobs), properties_json=json.dumps(jobs, ensure_ascii=False)
        )
        requests.append(
            Request(
                custom_id=f"batch-{batch_index}",
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                ),
            )
        )

    logger.info(
        "rank: submitting %d batches (%d listings) to the Batches API",
        len(requests), sum(len(b) for b in batches),
    )
    batch_job = client.messages.batches.create(requests=requests)

    timeout_minutes = float(
        ((settings.get("models") or {}).get("rank") or {}).get(
            "batch_poll_timeout_minutes", 15
        )
    )
    deadline = time.monotonic() + timeout_minutes * 60
    poll_interval = 10

    while time.monotonic() < deadline:
        batch_job = client.messages.batches.retrieve(batch_job.id)
        if batch_job.processing_status == "ended":
            break
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 60)
    else:
        logger.warning(
            "rank: Batches API did not finish within %.0f minutes; "
            "cancelling and falling back to sync calls",
            timeout_minutes,
        )
        try:
            client.messages.batches.cancel(batch_job.id)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        return []

    ranked: list = []
    total_in = total_out = 0

    for result in client.messages.batches.results(batch_job.id):
        if result.result.type != "succeeded":
            logger.warning(
                "rank: batch entry %s did not succeed (%s)",
                result.custom_id, result.result.type,
            )
            continue
        try:
            batch_index = int(str(result.custom_id).split("-")[-1])
        except ValueError:
            continue
        if batch_index >= len(batches):
            continue

        message = result.result.message
        usage = getattr(message, "usage", None)
        if usage:
            total_in += int(getattr(usage, "input_tokens", 0) or 0)
            total_out += int(getattr(usage, "output_tokens", 0) or 0)

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        batch = batches[batch_index]
        results = _parse_response(text, len(batch))
        _apply_scores(batch, results, ranker_version)
        ranked.extend(r for r in batch if r.freshly_ranked)

    # Batch usage is billed at the discounted rank_batch rates.
    from datetime import datetime

    from house_finder.util.quota import _append_entry, _log_path, estimate_cost_gbp

    cost = estimate_cost_gbp(settings, "rank_batch", total_in, total_out)
    _append_entry(
        _log_path(settings),
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": "rank_batch",
            "label": f"{len(requests)} batches",
            "model": model,
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cached_input_tokens": 0,
            "cost_gbp": round(cost, 6),
        },
    )
    logger.info(
        "rank: Batches API scored %d listings for £%.4f (50%% discount applied)",
        len(ranked), cost,
    )
    return ranked
