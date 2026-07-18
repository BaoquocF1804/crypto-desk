from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.commands import (
    SafeDailyResult,
    SafeExecuteResult,
    SafeHealthResult,
    SafeOrdersResult,
    SafePreviewResult,
    SafeReflectionsResult,
    SafeScreenResult,
    SafeSyncResult,
    SafeTicketsResult,
    ticket_fingerprint,
)
from crypto_desk.config import Settings
from crypto_desk.dispatcher import CommandDispatcher, DispatchError, execution_mode
from crypto_desk.domain import PortfolioSnapshot, TradeTicket, iso
from crypto_desk.execution import ExecutionResult
from crypto_desk.store import Store

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
OPERATOR = "quoc.lb@teko.vn"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )


def make_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        environment="testnet",
        nav_usdt=Decimal("1000"),
        free_usdt=Decimal("400"),
        positions=({"asset": "BTC", "free": "0.01"},),
        open_orders=(),
        as_of=iso(NOW),
    )


@dataclass
class FakeScreenItem:
    symbol: str = "BTCUSDT"
    passes: bool = True
    score: Decimal = Decimal("2")
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ("ev-1",)


class FakeService:
    def __init__(self):
        self.calls: list[str] = []

    def sync(self):
        self.calls.append("sync")
        return make_snapshot()

    def screen(self):
        self.calls.append("screen")
        return [FakeScreenItem()]

    def daily(self, *, due: bool = False, catch_up: bool = False):
        self.calls.append(f"daily:{due}:{catch_up}")
        return {
            "status": "COMPLETED",
            "bucket": "2026-07-17",
            "run_ids": ["run-1"],
            "screen": [
                {"symbol": "BTCUSDT", "passes": True, "score": "2", "reasons": []}
            ],
        }

    def health(self, *, due: bool = False):
        self.calls.append(f"health:{due}")
        return {
            "status": "COMPLETED",
            "bucket": "2026-07-18T09:00Z",
            "alerts": ["quote:BTCUSDT:BrokerError"],
            "run_ids": [],
            "reconciled": [{"ticket_id": "t-1", "status": "FILLED"}],
            "snapshot": None,
        }


def make_dispatcher(
    tmp_path: Path,
    *,
    service: FakeService | None = None,
) -> tuple[CommandDispatcher, Store]:
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    selected = service or FakeService()
    dispatcher = CommandDispatcher(
        settings,
        service_factory=lambda **kwargs: selected,
        execution_factory=lambda: pytest.fail("execution factory must not be built here"),
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    return dispatcher, store


def test_sync_maps_to_safe_result(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    result = dispatcher.dispatch("sync", {}, operator_email=OPERATOR)
    assert isinstance(result, SafeSyncResult)
    assert result.nav_usdt == "1000"
    assert result.positions_count == 1


def test_screen_and_daily_and_health_map_screen_items_and_counts(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    screen = dispatcher.dispatch("screen", {}, operator_email=OPERATOR)
    assert isinstance(screen, SafeScreenResult)
    assert screen.items[0].score == "2"

    daily = dispatcher.dispatch("daily", {}, operator_email=OPERATOR)
    assert isinstance(daily, SafeDailyResult)
    assert daily.run_ids == ["run-1"]

    health = dispatcher.dispatch("health", {}, operator_email=OPERATOR)
    assert isinstance(health, SafeHealthResult)
    assert health.reconciled_count == 1


def test_analyze_enforces_the_allowlist_like_the_cli(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("analyze", {"symbol": "ETHUSDT"}, operator_email=OPERATOR)
    assert excinfo.value.code == "VALIDATION_FAILED"


def test_tickets_orders_reflections_read_from_the_store(tmp_path: Path):
    dispatcher, store = make_dispatcher(tmp_path)
    store.save_submission("t-1", "testnet", "cdt-abc", {"status": "SUBMITTED"})

    tickets = dispatcher.dispatch("tickets", {}, operator_email=OPERATOR)
    assert isinstance(tickets, SafeTicketsResult)

    orders = dispatcher.dispatch("orders", {}, operator_email=OPERATOR)
    assert isinstance(orders, SafeOrdersResult)
    assert orders.orders[0].ticket_id == "t-1"
    dumped = orders.model_dump()
    assert "client_order_id" not in str(dumped)

    reflections = dispatcher.dispatch("reflections", {}, operator_email=OPERATOR)
    assert isinstance(reflections, SafeReflectionsResult)
    assert reflections.reflections == []


def test_doctor_is_offline_and_curated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "dashboard-key-sentinel")
    monkeypatch.setenv(
        "BINANCE_TESTNET_API_SECRET",
        "dashboard-secret-sentinel",
    )
    dispatcher, _ = make_dispatcher(tmp_path)
    result = dispatcher.dispatch("doctor", {}, operator_email=OPERATOR)
    assert result.execution_mode == "DRY_RUN"
    assert result.binance_keys_present is True
    assert result.database_schema_version == 3
    dumped = result.model_dump()
    assert "dashboard-key-sentinel" not in str(dumped)
    assert "dashboard-secret-sentinel" not in str(dumped)


def test_non_testnet_command_envelope_is_forbidden(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("sync", {}, operator_email=OPERATOR, environment="mainnet")
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"


def test_unknown_kind_or_bad_args_raise_validation_error(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("shutdown", {}, operator_email=OPERATOR)
    assert excinfo.value.code == "INVALID_COMMAND"
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("sync", {"extra": 1}, operator_email=OPERATOR)
    assert excinfo.value.code == "INVALID_COMMAND"


def test_execution_mode_follows_the_env_flag(monkeypatch):
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "1")
    assert execution_mode() == "TESTNET_ORDER"
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    assert execution_mode() == "DRY_RUN"


def make_ticket(environment: str = "testnet") -> TradeTicket:
    return TradeTicket(
        id="ticket-1",
        environment=environment,
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


class FakeExecution:
    def __init__(self):
        self.approve_kwargs = None
        self.reject_kwargs = None
        self.reconcile_ids: list[str] = []

    def approve(self, ticket_id: str, **kwargs):
        self.approve_kwargs = kwargs
        return ExecutionResult(
            ticket_id,
            "APPROVED_DRY_RUN",
            {"status": "APPROVED_DRY_RUN"},
        )

    def reject(self, ticket_id: str, **kwargs):
        self.reject_kwargs = kwargs
        return ExecutionResult(
            ticket_id,
            "REJECTED",
            {"status": "REJECTED"},
        )

    def reconcile(self, ticket_id: str):
        self.reconcile_ids.append(ticket_id)
        return ExecutionResult(
            ticket_id,
            "FILLED",
            {"status": "FILLED"},
        )


def make_execution_dispatcher(
    tmp_path: Path,
    monkeypatch,
    *,
    environment: str = "testnet",
) -> tuple[CommandDispatcher, Store, FakeExecution]:
    monkeypatch.setenv("BINANCE_ENV", environment)
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.save_ticket(make_ticket())
    execution = FakeExecution()
    dispatcher = CommandDispatcher(
        settings,
        service_factory=lambda **kwargs: pytest.fail(
            "service must not be built"
        ),
        execution_factory=lambda: execution,
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    return dispatcher, store, execution


def test_preview_returns_sanitized_ticket_with_fingerprint(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, _ = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    result = dispatcher.dispatch(
        "preview",
        {"action": "approve", "ticket_id": "ticket-1"},
        operator_email=OPERATOR,
    )
    assert isinstance(result, SafePreviewResult)
    assert result.execution_mode == "DRY_RUN"
    assert result.fingerprint == ticket_fingerprint(
        store.ticket("ticket-1")
    )
    assert result.entry == "100000.00"
    assert result.submission_status is None


def test_preview_unknown_ticket_fails_with_safe_code(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, _, _ = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "preview",
            {"action": "approve", "ticket_id": "missing"},
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "TICKET_NOT_FOUND"


def test_execute_revalidates_fingerprint_before_calling_execution(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, execution = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))
    result = dispatcher.dispatch(
        "execute",
        {
            "action": "approve",
            "ticket_id": "ticket-1",
            "preview_command_id": "cmd-preview",
            "fingerprint": fingerprint,
        },
        operator_email=OPERATOR,
    )
    assert isinstance(result, SafeExecuteResult)
    assert result.status == "APPROVED_DRY_RUN"
    assert execution.approve_kwargs == {
        "actor": OPERATOR,
        "channel": "local",
    }


def test_execute_with_stale_fingerprint_fails_closed_as_ticket_changed(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, execution = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    stale = ticket_fingerprint(store.ticket("ticket-1"))
    store.record_approval(
        "ticket-1",
        actor="owner",
        channel="local",
        status="APPROVED",
    )
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": stale,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "TICKET_CHANGED"
    assert execution.approve_kwargs is None


@pytest.mark.parametrize("action", ["reject", "reconcile"])
def test_execute_maps_reject_and_reconcile(
    action,
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, execution = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))
    result = dispatcher.dispatch(
        "execute",
        {
            "action": action,
            "ticket_id": "ticket-1",
            "preview_command_id": "cmd-preview",
            "fingerprint": fingerprint,
        },
        operator_email=OPERATOR,
    )
    assert isinstance(result, SafeExecuteResult)
    if action == "reject":
        assert execution.reject_kwargs == {
            "actor": OPERATOR,
            "channel": "local",
        }
    else:
        assert execution.reconcile_ids == ["ticket-1"]


def test_mainnet_is_hard_locked_at_every_layer(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, execution = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
        environment="mainnet",
    )
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": fingerprint,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"
    assert execution.approve_kwargs is None


def test_mainnet_ticket_is_forbidden_even_on_testnet_gate(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.save_ticket(make_ticket(environment="mainnet"))
    dispatcher = CommandDispatcher(
        settings,
        execution_factory=lambda: pytest.fail("must not execute"),
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "preview",
            {"action": "approve", "ticket_id": "ticket-1"},
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"


def test_execution_validation_error_becomes_safe_code(
    tmp_path: Path,
    monkeypatch,
):
    dispatcher, store, execution = make_execution_dispatcher(
        tmp_path,
        monkeypatch,
    )
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))

    def raising_approve(ticket_id: str, **kwargs):
        raise ValueError("Ticket ticket-1 is expired")

    execution.approve = raising_approve
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": fingerprint,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "VALIDATION_FAILED"
