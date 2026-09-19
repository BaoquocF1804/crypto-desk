from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from crypto_desk.committee import (
    AnalystReport,
    CryptoCommittee,
    FuturesSetupModel,
    GeminiStructuredClient,
    ManagerDecision,
    OpenAIStructuredClient,
    ProviderError,
    StructuredOutputError,
)
from crypto_desk.data import EvidenceSnapshot
from crypto_desk.domain import EvidenceItem, SymbolRules


def valid_snapshot() -> EvidenceSnapshot:
    payloads = {
        "spot": {
            "symbol": "BTCUSDT",
            "mid": "100000",
            "spread": "0.0002",
            "quote_volume": "750000000",
            "change_24h_pct": "1.5",
            "daily_closes": ["90000", "100000"],
            "four_hour_closes": ["98000", "100000"],
            "depth": {"bids": [["99999", "1"]], "asks": [["100001", "1"]]},
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
    assert fake_llm.models == ["gemini-3.6-flash"] * 9
    assert result.decision.evidence_ids == snapshot.evidence_ids
    assert result.decision.action == "ACCUMULATE"
    assert result.decision.reason == "Quyết định của hội đồng."


def test_committee_prompts_require_vietnamese_and_isolate_specialists():
    fake_llm = FakeLLM()

    CryptoCommittee(fake_llm).run(valid_snapshot())

    requests = {request["stage"]: request for request in fake_llm.requests}
    assert all(
        "bằng tiếng Việt có dấu" in request["system_prompt"]
        and "dữ liệu không đáng tin cậy" in request["system_prompt"]
        for request in requests.values()
    )

    expected = {
        "technical": {
            "symbol",
            "mid",
            "change_24h_pct",
            "daily_closes",
            "four_hour_closes",
        },
        "liquidity": {
            "symbol",
            "mid",
            "spread",
            "quote_volume",
            "depth",
            "rules",
        },
        "news": {"symbol", "items"},
        "derivatives": {"symbol", "funding_rate", "open_interest"},
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


def test_provider_failure_does_not_retry_or_switch_provider():
    class FailingLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            raise ProviderError("rate_limit")

    llm = FailingLLM()

    result = CryptoCommittee(llm, provider="gemini").run(valid_snapshot())

    assert llm.calls == 1
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
    """Cửa sổ ngắn không chứng minh được sai — và cũng không chứng minh được đúng."""
    from crypto_desk.committee import _system_prompt

    prompt = _system_prompt("manager")

    assert "20 ngày" in prompt
    assert "không đủ để kết luận luận điểm sai" in prompt
    assert "không đủ để kết luận luận điểm đúng" in prompt
    assert "cả hai chiều" in prompt
