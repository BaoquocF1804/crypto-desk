from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from crypto_desk.committee import (
    AnalystReport,
    CryptoCommittee,
    DeepSeekStructuredClient,
    FuturesSetupModel,
    GeminiStructuredClient,
    ManagerDecision,
    OpenAIStructuredClient,
    ProviderError,
    StructuredOutputError,
)
from crypto_desk.data import EvidenceSnapshot
from crypto_desk.domain import EvidenceItem, SymbolRules


def valid_snapshot(atr14_1d: str | None = "2500") -> EvidenceSnapshot:
    payloads = {
        "spot": {
            "symbol": "BTCUSDT",
            "mid": "100000",
            "spread": "0.0002",
            "quote_volume": "750000000",
            "change_24h_pct": "1.5",
            "daily_closes": ["90000", "100000"],
            "four_hour_closes": ["98000", "100000"],
            "technical_indicators": {
                "version": "technical-v1",
                "daily_as_of": "2026-07-16T23:59:59.999000+00:00",
                "four_hour_as_of": "2026-07-16T23:59:59.999000+00:00",
                "ema20_1d": "99000",
                "ema50_1d": "95000",
                "rsi14_4h": "62.5",
                "atr14_4h": "1000",
                "atr14_1d": atr14_1d,
            },
            "depth": {"bids": [["99999", "1"]], "asks": [["100001", "1"]]},
            "depth_summary": {
                "bid_levels": 1,
                "ask_levels": 1,
                "bid_qty": "1",
                "ask_qty": "1",
                "bid_notional_usdt": "99999.00",
                "ask_notional_usdt": "100001.00",
                "ask_to_bid_qty_ratio": "1.00",
            },
            "rules": {"tick_size": "0.01"},
        },
        "news": {
            "symbol": "BTCUSDT",
            "items": [
                {
                    "title": "Current Bitcoin headline",
                    "url": "https://example.test/news",
                    "published_at": "2026-07-17T00:00:00+00:00",
                    "content_hash": "news-hash",
                }
            ],
        },
        "derivatives": {
            "symbol": "BTCUSDT",
            "funding_rate": "0.0001",
            "open_interest": "120000",
            "funding_rate_trend": "stable",
            "oi_change_1h_pct": "0.1",
            "long_short_ratio": "2.5",
            "top_trader_ratio": "1.9",
            "taker_buy_sell_ratio": "0.65",
            "oi_change_24h_pct": "1.2",
            "taker_buy_sell_ratio_24h": "0.98",
            "long_short_ratio_pctile_20d": "40",
            "top_trader_ratio_pctile_20d": "55",
        },
        "reference": {
            "symbol": "BTCUSDT",
            "reference_usdt": "100000",
            "deviation": "0",
        },
    }
    items = tuple(
        EvidenceItem.create(
            kind=kind,
            provider="fixture",
            source=f"fixture://{kind}",
            fetched_at="2026-07-17T00:15:00+00:00",
            as_of="2026-07-17T00:15:00+00:00",
            delayed=False,
            stale=False,
            payload=payloads[kind],
        )
        for kind in ("spot", "news", "derivatives", "reference")
    )
    return EvidenceSnapshot(
        symbol="BTCUSDT",
        cutoff="2026-07-17T00:15:00+00:00",
        rules=SymbolRules(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
        ),
        items=items,
        binance_mid=Decimal("100000"),
        reference_usdt=Decimal("100000"),
        spread=Decimal("0.0002"),
        quote_volume=Decimal("750000000"),
        daily_closes=(Decimal("90000"),) * 119 + (Decimal("100000"),),
        four_hour_closes=(Decimal("98000"),) * 179 + (Decimal("100000"),),
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("120000"),
        news_count=1,
    )


class FakeLLM:
    def __init__(self):
        self.calls: list[str] = []
        self.models: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.invalid_for: set[str] = set()
        self.unknown_evidence_for: set[str] = set()

    def generate(
        self,
        *,
        stage: str,
        model: str,
        thinking: str,
        response_model: type,
        system_prompt: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(stage)
        self.models.append(model)
        self.requests.append(
            {
                "stage": stage,
                "system_prompt": system_prompt,
                "payload": payload,
            }
        )
        if stage in self.invalid_for:
            return {"invalid": True}
        evidence_ids = payload["evidence_ids"]
        if stage in self.unknown_evidence_for:
            evidence_ids = ["unknown-evidence"]
        if response_model is AnalystReport:
            return {
                "stance": "bullish",
                "confidence": "7",
                "observations": ["Xu hướng được evidence hỗ trợ."],
                "risks": ["Biến động Spot."],
                "evidence_ids": evidence_ids,
            }
        assert response_model is ManagerDecision
        return {
            "action": "ACCUMULATE",
            "conviction": "7",
            "decision_reason": "Spot trend supports an entry above nearby support.",
            "bull_case": "Xu hướng và thanh khoản đồng thuận.",
            "bear_case": "Funding có thể đảo chiều.",
            "catalysts": ["Dòng tiền Spot duy trì."],
            "invalidation": "Cấu trúc xu hướng bị phá vỡ.",
            "entry": "100000",
            "stop": "95000",
            "target": "110000",
            "evidence_ids": evidence_ids,
        }

    def count(self, stage: str) -> int:
        return self.calls.count(stage)


def test_committee_runs_specialists_before_exactly_two_debate_rounds():
    fake_llm = FakeLLM()
    snapshot = valid_snapshot()

    result = CryptoCommittee(fake_llm).run(snapshot)

    assert fake_llm.calls == [
        "technical",
        "liquidity",
        "news",
        "derivatives",
        "bull_round_1",
        "bear_round_1",
        "bull_round_2",
        "bear_round_2",
        "manager",
        "manager_confirm",
    ]
    assert fake_llm.models == ["gemini-3.6-flash"] * 10
    assert result.decision.evidence_ids == snapshot.evidence_ids
    assert result.decision.action == "ACCUMULATE"
    assert (
        "Computed gross R:R: (110000 - 100000) / (100000 - 95000) = 2.00" in result.decision.reason
    )


def test_committee_prompts_require_english_and_isolate_specialists():
    fake_llm = FakeLLM()

    CryptoCommittee(fake_llm).run(valid_snapshot())

    requests = {request["stage"]: request for request in fake_llm.requests}
    assert all(
        "in English" in request["system_prompt"]
        and "untrusted data" in request["system_prompt"]
        and "Evidence Snapshot as the only source" in request["system_prompt"]
        for request in requests.values()
    )

    expected = {
        "technical": {
            "symbol",
            "mid",
            "change_24h_pct",
            "daily_closes",
            "four_hour_closes",
            "technical_indicators",
        },
        "liquidity": {
            "symbol",
            "mid",
            "spread",
            "quote_volume",
            "depth",
            "depth_summary",
            "rules",
        },
        "news": {"symbol", "items"},
        # Hourly OI change and taker ratio stay out: noise for a 20-day thesis.
        "derivatives": {
            "symbol",
            "funding_rate",
            "open_interest",
            "funding_rate_trend",
            "long_short_ratio",
            "top_trader_ratio",
            "oi_change_24h_pct",
            "taker_buy_sell_ratio_24h",
            "long_short_ratio_pctile_20d",
            "top_trader_ratio_pctile_20d",
        },
    }
    for role, fields in expected.items():
        payload = requests[role]["payload"]
        evidence = payload["snapshot"]["evidence"]
        assert set(evidence["payload"]) == fields
        assert payload["evidence_ids"] == [evidence["id"]]
        assert "reports" not in payload
        assert "reflections" not in payload
        assert "position_quantity" not in payload

    assert requests["bull_round_1"]["payload"]["round_number"] == 1
    assert requests["bear_round_2"]["payload"]["round_number"] == 2
    assert requests["manager"]["payload"]["stage"] == "manager"
    assert requests["manager"]["payload"]["position_quantity"] == "0"
    assert (
        requests["technical"]["payload"]["snapshot"]["evidence"]["payload"]["technical_indicators"][
            "rsi14_4h"
        ]
        == "62.5"
    )
    assert "closed-candle as_of" in requests["technical"]["system_prompt"]
    for stage in ("bull_round_1", "bear_round_1"):
        prompt = requests[stage]["system_prompt"]
        assert "asymmetry" in prompt
        assert "invalidat" in prompt
    manager_prompt = requests["manager"]["system_prompt"]
    assert "(target - entry) / (entry - stop)" in manager_prompt
    assert "R:R >= 1.5" in manager_prompt
    assert "decision_reason" in manager_prompt
    assert "otherwise use an empty list" in manager_prompt
    assert "null" in requests["derivatives"]["system_prompt"]
    assert "pctile_20d" in requests["derivatives"]["system_prompt"]
    assert "not a directional signal" in requests["liquidity"]["system_prompt"]
    assert all(
        "20-day thesis horizon" in request["system_prompt"] for request in requests.values()
    )


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_debate_payloads_hide_hour_scale_fields_but_keep_what_prompts_require():
    fake_llm = FakeLLM()
    snapshot = valid_snapshot()

    CryptoCommittee(fake_llm).run(snapshot)

    debate = [r for r in fake_llm.requests if r["stage"].startswith(("bull", "bear", "manager"))]
    assert debate
    for request in debate:
        keys = _all_keys(request["payload"]["snapshot"])
        # Specialists đã đọc các trường này; ở horizon 20 ngày chúng chỉ là nhiễu bị viện dẫn.
        assert not {"depth", "oi_change_1h_pct", "taker_buy_sell_ratio"} & keys
        # Chuỗi close chỉ đi một lần, trong item spot.
        assert "daily_closes" not in request["payload"]["snapshot"]
        assert {
            "atr14_1d",
            "depth_summary",
            "taker_buy_sell_ratio_24h",
            "long_short_ratio_pctile_20d",
            "daily_closes",
        } <= keys
        assert request["payload"]["evidence_ids"] == list(snapshot.evidence_ids)


def test_invalid_manager_schema_retries_once_then_no_trade():
    fake_llm = FakeLLM()
    fake_llm.invalid_for = {"manager"}

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "structured output" in result.decision.reason


def test_unknown_evidence_id_retries_once_then_no_trade():
    fake_llm = FakeLLM()
    fake_llm.unknown_evidence_for = {"manager"}

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "evidence" in result.decision.reason


def test_reduce_without_position_is_forced_to_no_trade():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def reduce(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["action"] = "REDUCE"
            response["entry"] = None
            response["stop"] = None
            response["target"] = None
        return response

    fake_llm.generate = reduce

    result = CryptoCommittee(fake_llm).run(
        valid_snapshot(),
        position_quantity=Decimal("0"),
    )

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "position" in result.decision.reason


def test_invalid_accumulate_price_order_retries_then_no_trade():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def invalid_prices(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["stop"] = "101000"
        return response

    fake_llm.generate = invalid_prices

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "stop < entry < target" in result.decision.reason


def test_accumulate_stands_only_when_an_independent_manager_rerun_agrees():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def disagreeing(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager_confirm":
            response.update(
                action="NO_TRADE",
                decision_reason="Taker flow at 0.65 leaves no clear edge to add.",
            )
        return response

    fake_llm.generate = disagreeing
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "NO_TRADE"
    assert result.decision.decided
    assert "confirmation" in result.decision.reason
    assert "Taker flow at 0.65" in result.decision.reason


def test_decisions_other_than_accumulate_skip_the_confirmation_rerun():
    fake_llm = FakeLLM()
    manager_levels(fake_llm, action="NO_TRADE")

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "NO_TRADE"
    assert fake_llm.count("manager_confirm") == 0


def test_crypto_accumulate_requires_minimum_gross_reward_risk():
    for target, action in (("107500", "ACCUMULATE"), ("107000", "NO_TRADE")):
        fake_llm = FakeLLM()
        original_generate = fake_llm.generate

        def with_target(**kwargs):
            response = original_generate(**kwargs)
            if kwargs["stage"] == "manager":
                response["target"] = target
                response["decision_reason"] = "Proposed Spot USDT setup has a narrow upside edge."
            return response

        fake_llm.generate = with_target
        result = CryptoCommittee(fake_llm).run(valid_snapshot())

        assert result.decision.action == action
        if action == "NO_TRADE":
            assert "gross R:R" in result.decision.reason
            assert fake_llm.count("manager") == 2
        else:
            assert "= 1.50" in result.decision.reason


def manager_levels(fake_llm: FakeLLM, **levels: str) -> None:
    original_generate = fake_llm.generate

    def with_levels(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(levels)
        return response

    fake_llm.generate = with_levels


def test_accumulate_stop_must_sit_at_least_one_daily_atr_below_entry():
    # Fixture ATR14_1d is 2500 (ATR14_4h 1000) and entry is 100000; both setups keep
    # gross R:R at 2.00, so only the stop distance decides.
    for stop, target, action in (
        ("97500", "105000", "ACCUMULATE"),
        ("97501", "104998", "NO_TRADE"),
    ):
        fake_llm = FakeLLM()
        manager_levels(fake_llm, stop=stop, target=target)

        result = CryptoCommittee(fake_llm).run(valid_snapshot())

        assert result.decision.action == action
        if action == "NO_TRADE":
            assert "ATR14_1d" in result.decision.reason
            assert fake_llm.count("manager") == 2


def test_accumulate_without_daily_atr_is_rejected():
    fake_llm = FakeLLM()

    result = CryptoCommittee(fake_llm).run(valid_snapshot(atr14_1d=None))

    assert result.decision.action == "NO_TRADE"
    assert "ATR14_1d" in result.decision.reason


def test_reduce_is_not_blocked_by_the_accumulate_atr_stop_floor():
    fake_llm = FakeLLM()
    manager_levels(fake_llm, action="REDUCE", stop="99900", target="101000")

    result = CryptoCommittee(fake_llm).run(valid_snapshot(), position_quantity=Decimal("1"))

    assert result.decision.action == "REDUCE"


def test_crypto_hold_preserves_quantified_manager_reason():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def hold(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(
                action="HOLD",
                entry=None,
                stop=None,
                target=None,
                decision_reason="Only 0 of 2 defensible risk levels are available for a new entry.",
            )
        return response

    fake_llm.generate = hold
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "HOLD"
    assert result.decision.reason.startswith("Only 0 of 2")


def test_no_trade_with_levels_uses_computed_rr_instead_of_model_claim():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def no_trade(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(
                action="NO_TRADE",
                decision_reason="Taker buying is weak at 0.92 despite a rising Daily trend.",
            )
        return response

    fake_llm.generate = no_trade
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "NO_TRADE"
    assert result.decision.decided
    assert (
        "Computed gross R:R: (110000 - 100000) / (100000 - 95000) = 2.00" in result.decision.reason
    )


@pytest.mark.parametrize(
    "reason",
    [
        "The EMA20 stop implies a gross R:R of 0.82.",
        "Gross R:R is unquantifiable until a breakout confirms direction.",
    ],
)
def test_no_trade_rejects_rr_prose_conflicting_with_stored_levels(reason: str):
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def inconsistent(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(action="NO_TRADE", decision_reason=reason)
        return response

    fake_llm.generate = inconsistent
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert not result.decision.decided
    assert "decision_reason must omit R:R" in result.decision.reason
    assert [call.status for call in result.calls[-2:]] == ["failure", "failure"]


def test_manager_retries_with_consistency_guidance_and_accepts_correction():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def corrected(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(
                action="NO_TRADE",
                decision_reason=(
                    "Gross R:R of 0.82 blocks adding."
                    if fake_llm.count("manager") == 1
                    else "Crowded long positioning at 2.70 blocks adding."
                ),
            )
        return response

    fake_llm.generate = corrected
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.decided
    assert result.decision.reason.startswith("Crowded long positioning at 2.70")
    assert "= 2.00" in result.decision.reason
    assert "previous manager response failed validation" in fake_llm.requests[-1]["system_prompt"]


def test_no_trade_rejects_partial_spot_levels():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def partial(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(action="NO_TRADE", target=None)
        return response

    fake_llm.generate = partial
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert not result.decision.decided
    assert "Spot levels must be all present or all null" in result.decision.reason


def test_no_trade_rejects_unordered_spot_levels():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def unordered(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(action="NO_TRADE", stop="101000")
        return response

    fake_llm.generate = unordered
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert not result.decision.decided
    assert "stop < entry < target" in result.decision.reason


def test_no_trade_rejects_stop_price_in_reason_when_levels_are_null():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def unstructured_stop(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.update(
                action="NO_TRADE",
                entry=None,
                stop=None,
                target=None,
                decision_reason=(
                    "Ask depth exceeds bid depth 4.16 to 1.96. Using a structural "
                    "stop below Daily EMA20 at 80350.00 USDT offers insufficient upside."
                ),
            )
        return response

    fake_llm.generate = unstructured_stop
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert not result.decision.decided
    assert "must omit entry/stop/target price claims" in result.decision.reason


def test_crypto_manager_requires_decision_reason():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def without_reason(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response.pop("decision_reason")
        return response

    fake_llm.generate = without_reason
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "NO_TRADE"
    assert fake_llm.count("manager") == 2


def test_hallucinated_entry_far_from_mid_is_rejected():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def far_entry(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["entry"] = "150000"
            response["stop"] = "140000"
            response["target"] = "160000"
        return response

    fake_llm.generate = far_entry

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "deviates" in result.decision.reason


def test_prose_percentages_no_longer_force_no_trade():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def prose(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["bull_case"] = "Khối lượng Spot tăng khoảng 35% so với tuần trước."
        return response

    fake_llm.generate = prose

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "ACCUMULATE"


def test_reduce_with_position_but_missing_prices_is_rejected():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def reduce_without_prices(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["action"] = "REDUCE"
            response["entry"] = None
            response["stop"] = None
            response["target"] = None
        return response

    fake_llm.generate = reduce_without_prices

    result = CryptoCommittee(fake_llm).run(
        valid_snapshot(),
        position_quantity=Decimal("1"),
    )

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "entry" in result.decision.reason


def test_stale_snapshot_skips_all_llm_calls():
    fake_llm = FakeLLM()
    snapshot = valid_snapshot()
    stale_item = replace(snapshot.items[0], stale=True)
    snapshot = replace(snapshot, items=(stale_item, *snapshot.items[1:]))

    result = CryptoCommittee(fake_llm).run(snapshot)

    assert fake_llm.calls == []
    assert result.decision.action == "NO_TRADE"
    assert "evidence" in result.decision.reason


def test_openai_adapter_uses_responses_parse_without_tools():
    captured: dict[str, Any] = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=AnalystReport(
                    stance="neutral",
                    confidence=Decimal("5"),
                    observations=["Không có lợi thế rõ ràng."],
                    risks=[],
                    evidence_ids=["evidence-1"],
                )
            )

    parsed = OpenAIStructuredClient(SimpleNamespace(responses=Responses())).generate(
        stage="technical",
        model="gpt-5.4-mini",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    assert parsed.stance == "neutral"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["text_format"] is AnalystReport
    assert "tools" not in captured
    assert captured["input"][0] == {
        "role": "system",
        "content": "Bounded role",
    }


def test_gemini_adapter_uses_json_schema_without_server_storage():
    captured: dict[str, Any] = {}

    class Interactions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text=(
                    '{"stance":"neutral","confidence":"5",'
                    '"observations":["Không có lợi thế rõ ràng."],'
                    '"risks":[],"evidence_ids":["evidence-1"]}'
                )
            )

    parsed = GeminiStructuredClient(SimpleNamespace(interactions=Interactions())).generate(
        stage="technical",
        model="gemini-3.6-flash",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    assert parsed.stance == "neutral"
    assert captured["model"] == "gemini-3.6-flash"
    assert captured["system_instruction"] == "Bounded role"
    assert captured["generation_config"] == {"thinking_level": "low"}
    assert captured["store"] is False
    assert captured["response_format"][0]["mime_type"] == "application/json"
    assert captured["response_format"][0]["schema"] == AnalystReport.model_json_schema()


@pytest.mark.parametrize(
    ("model", "thinking", "level", "budget"),
    [
        ("gemini-3.8-flash", "high", "HIGH", None),
        ("gemini-3.8-flash", "low", "LOW", None),
        ("gemini-2.5-pro", "high", None, 2048),
    ],
)
def test_vertex_adapter_sets_thinking_per_model_generation(model, thinking, level, budget):
    # Gemini 3 bỏ qua thinking_budget khi có response_schema: phải gửi thinking_level.
    captured: dict[str, Any] = {}

    class Models:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text=(
                    '{"stance":"neutral","confidence":"5",'
                    '"observations":["Không có lợi thế rõ ràng."],'
                    '"risks":[],"evidence_ids":["evidence-1"]}'
                )
            )

    GeminiStructuredClient(SimpleNamespace(vertexai=True, models=Models())).generate(
        stage="manager",
        model=model,
        thinking=thinking,
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    thinking_config = captured["config"].thinking_config
    assert thinking_config.thinking_level == level
    assert thinking_config.thinking_budget == budget


def test_gemini_adapter_paces_consecutive_requests():
    current = [0.0]
    delays: list[float] = []

    def sleep(delay: float) -> None:
        delays.append(delay)
        current[0] += delay

    interaction = SimpleNamespace(
        output_text=(
            '{"stance":"neutral","confidence":"5",'
            '"observations":["Không có lợi thế rõ ràng."],'
            '"risks":[],"evidence_ids":["evidence-1"]}'
        )
    )
    client = GeminiStructuredClient(
        SimpleNamespace(interactions=SimpleNamespace(create=lambda **kwargs: interaction)),
        min_interval_seconds=6,
        clock=lambda: current[0],
        sleep=sleep,
    )
    kwargs = {
        "stage": "technical",
        "model": "gemini-3.6-flash",
        "thinking": "low",
        "response_model": AnalystReport,
        "system_prompt": "Bounded role",
        "payload": {"evidence_ids": ["evidence-1"]},
    }

    client.generate(**kwargs)
    client.generate(**kwargs)

    assert delays == [6]


def test_gemini_adapter_retries_rate_limit_after_provider_delay():
    delays: list[float] = []
    attempts = {"count": 0}

    class RateLimitError(Exception):
        status_code = 429

    def create(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RateLimitError("Please retry in 2.5s")
        return SimpleNamespace(
            output_text=(
                '{"stance":"neutral","confidence":"5",'
                '"observations":["Không có lợi thế rõ ràng."],'
                '"risks":[],"evidence_ids":["evidence-1"]}'
            )
        )

    parsed = GeminiStructuredClient(
        SimpleNamespace(interactions=SimpleNamespace(create=create)),
        sleep=delays.append,
    ).generate(
        stage="technical",
        model="gemini-3.6-flash",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    assert parsed.stance == "neutral"
    assert attempts["count"] == 2
    assert delays == [3.5]


def test_gemini_adapter_rejects_empty_or_invalid_output():
    empty = GeminiStructuredClient(
        SimpleNamespace(
            interactions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(output_text=""))
        )
    )
    invalid = GeminiStructuredClient(
        SimpleNamespace(
            interactions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(output_text="{}"))
        )
    )

    for client in (empty, invalid):
        with pytest.raises(StructuredOutputError):
            client.generate(
                stage="technical",
                model="gemini-3.6-flash",
                thinking="low",
                response_model=AnalystReport,
                system_prompt="Bounded role",
                payload={"evidence_ids": ["evidence-1"]},
            )


def test_deepseek_adapter_generates_and_parses_json_schema():
    captured: dict[str, Any] = {}

    class ChatCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"stance":"neutral","confidence":"5",'
                                '"observations":["Không có lợi thế rõ ràng."],'
                                '"risks":[],"evidence_ids":["evidence-1"]}'
                            )
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=ChatCompletions()))
    adapter = DeepSeekStructuredClient(client)

    parsed = adapter.generate(
        stage="technical",
        model="deepseek-chat",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    assert parsed.stance == "neutral"
    assert captured["model"] == "deepseek-chat"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["reasoning_effort"] == "low"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert (
        "You must return a valid JSON object strictly matching this JSON schema:"
        in captured["messages"][0]["content"]
    )


def test_deepseek_adapter_strips_markdown_codeblock():
    class ChatCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '```json\n{"stance":"neutral","confidence":"5",'
                                '"observations":["Không có lợi thế rõ ràng."],'
                                '"risks":[],"evidence_ids":["evidence-1"]}\n```'
                            )
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=ChatCompletions()))
    adapter = DeepSeekStructuredClient(client)

    parsed = adapter.generate(
        stage="technical",
        model="deepseek-chat",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )
    assert parsed.stance == "neutral"


def test_deepseek_adapter_raises_structured_output_error_on_empty_or_invalid():
    empty = DeepSeekStructuredClient(
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
                    )
                )
            )
        )
    )
    invalid = DeepSeekStructuredClient(
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
                    )
                )
            )
        )
    )

    for client in (empty, invalid):
        with pytest.raises(StructuredOutputError):
            client.generate(
                stage="technical",
                model="deepseek-chat",
                thinking="low",
                response_model=AnalystReport,
                system_prompt="Bounded role",
                payload={"evidence_ids": ["evidence-1"]},
            )


def test_deepseek_adapter_handles_provider_errors_and_retries():
    attempts = [0]

    class RateLimitThenSuccess:
        def create(self, **kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                err = Exception("Rate limit hit")
                setattr(err, "status_code", 429)
                raise err
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"stance":"neutral","confidence":"5",'
                                '"observations":["Không có lợi thế rõ ràng."],'
                                '"risks":[],"evidence_ids":["evidence-1"]}'
                            )
                        )
                    )
                ]
            )

    slept: list[float] = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=RateLimitThenSuccess()))
    adapter = DeepSeekStructuredClient(client, sleep=slept.append)

    parsed = adapter.generate(
        stage="technical",
        model="deepseek-chat",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )
    assert parsed.stance == "neutral"
    assert attempts[0] == 2
    assert len(slept) == 1

    # Authentication failure raises ProviderError immediately
    class AuthErrorClient:
        def create(self, **kwargs):
            err = Exception("Unauthorized")
            setattr(err, "status_code", 401)
            raise err

    auth_client = SimpleNamespace(chat=SimpleNamespace(completions=AuthErrorClient()))
    auth_adapter = DeepSeekStructuredClient(auth_client)
    with pytest.raises(ProviderError) as exc_info:
        auth_adapter.generate(
            stage="technical",
            model="deepseek-chat",
            thinking="low",
            response_model=AnalystReport,
            system_prompt="Bounded role",
            payload={"evidence_ids": ["evidence-1"]},
        )
    assert exc_info.value.category == "authentication"


def test_deepseek_adapter_fallbacks_when_thinking_unsupported():
    calls = []

    class ChatCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "extra_body" in kwargs:
                raise Exception("extra_body thinking is unrecognized parameter")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"stance":"neutral","confidence":"5",'
                                '"observations":["Không có lợi thế rõ ràng."],'
                                '"risks":[],"evidence_ids":["evidence-1"]}'
                            )
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=ChatCompletions()))
    adapter = DeepSeekStructuredClient(client)

    parsed = adapter.generate(
        stage="technical",
        model="deepseek-chat",
        thinking="low",
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )
    assert parsed.stance == "neutral"
    assert len(calls) == 2
    assert "extra_body" in calls[0]
    assert "extra_body" not in calls[1]


def test_provider_failure_does_not_retry_or_switch_provider():
    class FailingLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            raise ProviderError("rate_limit")

    llm = FailingLLM()

    result = CryptoCommittee(llm, provider="gemini").run(valid_snapshot())

    assert llm.calls == 2
    assert result.decision.action == "NO_TRADE"
    assert result.decision.reason == "provider:rate_limit"
    assert result.calls[0].provider == "gemini"
    assert result.calls[0].error_category == "rate_limit"


def test_futures_setup_validation():
    # Valid LONG setup
    long_setup = FuturesSetupModel(
        direction="LONG",
        entry=Decimal("100"),
        stop=Decimal("95"),
        target=Decimal("110"),
        risk_reward_ratio=Decimal("2.0"),
        rationale="Hỗ trợ mạnh và cá mập tích lũy.",
    )
    assert long_setup.direction == "LONG"
    assert long_setup.stop < long_setup.entry < long_setup.target

    # Invalid LONG setup: stop >= entry
    with pytest.raises(ValueError, match="LONG setup must satisfy stop < entry < target"):
        FuturesSetupModel(
            direction="LONG",
            entry=Decimal("100"),
            stop=Decimal("105"),
            target=Decimal("110"),
            risk_reward_ratio=Decimal("2.0"),
            rationale="Invalid",
        )

    # Valid SHORT setup
    short_setup = FuturesSetupModel(
        direction="SHORT",
        entry=Decimal("100"),
        stop=Decimal("105"),
        target=Decimal("90"),
        risk_reward_ratio=Decimal("2.0"),
        rationale="Kháng cự mạnh và funding quá nóng.",
    )
    assert short_setup.direction == "SHORT"
    assert short_setup.target < short_setup.entry < short_setup.stop

    # Invalid SHORT setup: stop <= entry
    with pytest.raises(ValueError, match="SHORT setup must satisfy target < entry < stop"):
        FuturesSetupModel(
            direction="SHORT",
            entry=Decimal("100"),
            stop=Decimal("95"),
            target=Decimal("90"),
            risk_reward_ratio=Decimal("2.0"),
            rationale="Invalid",
        )

    with pytest.raises(ValueError, match="risk_reward_ratio does not match"):
        FuturesSetupModel(
            direction="LONG",
            entry=Decimal("100"),
            stop=Decimal("95"),
            target=Decimal("110"),
            risk_reward_ratio=Decimal("1.5"),
            rationale="Incorrect arithmetic",
        )


def test_committee_returns_futures_bias_and_setups():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def manager_with_futures(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response["futures_bias"] = "BULLISH"
            response["futures_setups"] = [
                {
                    "direction": "LONG",
                    "entry": Decimal("100000"),
                    "stop": Decimal("98000"),
                    "target": Decimal("105000"),
                    "risk_reward_ratio": Decimal("2.5"),
                    "rationale": "Quét thanh lý Long xong bật tăng.",
                },
                {
                    "direction": "SHORT",
                    "entry": Decimal("105000"),
                    "stop": Decimal("107000"),
                    "target": Decimal("100000"),
                    "risk_reward_ratio": Decimal("2.5"),
                    "rationale": "Chạm kháng cự cứng trên khung Daily.",
                },
            ]
        return response

    fake_llm.generate = manager_with_futures
    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.futures_bias == "BULLISH"
    assert len(result.decision.futures_setups) == 2
    assert result.decision.futures_setups[0].direction == "LONG"
    assert result.decision.futures_setups[0].entry == Decimal("100000")
    assert result.decision.futures_setups[1].direction == "SHORT"
    assert result.decision.futures_setups[1].stop == Decimal("107000")


def test_committee_includes_prior_thesis_in_payload_and_records_continuity():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def manager_with_continuity(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager":
            response["thesis_continuity"] = "CONTINUED"
        return response

    fake_llm.generate = manager_with_continuity
    snapshot = valid_snapshot()
    prior_thesis = {
        "run_id": "test-prior-run-123",
        "cutoff": "2026-07-16T20:00:00+00:00",
        "hours_ago": "4.2",
        "action": "ACCUMULATE",
        "conviction": "7.5",
        "prior_price": "98000",
        "current_price": "100000",
        "price_change_pct": "2.04",
        "bull_case": "Xu hướng tiếp diễn",
        "bear_case": "Biến động",
        "catalysts": ["Spot inflow"],
        "invalidation": "Thủng 95000",
        "entry": "98000",
        "stop": "95000",
        "target": "105000",
    }

    result = CryptoCommittee(fake_llm).run(snapshot, prior_thesis=prior_thesis)

    assert result.decision.thesis_continuity == "CONTINUED"
    assert result.decision.prior_run_id == "test-prior-run-123"

    manager_call = next(req for req in fake_llm.requests if req["stage"] == "manager")
    assert "prior_thesis" in manager_call["payload"]
    assert manager_call["payload"]["prior_thesis"]["run_id"] == "test-prior-run-123"

    bull_call = next(req for req in fake_llm.requests if req["stage"] == "bull_round_1")
    assert "prior_thesis" in bull_call["payload"]
    assert bull_call["payload"]["prior_thesis"]["action"] == "ACCUMULATE"


def test_output_contract_states_the_reflection_window_cuts_both_ways():
    """A short window is insufficient to conclude wrong, and insufficient to conclude right."""
    from crypto_desk.committee import _system_prompt

    prompt = _system_prompt("manager")

    assert "20-day" in prompt
    assert "insufficient to conclude a thesis is wrong" in prompt
    assert "insufficient to conclude a thesis is right" in prompt
    assert "both directions" in prompt


def test_crypto_behaviour_is_unchanged_when_no_new_arguments_are_passed():
    from crypto_desk.committee import (
        ROLE_PROMPTS,
        SPECIALISTS,
        SPECIALIST_EVIDENCE,
        CryptoCommittee,
    )

    committee = CryptoCommittee(object())

    assert committee.specialists == SPECIALISTS
    assert committee.role_prompts == ROLE_PROMPTS
    assert committee.specialist_evidence == SPECIALIST_EVIDENCE
    assert committee.optional_kinds == frozenset()
    assert committee.mid_label == "Binance mid"


def test_required_kinds_are_derived_from_the_evidence_map_excluding_optional():
    from crypto_desk.committee import SPECIALIST_EVIDENCE, CryptoCommittee

    committee = CryptoCommittee(object())

    assert committee.required_kinds == {kind for kind, _ in SPECIALIST_EVIDENCE.values()} | {
        "reference"
    }
    assert committee.required_kinds == {"spot", "news", "derivatives", "reference"}

    vn_committee = CryptoCommittee(
        object(),
        specialists=("technical", "cơ bản"),
        specialist_evidence={
            "technical": ("spot", ("symbol", "mid")),
            "cơ bản": ("fundamentals", ("symbol",)),
        },
        optional_kinds=frozenset({"fundamentals"}),
    )
    assert vn_committee.required_kinds == {"spot", "reference"}
    assert vn_committee.optional_kinds == frozenset({"fundamentals"})


def test_a_specialist_whose_evidence_is_absent_is_skipped_not_fatal():
    from crypto_desk.committee import CryptoCommittee

    committee = CryptoCommittee(
        object(),
        specialists=("technical", "cơ bản"),
        specialist_evidence={
            "technical": ("spot", ("symbol", "mid")),
            "cơ bản": ("fundamentals", ("symbol",)),
        },
        optional_kinds=frozenset({"fundamentals"}),
    )
    snapshot = valid_snapshot()  # chỉ có spot/news/derivatives/reference

    payload = committee._specialist_payload(snapshot, "cơ bản")

    assert payload is None


def test_rate_limited_stage_is_retried_before_the_run_is_abandoned():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate
    remaining = {"manager": 1}

    def flaky(**kwargs):
        stage = kwargs["stage"]
        if remaining.get(stage):
            remaining[stage] -= 1
            raise ProviderError("rate_limit")
        return original_generate(**kwargs)

    fake_llm.generate = flaky

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is True
    assert result.decision.action == "ACCUMULATE"
    manager_calls = [call for call in result.calls if call.stage == "manager"]
    assert [call.status for call in manager_calls] == ["failure", "success"]
    assert manager_calls[0].error_category == "rate_limit"


def test_authentication_error_aborts_the_run_without_retrying():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def unauthorized(**kwargs):
        if kwargs["stage"] == "manager":
            raise ProviderError("authentication")
        return original_generate(**kwargs)

    fake_llm.generate = unauthorized

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is False
    manager_calls = [call for call in result.calls if call.stage == "manager"]
    assert len(manager_calls) == 1
    assert manager_calls[0].error_category == "authentication"


def test_rate_limited_specialist_is_retried_and_later_reports_survive():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate
    remaining = {"liquidity": 1}

    def flaky(**kwargs):
        stage = kwargs["stage"]
        if remaining.get(stage):
            remaining[stage] -= 1
            raise ProviderError("rate_limit")
        return original_generate(**kwargs)

    fake_llm.generate = flaky

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is True
    assert set(result.reports) == {
        "technical",
        "liquidity",
        "news",
        "derivatives",
        "bull_round_1",
        "bear_round_1",
        "bull_round_2",
        "bear_round_2",
    }
    liquidity_calls = [call for call in result.calls if call.stage == "liquidity"]
    assert [call.status for call in liquidity_calls] == ["failure", "success"]
    assert result.skipped_specialists == ()
