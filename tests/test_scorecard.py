from __future__ import annotations

from decimal import Decimal

from crypto_desk.scorecard import build_scorecard


def _reflection(
    symbol: str,
    action: str | None,
    realized: str,
    alpha: str = "0",
    *,
    adverse: str = "-0.02",
    cutoff: str | None = None,
    created_at: str = "2026-09-01T00:00:00+00:00",
    benchmark: str | None = None,
    barrier: str | None = None,
    r_multiple: str | None = None,
) -> dict:
    payload = {
        "realized_return": realized,
        "maximum_adverse_excursion": adverse,
        "maximum_favorable_excursion": "0.08",
        "benchmark_return": "0.01",
        "alpha": alpha,
    }
    if action is not None:
        payload["decision_action"] = action
    if cutoff is not None:
        payload["decision_cutoff"] = cutoff
    if benchmark is not None:
        payload["benchmark_symbol"] = benchmark
    if barrier is not None:
        payload["barrier_outcome"] = barrier
        payload["r_multiple"] = r_multiple
    return {
        "run_id": f"{symbol}-{action}-{realized}-{cutoff}-{created_at}",
        "symbol": symbol,
        "created_at": created_at,
        "payload": payload,
    }


def _scores(card, group: int = 0):
    return {score.action: score for score in card.groups[group].scores}


def test_direction_is_judged_on_absolute_return_because_the_alternative_is_usdt():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.02", alpha="-0.01"),
            _reflection("SOLUSDT", "ACCUMULATE", "0.04"),
            _reflection("SUIUSDT", "ACCUMULATE", "-0.03"),
            _reflection("BNBUSDT", "NO_TRADE", "-0.01", alpha="0.02"),
        ]
    )
    scores = _scores(card)
    assert scores["ACCUMULATE"].samples == 3
    assert scores["ACCUMULATE"].mean_return == Decimal("0.01")
    assert scores["ACCUMULATE"].correct_direction_rate == Decimal("0.6667")
    assert scores["ACCUMULATE"].mean_worst_excursion == Decimal("-0.02")
    # NO_TRADE đúng vì tài sản giảm, dù nó vẫn hơn benchmark.
    assert scores["NO_TRADE"].correct_direction_rate == Decimal(1)
    assert card.scored == 4


def test_step_away_actions_are_correct_when_the_asset_fell():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "NO_TRADE", "-0.04"),
            _reflection("SOLUSDT", "NO_TRADE", "0.04"),
            _reflection("SUIUSDT", "EXIT", "-0.01"),
            _reflection("BNBUSDT", "REDUCE", "0.01"),
        ]
    )
    scores = _scores(card)
    assert scores["NO_TRADE"].correct_direction_rate == Decimal("0.5")
    assert scores["EXIT"].correct_direction_rate == Decimal(1)
    assert scores["REDUCE"].correct_direction_rate == Decimal(0)


def test_benchmark_symbol_is_scored_but_left_out_of_the_alpha_mean():
    card = build_scorecard(
        [
            _reflection("BTCUSDT", "ACCUMULATE", "0.06", alpha="0"),
            _reflection("ETHUSDT", "ACCUMULATE", "0.02", alpha="0.04"),
        ]
    )
    score = _scores(card)["ACCUMULATE"]
    assert score.samples == 2
    assert score.mean_return == Decimal("0.04")
    assert score.mean_alpha == Decimal("0.04")


def test_alpha_is_unavailable_when_only_the_benchmark_was_decided():
    card = build_scorecard([_reflection("BTCUSDT", "ACCUMULATE", "0.06")])

    assert _scores(card)["ACCUMULATE"].mean_alpha is None
    assert "—" in card.render()


def test_repeat_runs_of_one_symbol_day_and_action_count_once_using_the_latest():
    card = build_scorecard(
        [
            _reflection("BTCUSDT", "NO_TRADE", "0.01", cutoff="2026-09-25T05:54:00+00:00"),
            _reflection("BTCUSDT", "NO_TRADE", "0.03", cutoff="2026-09-25T06:29:00+00:00"),
            _reflection("BTCUSDT", "ACCUMULATE", "0.02", cutoff="2026-09-25T06:35:00+00:00"),
            _reflection("BTCUSDT", "NO_TRADE", "0.05", cutoff="2026-09-26T06:00:00+00:00"),
        ]
    )
    scores = _scores(card)
    assert scores["NO_TRADE"].samples == 2
    assert scores["NO_TRADE"].mean_return == Decimal("0.04")
    assert scores["ACCUMULATE"].samples == 1
    assert card.collapsed == 1
    assert card.scored == 3


def test_legacy_rows_collapse_by_their_recording_day():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "HOLD", "0.04", created_at="2026-08-07T00:00:00+00:00"),
            _reflection("ETHUSDT", "HOLD", "0.05", created_at="2026-08-07T11:00:00+00:00"),
        ]
    )
    assert _scores(card)["HOLD"].samples == 1
    assert _scores(card)["HOLD"].mean_return == Decimal("0.05")


def test_setups_report_target_first_rate_and_expectancy_in_r():
    card = build_scorecard(
        [
            _reflection("BTCUSDT", "ACCUMULATE", "0.03", barrier="target", r_multiple="2.5"),
            _reflection("ETHUSDT", "ACCUMULATE", "-0.02", barrier="stop", r_multiple="-1"),
            _reflection("SOLUSDT", "ACCUMULATE", "-0.03", barrier="stop", r_multiple="-1"),
            _reflection("SUIUSDT", "ACCUMULATE", "0.01", barrier="open", r_multiple="0.3"),
            _reflection("BNBUSDT", "ACCUMULATE", "0.01"),
        ]
    )
    score = _scores(card)["ACCUMULATE"]
    assert score.resolved == 3
    assert score.target_first_rate == Decimal("0.3333")
    assert score.mean_r_multiple == Decimal("0.2")
    assert "+0.20R" in card.render()


def test_actions_without_setups_have_no_barrier_statistics():
    score = _scores(build_scorecard([_reflection("ETHUSDT", "HOLD", "0.01")]))["HOLD"]

    assert score.resolved == 0
    assert score.target_first_rate is None
    assert score.mean_r_multiple is None


def test_legacy_rows_without_decision_action_are_skipped_and_counted():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", None, "0.04"),
            _reflection("SOLUSDT", "HOLD", "0.01"),
        ]
    )
    assert card.skipped_no_action == 1
    assert card.scored == 1


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


def test_empty_input_renders_without_crashing():
    card = build_scorecard([])
    assert card.scored == 0
    assert "Chưa đủ dữ liệu" in card.render()


def test_actions_are_derived_from_the_domain_literal():
    from typing import get_args

    from crypto_desk.domain import Action
    from crypto_desk.scorecard import ACTIONS

    assert set(ACTIONS) == set(get_args(Action))


def test_render_states_horizon_benchmark_and_how_each_column_is_measured():
    rendered = build_scorecard([_reflection("ETHUSDT", "ACCUMULATE", "0.04")]).render()

    assert "20 ngày" in rendered
    assert "BTCUSDT" in rendered
    assert "+4.00%" in rendered
    assert "USDT" in rendered
    assert "chồng lấn" in rendered
    assert "tệ nhất" in rendered.lower()
    assert "stop" in rendered


def test_statistics_are_quantized_for_json_consumers():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "HOLD", "0.01", alpha="0.01"),
            _reflection("SOLUSDT", "HOLD", "0.01", alpha="0.01"),
            _reflection("SUIUSDT", "HOLD", "-0.01", alpha="-0.01"),
        ]
    )
    score = card.groups[0].scores[0]
    assert str(score.correct_direction_rate) == "0.6667"
    assert str(score.mean_alpha) == "0.0033"
    assert str(score.mean_return) == "0.0033"


def test_alpha_against_different_benchmarks_never_shares_a_mean():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.05", alpha="0.04", benchmark="BTCUSDT"),
            _reflection("FPT", "ACCUMULATE", "0.05", alpha="-0.10", benchmark="VN30"),
        ]
    )
    by = {g.benchmark: g for g in card.groups}

    assert set(by) == {"BTCUSDT", "VN30"}
    assert by["BTCUSDT"].scores[0].mean_alpha == Decimal("0.04")
    assert by["VN30"].scores[0].mean_alpha == Decimal("-0.10")


def test_legacy_row_without_benchmark_is_inferred_from_the_symbol_and_marked():
    card = build_scorecard([_reflection("ETHUSDT", "HOLD", "0.01")])
    group = card.groups[0]

    assert group.benchmark == "BTCUSDT"
    assert group.inferred is True
    assert "suy ra" in card.render()


def test_render_shows_one_table_per_benchmark():
    rendered = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.05", benchmark="BTCUSDT"),
            _reflection("FPT", "ACCUMULATE", "0.05", benchmark="VN30"),
        ]
    ).render()

    assert "VN30" in rendered
    assert rendered.count("Action") == 2
