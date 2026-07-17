from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.domain import PortfolioSnapshot, ResearchDecision, TradeTicket, iso
from crypto_desk.store import Store


def make_ticket(environment: str = "testnet") -> TradeTicket:
    return TradeTicket.create(
        environment=environment,
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000"),
        stop_price=Decimal("90000"),
        target_price=Decimal("120000"),
        notional_usdt=Decimal("25"),
        risk_snapshot={"risk_budget": Decimal("50")},
        ttl_minutes=30,
    )


def test_store_uses_schema_version_one(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.schema_version() == 1


def test_snapshot_round_trip_preserves_decimal_values(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    snapshot = PortfolioSnapshot(
        environment="testnet",
        nav_usdt=Decimal("10000.12345678"),
        free_usdt=Decimal("2500.00000001"),
        positions=({"symbol": "BTCUSDT", "quantity": "0.01230000"},),
        open_orders=(),
        as_of=iso(),
    )

    store.save_snapshot(snapshot)
    loaded = store.latest_snapshot("testnet")

    assert loaded == snapshot
    assert isinstance(loaded.nav_usdt, Decimal)


def test_ticket_round_trip_and_approval_are_auditable(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    ticket = make_ticket()

    store.save_ticket(ticket)
    store.approve_ticket(ticket.id, actor="owner", channel="telegram")

    loaded = store.ticket(ticket.id)
    approval = store.approval(ticket.id)
    assert loaded.quantity == Decimal("0.00025")
    assert loaded.risk_snapshot["risk_budget"] == "50"
    assert approval["actor"] == "owner"
    assert approval["channel"] == "telegram"


def test_research_run_round_trip_preserves_decision_decimals(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    decision = ResearchDecision(
        symbol="BTCUSDT",
        action="ACCUMULATE",
        conviction=Decimal("7.5"),
        bull_case="Trend is constructive.",
        bear_case="Funding is elevated.",
        catalysts=("ETF flow",),
        invalidation="Daily close below support.",
        entry=Decimal("100000"),
        stop=Decimal("90000"),
        target=Decimal("120000"),
        evidence_ids=("evidence-1",),
        reason="Reward exceeds risk.",
    )

    store.save_run("run-1", iso(), decision, Path("artifacts/run-1"))
    loaded = store.latest_run("BTCUSDT")

    assert loaded is not None
    assert loaded["decision"]["entry"] == "100000"
    assert loaded["decision"]["conviction"] == "7.5"


def test_client_order_id_is_unique_per_environment(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})

    with pytest.raises(ValueError, match="already submitted"):
        store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})

    store.save_submission("ticket-2", "mainnet", "desk_ticket_1", {"status": "NEW"})


def test_mainnet_count_requires_terminal_reconcile(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "mainnet", "desk_ticket_1", {"status": "NEW"})
    store.save_submission("ticket-2", "testnet", "desk_ticket_2", {"status": "NEW"})

    assert store.completed_mainnet_chains() == 0

    store.record_order_event("ticket-1", "FILLED", {"executedQty": "0.00025"})
    store.record_order_event("ticket-2", "FILLED", {"executedQty": "0.00025"})

    assert store.completed_mainnet_chains() == 1


def test_schedule_bucket_and_reflection_are_idempotent(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert not store.scheduled_done("health", "2026-07-17T00:15Z")
    assert store.mark_scheduled("health", "2026-07-17T00:15Z")
    assert not store.mark_scheduled("health", "2026-07-17T00:15Z")
    assert store.scheduled_done("health", "2026-07-17T00:15Z")

    assert store.save_reflection("run-1", "BTCUSDT", {"return": Decimal("0.05")})
    assert not store.save_reflection("run-1", "BTCUSDT", {"return": Decimal("0.10")})
