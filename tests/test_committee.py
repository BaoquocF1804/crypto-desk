from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from crypto_desk.committee import (
    AnalystReport,
    CryptoCommittee,
    ManagerDecision,
    OpenAIStructuredClient,
)
from crypto_desk.data import EvidenceSnapshot
from crypto_desk.domain import EvidenceItem, SymbolRules


def valid_snapshot() -> EvidenceSnapshot:
    items = tuple(
        EvidenceItem.create(
            kind=kind,
            provider="fixture",
            source=f"fixture://{kind}",
            fetched_at="2026-07-17T00:15:00+00:00",
            as_of="2026-07-17T00:15:00+00:00",
            delayed=False,
            stale=False,
            payload={"symbol": "BTCUSDT", "value": value},
        )
        for kind, value in (
            ("spot", "100000"),
            ("news", "current"),
            ("derivatives", "0.0001"),
            ("reference", "100000"),
        )
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
        self.invalid_for: set[str] = set()
        self.unknown_evidence_for: set[str] = set()

    def generate(
        self,
        *,
        stage: str,
        model: str,
        response_model: type,
        system_prompt: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(stage)
        self.models.append(model)
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
    ]
    assert fake_llm.models[:4] == ["gpt-5.4-mini"] * 4
    assert fake_llm.models[4:] == ["gpt-5.5"] * 5
    assert result.decision.evidence_ids == snapshot.evidence_ids
    assert result.decision.action == "ACCUMULATE"


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
        response_model=AnalystReport,
        system_prompt="Bounded role",
        payload={"evidence_ids": ["evidence-1"]},
    )

    assert parsed.stance == "neutral"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["text_format"] is AnalystReport
    assert "tools" not in captured
    assert captured["input"][0] == {
        "role": "system",
        "content": "Bounded role",
    }
