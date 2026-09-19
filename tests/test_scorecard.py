from __future__ import annotations

from decimal import Decimal

from crypto_desk.scorecard import build_scorecard


def _reflection(
    symbol: str,
    action: str | None,
    alpha: str,
    adverse: str = "-0.02",
    cutoff: str | None = None,
    created_at: str = "2026-09-01T00:00:00+00:00",
) -> dict:
    payload = {
        "realized_return": "0.05",
        "maximum_adverse_excursion": adverse,
        "maximum_favorable_excursion": "0.08",
        "benchmark_return": "0.01",
        "alpha": alpha,
    }
    if action is not None:
        payload["decision_action"] = action
    if cutoff is not None:
        payload["decision_cutoff"] = cutoff
    return {
        "run_id": f"{symbol}-{action}-{alpha}",
        "symbol": symbol,
        "created_at": created_at,
        "payload": payload,
    }


def test_groups_by_action_and_computes_mean_median_and_correct_direction_rate():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.02"),
            _reflection("SOLUSDT", "ACCUMULATE", "0.04"),
            _reflection("SUIUSDT", "ACCUMULATE", "-0.03"),
            _reflection("BNBUSDT", "HOLD", "0.01"),
        ]
    )
    scores = {score.action: score for score in card.scores}
    assert scores["ACCUMULATE"].samples == 3
    assert scores["ACCUMULATE"].mean_alpha == Decimal("0.01")
    assert scores["ACCUMULATE"].median_alpha == Decimal("0.02")
    assert scores["ACCUMULATE"].correct_direction_rate == Decimal("0.6667")
    assert scores["ACCUMULATE"].mean_worst_excursion == Decimal("-0.02")
    assert scores["HOLD"].samples == 1
    assert card.scored == 4


def test_median_of_even_sample_averages_the_middle_pair():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "HOLD", "0.01"),
            _reflection("SOLUSDT", "HOLD", "0.03"),
        ]
    )
    assert card.scores[0].median_alpha == Decimal("0.02")


def test_benchmark_symbol_is_excluded_because_its_alpha_is_zero_by_construction():
    card = build_scorecard(
        [
            _reflection("BTCUSDT", "ACCUMULATE", "0"),
            _reflection("ETHUSDT", "ACCUMULATE", "0.04"),
        ]
    )
    assert card.skipped_benchmark == 1
    assert card.scored == 1
    assert card.scores[0].mean_alpha == Decimal("0.04")


def test_legacy_rows_without_decision_action_are_skipped_and_counted():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", None, "0.04"),
            _reflection("SOLUSDT", "HOLD", "0.01"),
        ]
    )
    assert card.skipped_no_action == 1
    assert card.scored == 1


def test_empty_input_renders_without_crashing():
    card = build_scorecard([])
    assert card.scored == 0
    assert "Chưa đủ dữ liệu" in card.render()


def test_render_states_horizon_and_benchmark():
    card = build_scorecard([_reflection("ETHUSDT", "ACCUMULATE", "0.04")])
    rendered = card.render()
    assert "20 ngày" in rendered
    assert "BTCUSDT" in rendered
    assert "ACCUMULATE" in rendered
    assert "+4.00%" in rendered


def test_step_away_actions_score_negative_alpha_as_the_correct_call():
    """Alpha là long-only: NO_TRADE/EXIT/REDUCE đúng khi tài sản thua benchmark."""
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "NO_TRADE", "-0.04", cutoff="2026-07-01T00:00:00+00:00"),
            _reflection("SOLUSDT", "NO_TRADE", "0.04", cutoff="2026-07-02T00:00:00+00:00"),
            _reflection("SUIUSDT", "EXIT", "-0.01", cutoff="2026-07-03T00:00:00+00:00"),
            _reflection("BNBUSDT", "REDUCE", "0.01", cutoff="2026-07-04T00:00:00+00:00"),
        ]
    )
    scores = {score.action: score for score in card.scores}
    assert scores["NO_TRADE"].correct_direction_rate == Decimal("0.5")
    assert scores["EXIT"].correct_direction_rate == Decimal(1)
    assert scores["REDUCE"].correct_direction_rate == Decimal(0)
    # Trung bình alpha giữ nguyên dấu long-only, không đảo theo action.
    assert scores["NO_TRADE"].mean_alpha == Decimal(0)


def test_holding_actions_still_score_positive_alpha_as_correct():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.04"),
            _reflection("SOLUSDT", "HOLD", "-0.04"),
        ]
    )
    scores = {score.action: score for score in card.scores}
    assert scores["ACCUMULATE"].correct_direction_rate == Decimal(1)
    assert scores["HOLD"].correct_direction_rate == Decimal(0)


def test_distinct_days_collapse_same_symbol_same_decision_day_runs():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "NO_TRADE", "0.04", cutoff="2026-07-17T01:00:00+00:00"),
            _reflection("ETHUSDT", "NO_TRADE", "0.05", cutoff="2026-07-17T09:00:00+00:00"),
            _reflection("SOLUSDT", "NO_TRADE", "0.06", cutoff="2026-07-17T09:00:00+00:00"),
        ]
    )
    assert card.scores[0].samples == 3
    assert card.scores[0].distinct_days == 2


def test_distinct_days_fall_back_to_created_at_for_legacy_rows():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "HOLD", "0.04", created_at="2026-08-07T00:00:00+00:00"),
            _reflection("ETHUSDT", "HOLD", "0.05", created_at="2026-08-07T11:00:00+00:00"),
        ]
    )
    assert card.scores[0].distinct_days == 1


def test_unknown_action_is_counted_so_the_header_arithmetic_closes():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "SCALE_IN", "0.04"),
            _reflection("SOLUSDT", "HOLD", "0.01"),
        ]
    )
    assert card.skipped_unknown_action == 1
    assert card.scored == 1
    assert "1 action lạ" in card.render()


def test_actions_are_derived_from_the_domain_literal():
    from typing import get_args

    from crypto_desk.domain import Action
    from crypto_desk.scorecard import ACTIONS

    assert set(ACTIONS) == set(get_args(Action))


def test_render_explains_long_only_alpha_and_overlapping_windows():
    rendered = build_scorecard([_reflection("ETHUSDT", "NO_TRADE", "-0.04")]).render()
    assert "Đúng hướng" in rendered
    assert "Thắng" not in rendered
    assert "long" in rendered
    assert "chồng lấn" in rendered
    assert "tệ nhất" in rendered.lower()
    assert "sụt sâu" not in rendered.lower()


def test_statistics_are_quantized_for_json_consumers():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "HOLD", "0.01"),
            _reflection("SOLUSDT", "HOLD", "0.01"),
            _reflection("SUIUSDT", "HOLD", "-0.01"),
        ]
    )
    score = card.scores[0]
    assert str(score.correct_direction_rate) == "0.6667"
    assert str(score.mean_alpha) == "0.0033"
