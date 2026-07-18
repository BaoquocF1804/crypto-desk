from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_desk.commands import (
    EXECUTION_KINDS,
    FORBIDDEN_RESULT_KEYS,
    MAX_ARGS_BYTES,
    STATE_CHANGING_KINDS,
    ExecuteArgs,
    SafeExecuteResult,
    SafePreviewResult,
    SafeSyncResult,
    UnsafeResultError,
    canonical_json,
    command_hash,
    ensure_safe_result,
    parse_args,
    ticket_fingerprint,
)
from crypto_desk.domain import TradeTicket, iso

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def make_ticket(**overrides) -> TradeTicket:
    values = dict(
        id="ticket-1",
        environment="testnet",
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000.00"),
        stop_price=Decimal("95000.00"),
        target_price=Decimal("110000.00"),
        notional_usdt=Decimal("25.0000000"),
        risk_snapshot={"limiting_rule": "per_trade"},
        created_at=iso(NOW),
        expires_at=iso(NOW + timedelta(minutes=30)),
    )
    values.update(overrides)
    return TradeTicket(**values)


def test_parse_args_accepts_every_kind_and_rejects_extras():
    assert parse_args("sync", {}).model_dump() == {}
    assert parse_args("analyze", {"symbol": "btcusdt"}).symbol == "BTCUSDT"
    assert parse_args("reflections", {}).symbol is None
    assert (
        parse_args(
            "preview",
            {"action": "approve", "ticket_id": "t-1"},
        ).action
        == "approve"
    )
    execute = parse_args(
        "execute",
        {
            "action": "reconcile",
            "ticket_id": "t-1",
            "preview_command_id": "c-1",
            "fingerprint": "a" * 64,
        },
    )
    assert isinstance(execute, ExecuteArgs)
    with pytest.raises(ValueError):
        parse_args("sync", {"extra": 1})
    with pytest.raises(ValueError):
        parse_args("analyze", {"symbol": "BTCUSDT; DROP"})
    with pytest.raises(ValueError):
        parse_args(
            "preview",
            {"action": "submit", "ticket_id": "t"},
        )
    with pytest.raises(ValueError):
        parse_args("shutdown", {})


def test_args_size_fails_closed():
    with pytest.raises(ValueError, match="4 KiB"):
        parse_args(
            "analyze",
            {"symbol": "B" * (MAX_ARGS_BYTES + 1)},
        )


def test_canonical_json_and_command_hash_are_stable():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    first = command_hash(
        "cmd-1",
        "analyze",
        {"symbol": "BTCUSDT"},
    )
    assert first == command_hash(
        "cmd-1",
        "analyze",
        {"symbol": "BTCUSDT"},
    )
    assert first != command_hash(
        "cmd-1",
        "analyze",
        {"symbol": "ETHUSDT"},
    )
    assert len(first) == 64


def test_ticket_fingerprint_changes_with_any_covered_field():
    base = ticket_fingerprint(make_ticket())
    assert base == ticket_fingerprint(make_ticket())
    assert base != ticket_fingerprint(make_ticket(status="APPROVED"))
    assert base != ticket_fingerprint(make_ticket(quantity=Decimal("0.00026")))
    assert len(base) == 64


def test_ensure_safe_result_blocks_forbidden_keys_exactly():
    good = SafeSyncResult(
        environment="testnet",
        nav_usdt="1000",
        free_usdt="500",
        positions_count=2,
        open_orders_count=1,
        as_of=iso(NOW),
    )
    dumped = ensure_safe_result(good)
    assert dumped["nav_usdt"] == "1000"

    class Sneaky(SafeSyncResult):
        model_config = {"extra": "allow"}

    bad = Sneaky(
        environment="testnet",
        nav_usdt="1",
        free_usdt="1",
        positions_count=0,
        open_orders_count=0,
        as_of=iso(NOW),
    )
    bad.__pydantic_extra__["client_order_id"] = "leak"
    with pytest.raises(
        UnsafeResultError,
        match="client_order_id",
    ):
        ensure_safe_result(bad)


def test_every_safe_result_model_declares_no_forbidden_field():
    from crypto_desk import commands

    for name in dir(commands):
        model = getattr(commands, name)
        if isinstance(model, type) and name.startswith("Safe") and hasattr(model, "model_fields"):
            for field in model.model_fields:
                assert field.lower() not in FORBIDDEN_RESULT_KEYS, f"{name}.{field}"


def test_preview_and_execute_results_round_trip():
    preview = SafePreviewResult(
        action="approve",
        ticket_id="t-1",
        environment="testnet",
        execution_mode="DRY_RUN",
        status="PENDING",
        symbol="BTCUSDT",
        side="BUY",
        intent="OPEN",
        quantity="0.00025",
        notional_usdt="25",
        entry="100000.00",
        stop="95000.00",
        target="110000.00",
        created_at=iso(NOW),
        expires_at=iso(NOW + timedelta(minutes=30)),
        submission_status=None,
        fingerprint="a" * 64,
    )
    assert ensure_safe_result(preview)["fingerprint"] == "a" * 64
    execute = SafeExecuteResult(
        action="approve",
        ticket_id="t-1",
        status="APPROVED_DRY_RUN",
    )
    assert ensure_safe_result(execute)["status"] == "APPROVED_DRY_RUN"
    assert "execute" in EXECUTION_KINDS and "execute" in STATE_CHANGING_KINDS


def test_result_size_fails_closed():
    result = SafeExecuteResult(
        action="approve",
        ticket_id="x" * 40000,
        status="S",
    )
    with pytest.raises(UnsafeResultError, match="32 KiB"):
        ensure_safe_result(result)
