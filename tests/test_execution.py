from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.broker import BrokerError, SpotQuote
from crypto_desk.config import BinanceSettings, Settings
from crypto_desk.domain import PortfolioSnapshot, SymbolRules, TradeTicket, iso
from crypto_desk.execution import (
    ExecutionService,
    confirmation_code,
    verify_confirmation_code,
)
from crypto_desk.store import Store


NOW = datetime(2026, 7, 17, 0, 15, tzinfo=UTC)
SECRET = "local-confirmation-secret"


def make_ticket(
    environment: str = "testnet",
    *,
    created_at: datetime = NOW,
) -> TradeTicket:
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
        risk_snapshot={"limiting_rule": "mainnet_cap"},
        created_at=iso(created_at),
        expires_at=iso(created_at + timedelta(minutes=30)),
    )


class FakeBroker:
    def __init__(self, environment: str):
        self.environment = environment
        self.mid = Decimal("100000")
        self.min_notional = Decimal("5")
        self.place_calls = 0
        self.reconcile_calls = 0
        self.timeout = False
        self.chain: dict | None = {"status": "FILLED", "orderListId": 123}
        self.positions: tuple[dict[str, str], ...] = ()
        self.cancel_calls = 0
        self.exit_calls = 0
        self.protection_quantities: list[Decimal] = []

    def client_order_id(self, ticket_id: str) -> str:
        prefix = "cdt" if self.environment == "testnet" else "cdm"
        return f"{prefix}-{ticket_id}"

    def symbol_rules(self, symbol: str) -> SymbolRules:
        return SymbolRules(
            symbol=symbol,
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=self.min_notional,
        )

    def latest_quote(self, symbol: str) -> SpotQuote:
        return SpotQuote(
            symbol=symbol,
            bid=self.mid - Decimal("10"),
            ask=self.mid + Decimal("10"),
            mid=self.mid,
        )

    def account_snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            environment=self.environment,
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("5000"),
            positions=self.positions,
            open_orders=(),
            as_of=iso(NOW),
        )

    def place_entry_otoco(self, ticket: TradeTicket) -> dict:
        self.place_calls += 1
        if self.timeout:
            raise TimeoutError("ambiguous transport timeout")
        return {
            "orderListId": 123,
            "listOrderStatus": "EXECUTING",
            "listClientOrderId": self.client_order_id(ticket.id),
        }

    def order_chain(self, list_client_order_id: str) -> dict:
        self.reconcile_calls += 1
        if self.chain is None:
            raise BrokerError("not found")
        return self.chain

    def cancel_order_list(self, symbol: str, list_client_order_id: str) -> dict:
        self.cancel_calls += 1
        return {"listStatusType": "ALL_DONE"}

    def place_exit_fok(self, ticket: TradeTicket) -> dict:
        self.exit_calls += 1
        remaining = Decimal(self.positions[0]["free"]) - ticket.quantity
        self.positions = (
            {
                **self.positions[0],
                "free": str(remaining),
                "total": str(remaining),
                "value_usdt": str(remaining * self.mid),
            },
        )
        return {"status": "FILLED", "orderId": 456}

    def place_protection_oco(
        self,
        ticket: TradeTicket,
        quantity: Decimal,
    ) -> dict:
        self.protection_quantities.append(quantity)
        return {"listStatusType": "EXEC_STARTED", "orderListId": 789}


def make_service(
    tmp_path: Path,
    *,
    environment: str = "testnet",
    ticket_environment: str | None = None,
    live_enabled: bool = False,
    testnet_enabled: bool = False,
    broker: FakeBroker | None = None,
    now: datetime = NOW,
    ticket: TradeTicket | None = None,
) -> tuple[ExecutionService, Store, FakeBroker]:
    selected_broker = broker or FakeBroker(environment)
    settings = Settings(
        symbols=("BTCUSDT",),
        binance=BinanceSettings(environment=environment),
        telegram_allowlist=("owner",),
    )
    store = Store(tmp_path / "crypto.sqlite3")
    store.save_ticket(ticket or make_ticket(ticket_environment or environment))
    service = ExecutionService(
        store,
        selected_broker,
        settings,
        now=lambda: now,
        live_enabled=live_enabled,
        testnet_enabled=testnet_enabled,
        confirmation_secret=SECRET,
    )
    return service, store, selected_broker


@pytest.mark.parametrize(
    ("environment", "live_enabled", "code", "reason"),
    [
        ("testnet", True, "123456", "environment"),
        ("mainnet", False, "123456", "disabled"),
        ("mainnet", True, "000000", "confirmation"),
    ],
)
def test_mainnet_requires_all_three_gates(
    tmp_path,
    environment,
    live_enabled,
    code,
    reason,
):
    service, _, _ = make_service(
        tmp_path,
        environment=environment,
        ticket_environment="mainnet",
        live_enabled=live_enabled,
    )

    with pytest.raises(ValueError, match=reason):
        service.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
            code=code,
        )


def test_confirmation_code_is_ticket_bound_and_five_minute_scoped():
    first = confirmation_code(SECRET, "ticket-1", NOW)

    assert len(first) == 6
    assert first != confirmation_code(SECRET, "ticket-2", NOW)
    assert first != confirmation_code(
        SECRET,
        "ticket-1",
        NOW + timedelta(minutes=5),
    )
    assert verify_confirmation_code(SECRET, "ticket-1", first, NOW)
    assert not verify_confirmation_code(
        SECRET,
        "ticket-1",
        first,
        NOW + timedelta(minutes=5),
    )


def test_valid_mainnet_telegram_approval_submits_once(tmp_path):
    service, store, broker = make_service(
        tmp_path,
        environment="mainnet",
        live_enabled=True,
    )
    code = confirmation_code(SECRET, "ticket-1", NOW)

    result = service.approve(
        "ticket-1",
        actor="owner",
        channel="telegram",
        code=code,
    )

    assert result.status == "SUBMITTED"
    assert broker.place_calls == 1
    assert store.submission("ticket-1")["status"] == "SUBMITTED"

    with pytest.raises(ValueError, match="PENDING|submitted"):
        service.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
            code=code,
        )
    assert broker.place_calls == 1


def test_mainnet_initial_cap_blocks_ticket_over_twenty_five_usdt(tmp_path):
    ticket = replace(
        make_ticket("mainnet"),
        quantity=Decimal("0.00026"),
        notional_usdt=Decimal("26.0000000"),
    )
    service, _, broker = make_service(
        tmp_path,
        environment="mainnet",
        live_enabled=True,
        ticket=ticket,
    )

    with pytest.raises(ValueError, match="risk room|25 USDT"):
        service.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
            code=confirmation_code(SECRET, "ticket-1", NOW),
        )

    assert broker.place_calls == 0


def test_testnet_disabled_records_dry_run_without_broker_calls(tmp_path):
    service, store, broker = make_service(
        tmp_path,
        environment="testnet",
        testnet_enabled=False,
    )

    result = service.approve(
        "ticket-1",
        actor="owner",
        channel="telegram",
    )

    assert result.status == "APPROVED_DRY_RUN"
    assert store.ticket("ticket-1").status == "APPROVED_DRY_RUN"
    assert broker.place_calls == 0


def test_wrong_actor_expired_ticket_and_mainnet_local_channel_are_blocked(
    tmp_path,
):
    service, _, _ = make_service(tmp_path / "wrong-actor")
    with pytest.raises(ValueError, match="allowlist"):
        service.approve(
            "ticket-1",
            actor="intruder",
            channel="telegram",
        )

    expired, _, _ = make_service(
        tmp_path / "expired",
        now=NOW + timedelta(minutes=31),
    )
    with pytest.raises(ValueError, match="expired"):
        expired.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
        )

    mainnet, _, _ = make_service(
        tmp_path / "local",
        environment="mainnet",
        live_enabled=True,
    )
    with pytest.raises(ValueError, match="Telegram"):
        mainnet.approve(
            "ticket-1",
            actor="owner",
            channel="local",
            code=confirmation_code(SECRET, "ticket-1", NOW),
        )


def test_price_move_over_half_percent_blocks_submission(tmp_path):
    broker = FakeBroker("testnet")
    broker.mid = Decimal("100501")
    service, _, broker = make_service(
        tmp_path,
        environment="testnet",
        testnet_enabled=True,
        broker=broker,
    )

    with pytest.raises(ValueError, match="price deviation"):
        service.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
        )

    assert broker.place_calls == 0


def test_changed_min_notional_blocks_submission(tmp_path):
    broker = FakeBroker("testnet")
    broker.min_notional = Decimal("30")
    service, _, broker = make_service(
        tmp_path,
        environment="testnet",
        testnet_enabled=True,
        broker=broker,
    )

    with pytest.raises(ValueError, match="minNotional"):
        service.approve(
            "ticket-1",
            actor="owner",
            channel="telegram",
        )

    assert broker.place_calls == 0


def test_unknown_submission_is_reconciled_without_resubmission(tmp_path):
    broker = FakeBroker("testnet")
    broker.timeout = True
    service, store, broker = make_service(
        tmp_path,
        environment="testnet",
        testnet_enabled=True,
        broker=broker,
    )

    result = service.approve(
        "ticket-1",
        actor="owner",
        channel="telegram",
    )

    assert result.status == "FILLED"
    assert broker.place_calls == 1
    assert broker.reconcile_calls == 1
    assert store.submission("ticket-1")["status"] == "FILLED"

    reconciled = service.reconcile("ticket-1")
    assert reconciled.status == "FILLED"
    assert broker.place_calls == 1


def test_unknown_submission_not_found_requires_manual_reconcile(tmp_path):
    broker = FakeBroker("testnet")
    broker.timeout = True
    broker.chain = None
    service, store, broker = make_service(
        tmp_path,
        environment="testnet",
        testnet_enabled=True,
        broker=broker,
    )

    result = service.approve(
        "ticket-1",
        actor="owner",
        channel="telegram",
    )

    assert result.status == "RECONCILE_REQUIRED"
    assert store.submission("ticket-1")["status"] == "RECONCILE_REQUIRED"
    assert broker.place_calls == 1


def test_reject_is_audited_and_never_submits(tmp_path):
    service, store, broker = make_service(tmp_path)

    result = service.reject(
        "ticket-1",
        actor="owner",
        channel="telegram",
    )

    assert result.status == "REJECTED"
    assert store.ticket("ticket-1").status == "REJECTED"
    assert store.approval("ticket-1")["decision"] == "REJECT"
    assert broker.place_calls == 0


def test_reduce_replaces_protection_after_fok_fill(tmp_path):
    broker = FakeBroker("testnet")
    broker.positions = (
        {
            "asset": "BTC",
            "symbol": "BTCUSDT",
            "free": "0.01000",
            "locked": "0",
            "total": "0.01000",
            "value_usdt": "1000.00000",
        },
    )
    ticket = TradeTicket(
        id="ticket-1",
        environment="testnet",
        symbol="BTCUSDT",
        intent="REDUCE",
        side="SELL",
        quantity=Decimal("0.00500"),
        limit_price=Decimal("100000.00"),
        stop_price=Decimal("95000.00"),
        target_price=Decimal("110000.00"),
        notional_usdt=Decimal("500.0000000"),
        risk_snapshot={"protection_list_client_order_id": "cdt-existing-protection"},
        created_at=iso(NOW),
        expires_at=iso(NOW + timedelta(minutes=30)),
    )
    service, _, broker = make_service(
        tmp_path,
        testnet_enabled=True,
        broker=broker,
        ticket=ticket,
    )

    result = service.approve(
        "ticket-1",
        actor="owner",
        channel="telegram",
    )

    assert result.status == "FILLED"
    assert broker.cancel_calls == 1
    assert broker.exit_calls == 1
    assert broker.protection_quantities == [Decimal("0.00500")]


def test_mainnet_terminal_chain_counts_only_after_reconcile(tmp_path):
    store = Store(tmp_path / "crypto.sqlite3")
    store.save_submission(
        "ticket-1",
        "mainnet",
        "cdm-ticket-1",
        {"status": "FILLED", "_reconciled": False},
    )

    assert store.completed_mainnet_chains() == 0

    store.record_order_event(
        "ticket-1",
        "FILLED",
        {"status": "FILLED", "_reconciled": True},
    )

    assert store.completed_mainnet_chains() == 1
