from __future__ import annotations

from decimal import Decimal

from crypto_desk.scorecard import build_scorecard


def _reflection(
    symbol: str,
    action: str | None,
    alpha: str,
    adverse: str = "-0.02",
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
    return {
        "run_id": f"{symbol}-{action}-{alpha}",
        "symbol": symbol,
        "created_at": "2026-09-01T00:00:00+00:00",
        "payload": payload,
    }


def test_groups_by_action_and_computes_mean_median_hit_rate():
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
    assert scores["ACCUMULATE"].hit_rate == Decimal(2) / Decimal(3)
    assert scores["ACCUMULATE"].mean_adverse_excursion == Decimal("-0.02")
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
