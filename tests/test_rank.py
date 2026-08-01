"""Ranking: keyword pre-score, cost gating, and response parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from house_finder.pipeline.rank import (
    _apply_scores,
    _build_job,
    _needs_ranking,
    _parse_response,
    pre_score,
    rank_records,
)

CRITERIA = {
    "must_haves": ["garden", "off-street parking"],
    "nice_to_haves": ["garage", "no chain"],
    "price": {"min": 0, "max": 300000},
}

RANKER_CONFIG = {
    "version": "v1-test",
    "pre_score_threshold": 0.0,
    "scoring_rubric": "rubric",
    "system_prompt_template": "{scoring_rubric}{mode_context}{profile_json}",
    "user_prompt_template": "{n}{properties_json}",
    "mode_context": {"sale": "sale context", "rent": "rent context"},
}


def test_pre_score_weights_must_haves_higher(record_factory):
    with_must = record_factory(description="Lovely garden to the rear.", key_features=[])
    with_nice = record_factory(description="Includes a garage.", key_features=[])

    must_score, must_matched = pre_score(with_must, CRITERIA)
    nice_score, _ = pre_score(with_nice, CRITERIA)

    assert must_score > nice_score
    assert "garden" in must_matched


def test_pre_score_is_zero_when_nothing_matches(record_factory):
    record = record_factory(description="A property.", key_features=[], title="")
    score, matched = pre_score(record, CRITERIA)
    assert score == 0.0
    assert matched == []


def test_pre_score_respects_word_boundaries(record_factory):
    """'garage' must not be matched by the word 'garages' inside another word."""
    record = record_factory(description="No gardening required.", key_features=[], title="")
    score, matched = pre_score(record, {"must_haves": ["garden"], "nice_to_haves": []})
    assert "garden" not in matched


def test_needs_ranking_for_a_new_property():
    assert _needs_ranking(SimpleNamespace(content_hash="a"), None, "v1")


def test_skips_ranking_when_nothing_changed():
    record = SimpleNamespace(content_hash="abc")
    stored = {"fit_score": 7.0, "ranker_version": "v1", "content_hash": "abc"}
    assert not _needs_ranking(record, stored, "v1")


def test_re_ranks_when_the_listing_changed():
    record = SimpleNamespace(content_hash="new-hash")
    stored = {"fit_score": 7.0, "ranker_version": "v1", "content_hash": "old-hash"}
    assert _needs_ranking(record, stored, "v1")


def test_re_ranks_when_the_ranker_was_upgraded():
    record = SimpleNamespace(content_hash="abc")
    stored = {"fit_score": 7.0, "ranker_version": "v0", "content_hash": "abc"}
    assert _needs_ranking(record, stored, "v1")


def test_parse_response_handles_plain_json():
    parsed = _parse_response('[{"i":0,"s":8.0,"c":0.9,"r":"Good","k":["garden"]}]', 1)
    assert parsed[0]["s"] == 8.0


def test_parse_response_handles_code_fences():
    text = '```json\n[{"i":0,"s":7.0,"c":0.8,"r":"OK","k":[]}]\n```'
    assert _parse_response(text, 1)[0]["s"] == 7.0


def test_parse_response_survives_surrounding_prose():
    text = 'Here you go:\n[{"i":0,"s":6.0,"c":0.7,"r":"Fine","k":[]}]\nHope that helps.'
    assert _parse_response(text, 1)[0]["s"] == 6.0


def test_parse_response_returns_empty_on_garbage():
    assert _parse_response("not json at all", 1) == []


def test_scores_are_matched_by_echoed_index_not_position(record_factory):
    """Out-of-order results must not attach the wrong score to a property."""
    batch = [
        record_factory(property_id="a", url="http://a"),
        record_factory(property_id="b", url="http://b"),
    ]
    results = [
        {"i": 1, "s": 9.0, "c": 0.9, "r": "Second property", "k": []},
        {"i": 0, "s": 3.0, "c": 0.8, "r": "First property", "k": []},
    ]
    _apply_scores(batch, results, "v1")

    assert batch[0].fit_score == 3.0
    assert batch[1].fit_score == 9.0
    assert batch[0].fit_reason == "First property"


def test_apply_scores_ignores_entries_without_an_index(record_factory):
    batch = [record_factory()]
    _apply_scores(batch, [{"s": 9.0}], "v1")
    assert batch[0].fit_score is None


def test_build_job_includes_enrichment_context(record_factory):
    record = record_factory(
        price_vs_local_pct=12.5, local_sold_avg_price=222000, crime_incidents_nearby=300
    )
    job = _build_job(record, 0)
    assert job["local_context"]["vs_local_sold_prices"] == "+12%"
    assert job["local_context"]["local_sold_average"] == "£222,000"


def test_falls_back_to_keyword_scores_without_an_api_key(record_factory, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    records = [record_factory(description="Large garden and off-street parking.")]

    ranked = rank_records(records, CRITERIA, {}, ranker_config=RANKER_CONFIG)

    assert len(ranked) == 1
    assert records[0].fit_score is not None
    assert "no api key" in records[0].fit_reason.lower()
    assert records[0].fit_confidence <= 0.3, "keyword-only scores must be low confidence"


def test_pre_score_threshold_skips_hopeless_listings(record_factory, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = {**RANKER_CONFIG, "pre_score_threshold": 1.0}
    record = record_factory(description="Nothing relevant here.", key_features=[], title="")

    ranked = rank_records([record], CRITERIA, {}, ranker_config=config)
    assert ranked == []


def test_llm_path_scores_records(record_factory, monkeypatch):
    """The Claude path parses a response and attaches scores."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-abcdefgh")

    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps([
            {"i": 0, "s": 8.5, "c": 0.9, "r": "Garden and parking both present.",
             "k": ["garden", "off-street parking"]},
        ]))],
        usage=SimpleNamespace(input_tokens=500, output_tokens=60, cache_read_input_tokens=0),
    )

    client = MagicMock()
    client.messages.create.return_value = response

    settings = {
        "models": {
            "rank": {
                "model": "claude-haiku-4-5",
                "batch_size": 8,
                "use_batch_api": False,
                "input_gbp_per_million": 0.64,
                "output_gbp_per_million": 3.20,
            }
        },
        "quota_soft_cap_gbp": 0,
    }

    records = [record_factory(description="Large garden and off-street parking.")]

    with patch("anthropic.Anthropic", return_value=client):
        ranked = rank_records(
            records, CRITERIA, settings, ranker_config=RANKER_CONFIG
        )

    assert len(ranked) == 1
    assert records[0].fit_score == 8.5
    assert records[0].fit_reason == "Garden and parking both present."
    assert records[0].ranker_version == "v1-test"
    assert records[0].freshly_ranked is True


def test_batch_api_is_used_when_there_are_enough_batches(record_factory, monkeypatch):
    """Two or more batches should go through the discounted Batches API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-abcdefgh")

    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="batch_1")
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        id="batch_1", processing_status="ended"
    )
    client.messages.batches.results.return_value = [
        SimpleNamespace(
            custom_id="batch-0",
            result=SimpleNamespace(
                type="succeeded",
                message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=json.dumps(
                        [{"i": 0, "s": 7.0, "c": 0.8, "r": "Fine", "k": []}]
                    ))],
                    usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                ),
            ),
        ),
        SimpleNamespace(
            custom_id="batch-1",
            result=SimpleNamespace(
                type="succeeded",
                message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=json.dumps(
                        [{"i": 0, "s": 9.0, "c": 0.9, "r": "Excellent", "k": []}]
                    ))],
                    usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                ),
            ),
        ),
    ]

    settings = {
        "models": {
            "rank": {"model": "claude-haiku-4-5", "batch_size": 1, "use_batch_api": True},
            "rank_batch": {"input_gbp_per_million": 0.32, "output_gbp_per_million": 1.60},
        },
        "quota_soft_cap_gbp": 0,
    }

    records = [
        record_factory(property_id="a", url="http://a"),
        record_factory(property_id="b", url="http://b"),
    ]

    with patch("anthropic.Anthropic", return_value=client):
        ranked = rank_records(records, CRITERIA, settings, ranker_config=RANKER_CONFIG)

    client.messages.batches.create.assert_called_once()
    client.messages.create.assert_not_called()
    assert len(ranked) == 2
    assert {r.fit_score for r in records} == {7.0, 9.0}
