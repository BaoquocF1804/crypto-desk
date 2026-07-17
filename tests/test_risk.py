from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_desk.config import RiskSettings
from crypto_desk.domain import ResearchDecision, SymbolRules
from crypto_desk.risk import (
    build_ticket,
    round_down,
    size_buy,
    size_sell,
    validate_prices,
)


@pytest.fixture
def rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
    )


@pytest.fixture
def default_inputs(rules: SymbolRules) -> dict:
    return {
        "environment": "testnet",
        "nav_usdt": Decimal("10000"),
        "free_usdt": Decimal("5000"),
        "entry": Decimal("100000"),
        "stop": Decimal("90000"),
        "current_symbol_value": Decimal("0"),
        "current_gross_value": Decimal("0"),
        "mainnet_order_cap_usdt": Decimal("25"),
        "completed_mainnet_chains": 0,
        "risk": RiskSettings(
            per_trade=Decimal("0.005"),
            max_symbol=Decimal("0.20"),
            max_gross=Decimal("0.80"),
            min_usdt_reserve=Decimal("0.20"),
        ),
        "rules": rules,
    }


def test_half_percent_risk_is_tightest(default_inputs):
    result = size_buy(**default_inputs)

    assert result.quantity == Decimal("0.005")
    assert result.notional_usdt == Decimal("500.000")
    assert result.limiting_rule == "per_trade"


def test_symbol_room_can_be_tightest(default_inputs):
    default_inputs["current_symbol_value"] = Decimal("1900")

    result = size_buy(**default_inputs)

    assert result.quantity == Decimal("0.001")
    assert result.limiting_rule == "max_symbol"
    assert result.rooms["max_symbol"] == Decimal("100.00")


def test_gross_room_can_be_tightest(default_inputs):
    default_inputs["current_gross_value"] = Decimal("7900")

    assert size_buy(**default_inputs).limiting_rule == "max_gross"


def test_reserve_room_can_be_tightest(default_inputs):
    default_inputs["free_usdt"] = Decimal("2100")

    assert size_buy(**default_inputs).limiting_rule == "usdt_reserve"


def test_mainnet_cap_is_tightest_until_twenty_reconciled_chains(default_inputs):
    default_inputs["environment"] = "mainnet"

    capped = size_buy(**default_inputs)
    default_inputs["completed_mainnet_chains"] = 20
    graduated = size_buy(**default_inputs)

    assert capped.notional_usdt == Decimal("25.00000")
    assert capped.limiting_rule == "mainnet_cap"
    assert graduated.notional_usdt == Decimal("500.000")
    assert graduated.limiting_rule == "per_trade"


def test_mainnet_initial_cap_cannot_be_configured_above_25(default_inputs):
    default_inputs["environment"] = "mainnet"
    default_inputs["mainnet_order_cap_usdt"] = Decimal("100")

    result = size_buy(**default_inputs)

    assert result.notional_usdt == Decimal("25.00000")
    assert result.rooms["mainnet_cap"] == Decimal("25")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stop", Decimal("100000"), "stop"),
        ("nav_usdt", Decimal("0"), "NAV"),
        ("current_symbol_value", Decimal("2000"), "room"),
        ("current_gross_value", Decimal("8000"), "room"),
        ("free_usdt", Decimal("2000"), "room"),
    ],
)
def test_invalid_stop_or_exhausted_room_is_blocked(
    default_inputs,
    field,
    value,
    message,
):
    default_inputs[field] = value

    with pytest.raises(ValueError, match=message):
        size_buy(**default_inputs)


def test_quantity_below_min_notional_is_blocked(default_inputs):
    default_inputs["nav_usdt"] = Decimal("10")
    default_inputs["free_usdt"] = Decimal("10")

    with pytest.raises(ValueError, match="minNotional"):
        size_buy(**default_inputs)


def test_round_down_and_price_tick_validation_are_exact(rules):
    assert round_down(Decimal("0.001239"), rules.step_size) == Decimal("0.00123")
    validate_prices(
        Decimal("100000.00"),
        Decimal("95000.00"),
        Decimal("110000.00"),
        rules,
    )

    with pytest.raises(ValueError, match="tick"):
        validate_prices(
            Decimal("100000.001"),
            Decimal("95000.00"),
            Decimal("110000.00"),
            rules,
        )


def test_long_only_sell_uses_free_balance_and_never_locked_balance(rules):
    reduced = size_sell(
        action="REDUCE",
        free_base=Decimal("0.01001"),
        price=Decimal("100000"),
        rules=rules,
    )
    exited = size_sell(
        action="EXIT",
        free_base=Decimal("0.01001"),
        price=Decimal("100000"),
        rules=rules,
    )

    assert reduced.quantity == Decimal("0.00500")
    assert exited.quantity == Decimal("0.01001")
    assert exited.quantity <= Decimal("0.01001")


def test_sell_below_minimum_and_unsupported_action_are_blocked(rules):
    with pytest.raises(ValueError, match="minNotional"):
        size_sell(
            action="EXIT",
            free_base=Decimal("0.00001"),
            price=Decimal("100000"),
            rules=rules,
        )
    with pytest.raises(ValueError, match="REDUCE or EXIT"):
        size_sell(
            action="HOLD",
            free_base=Decimal("1"),
            price=Decimal("100000"),
            rules=rules,
        )
    with pytest.raises(ValueError, match="tick"):
        size_sell(
            action="EXIT",
            free_base=Decimal("1"),
            price=Decimal("100000.001"),
            rules=rules,
        )


def test_build_ticket_maps_accumulate_to_open_and_preserves_decimal_values(rules):
    decision = ResearchDecision(
        symbol="BTCUSDT",
        action="ACCUMULATE",
        conviction=Decimal("7"),
        bull_case="Bull",
        bear_case="Bear",
        catalysts=("Catalyst",),
        invalidation="Invalidation",
        entry=Decimal("100000.00"),
        stop=Decimal("95000.00"),
        target=Decimal("110000.00"),
        evidence_ids=("evidence-1",),
        reason="committee decision",
    )

    ticket = build_ticket(
        environment="testnet",
        decision=decision,
        quantity=Decimal("0.00025"),
        current_position_quantity=Decimal("0"),
        rules=rules,
        risk_snapshot={"limiting_rule": "mainnet_cap"},
        ttl_minutes=30,
    )

    assert ticket.intent == "OPEN"
    assert ticket.side == "BUY"
    assert ticket.notional_usdt == Decimal("25.0000000")
    assert ticket.limit_price == decision.entry
