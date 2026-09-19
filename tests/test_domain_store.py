import sqlite3
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


def test_store_uses_schema_version_three(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.schema_version() == 3


def test_journal_lifecycle_first_run_finish_report(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.journal_entry("cmd-1") is None
    store.journal_start("cmd-1", "hash-1", "sync")
    entry = store.journal_entry("cmd-1")
    assert entry["state"] == "RUNNING"
    assert entry["reported"] is False

    store.journal_finish(
        "cmd-1",
        state="SUCCEEDED",
        result={"nav_usdt": "1"},
    )
    assert store.journal_running() == []
    unreported = store.journal_unreported()
    assert [item["command_id"] for item in unreported] == ["cmd-1"]
    assert unreported[0]["result"] == {"nav_usdt": "1"}

    store.journal_mark_reported("cmd-1")
    assert store.journal_unreported() == []


def test_journal_duplicate_start_raises(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.journal_start("cmd-1", "hash-1", "sync")

    with pytest.raises(ValueError, match="cmd-1"):
        store.journal_start("cmd-1", "hash-2", "sync")


def test_journal_failed_state_records_error_code(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.journal_start("cmd-2", "hash-2", "execute")
    store.journal_finish(
        "cmd-2",
        state="NEEDS_REVIEW",
        error_code="EXECUTION_UNCERTAIN",
    )

    entry = store.journal_entry("cmd-2")
    assert entry["state"] == "NEEDS_REVIEW"
    assert entry["error_code"] == "EXECUTION_UNCERTAIN"
    assert entry["result"] is None


def test_existing_v2_database_migrates_to_v3(tmp_path: Path):
    path = tmp_path / "crypto.db"
    first = Store(path)
    first.close()
    second = sqlite3.connect(path)
    second.execute("DROP TABLE command_journal")
    second.execute("UPDATE schema_meta SET version = 2")
    second.commit()
    second.close()

    migrated = Store(path)
    assert migrated.schema_version() == 3
    migrated.journal_start("cmd-1", "hash-1", "sync")


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
    assert approval["decision"] == "APPROVE"


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


def test_unreflected_runs_returns_only_due_rows(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    decision = make_decision(evidence_ids=("evidence-1",))
    store.save_run("due", "2026-06-01T00:15:00+00:00", decision, Path("artifacts/due"))
    store.save_run("future", "2026-07-01T00:15:00+00:00", decision, Path("artifacts/future"))
    store.save_reflection("done", "BTCUSDT", {"realized_return": Decimal("0.05")})
    store.save_run("done", "2026-05-01T00:15:00+00:00", decision, Path("artifacts/done"))

    rows = store.unreflected_runs("2026-06-30T00:15:00+00:00")

    assert [row["id"] for row in rows] == ["due"]


def make_decision(*, evidence_ids: tuple[str, ...] = (), action: str = "HOLD") -> ResearchDecision:
    return ResearchDecision(
        symbol="BTCUSDT",
        action=action,
        conviction=Decimal("7.5"),
        bull_case="Trend is constructive.",
        bear_case="Funding is elevated.",
        catalysts=("ETF flow",),
        invalidation="Daily close below support.",
        entry=Decimal("100000"),
        stop=Decimal("90000"),
        target=Decimal("120000"),
        evidence_ids=evidence_ids,
        reason="committee decision" if evidence_ids else "binance evidence is from the future",
    )


def test_latest_valid_run_falls_back_past_a_blocked_attempt(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    valid = make_decision(evidence_ids=("evidence-1",))
    blocked = make_decision(evidence_ids=())

    store.save_run("run-1", "2026-07-17T00:00:00+00:00", valid, Path("artifacts/run-1"))
    store.save_run("run-2", "2026-07-18T00:00:00+00:00", blocked, Path("artifacts/run-2"))

    latest_attempt = store.latest_run("BTCUSDT")
    assert latest_attempt is not None
    assert latest_attempt["id"] == "run-2"
    assert latest_attempt["decision"]["evidence_ids"] == []

    latest_valid = store.latest_valid_run("BTCUSDT")
    assert latest_valid is not None
    assert latest_valid["id"] == "run-1"
    assert latest_valid["decision"]["evidence_ids"] == ["evidence-1"]


def test_latest_valid_run_accepts_evidence_backed_no_trade(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    no_trade_with_evidence = make_decision(evidence_ids=("evidence-1",), action="NO_TRADE")

    store.save_run(
        "run-1", "2026-07-18T00:00:00+00:00", no_trade_with_evidence, Path("artifacts/run-1")
    )

    latest_valid = store.latest_valid_run("BTCUSDT")
    assert latest_valid is not None
    assert latest_valid["decision"]["action"] == "NO_TRADE"
    assert latest_valid["decision"]["evidence_ids"] == ["evidence-1"]


def test_latest_valid_run_returns_none_without_any_valid_attempt(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    blocked = make_decision(evidence_ids=())

    store.save_run("run-1", "2026-07-18T00:00:00+00:00", blocked, Path("artifacts/run-1"))

    assert store.latest_valid_run("BTCUSDT") is None
    assert store.latest_valid_run("ETHUSDT") is None


def test_latest_scheduled_run_orders_by_completed_at_not_rowid(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    # Insert the newer completed_at first so a rowid-based ORDER BY would pick the wrong row.
    store.db.execute(
        "INSERT INTO scheduled_runs(kind,bucket,completed_at) VALUES (?,?,?)",
        ("health", "2026-07-18T00:15Z", "2026-07-18T00:20:00+00:00"),
    )
    store.db.execute(
        "INSERT INTO scheduled_runs(kind,bucket,completed_at) VALUES (?,?,?)",
        ("health", "2026-07-17T00:15Z", "2026-07-17T00:20:00+00:00"),
    )
    store.db.commit()

    latest = store.latest_scheduled_run("health")

    assert latest is not None
    assert latest["bucket"] == "2026-07-18T00:15Z"
    assert latest["completed_at"] == "2026-07-18T00:20:00+00:00"


def test_latest_scheduled_run_returns_none_when_never_run(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.latest_scheduled_run("health") is None


def test_recent_order_events_omit_raw_payload(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})

    store.record_order_event("ticket-1", "FILLED", {"secret": "nope", "executedQty": "0.00025"})

    events = store.recent_order_events(limit=5, environment="testnet")

    assert len(events) == 1
    assert events[0]["ticket_id"] == "ticket-1"
    assert events[0]["status"] == "FILLED"
    assert "event_time" in events[0]
    assert set(events[0]) == {"ticket_id", "status", "event_time"}


def test_recent_order_events_respects_limit_and_recency(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})
    store.record_order_event("ticket-1", "NEW", {"executedQty": "0"})
    store.record_order_event("ticket-1", "PARTIALLY_FILLED", {"executedQty": "0.0001"})
    store.record_order_event("ticket-1", "FILLED", {"executedQty": "0.00025"})

    events = store.recent_order_events(limit=2, environment="testnet")

    assert len(events) == 2
    assert events[0]["status"] == "FILLED"
    assert events[1]["status"] == "PARTIALLY_FILLED"


def test_recent_order_events_are_scoped_to_environment(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-testnet", "testnet", "desk_ticket_testnet", {"status": "NEW"})
    store.save_submission("ticket-mainnet", "mainnet", "desk_ticket_mainnet", {"status": "NEW"})

    store.record_order_event("ticket-testnet", "FILLED", {"executedQty": "0.00025"})
    store.record_order_event("ticket-mainnet", "FILLED", {"executedQty": "0.00025"})

    testnet_events = store.recent_order_events(limit=5, environment="testnet")
    mainnet_events = store.recent_order_events(limit=5, environment="mainnet")

    assert [event["ticket_id"] for event in testnet_events] == ["ticket-testnet"]
    assert [event["ticket_id"] for event in mainnet_events] == ["ticket-mainnet"]


def test_unreflected_runs_filters_by_symbol(tmp_path: Path):
    from crypto_desk.store import Store

    store = Store(tmp_path / "t.sqlite3")
    try:
        for run_id, symbol in (("r-btc", "BTCUSDT"), ("r-fpt", "FPT")):
            store.db.execute(
                "INSERT INTO research_runs(id,symbol,cutoff,decision,report_dir)"
                " VALUES (?,?,?,?,?)",
                (run_id, symbol, "2026-01-01T00:00:00+00:00", "{}", "/tmp"),
            )
        store.db.commit()

        both = store.unreflected_runs("2026-02-01T00:00:00+00:00")
        crypto = store.unreflected_runs("2026-02-01T00:00:00+00:00", ("BTCUSDT",))
        vn = store.unreflected_runs("2026-02-01T00:00:00+00:00", ("FPT",))
    finally:
        store.close()

    assert {r["symbol"] for r in both} == {"BTCUSDT", "FPT"}
    assert [r["symbol"] for r in crypto] == ["BTCUSDT"]
    assert [r["symbol"] for r in vn] == ["FPT"]


def test_unreflected_runs_with_empty_symbol_tuple_returns_nothing(tmp_path: Path):
    from crypto_desk.store import Store

    store = Store(tmp_path / "t.sqlite3")
    try:
        store.db.execute(
            "INSERT INTO research_runs(id,symbol,cutoff,decision,report_dir)"
            " VALUES (?,?,?,?,?)",
            ("r-btc", "BTCUSDT", "2026-01-01T00:00:00+00:00", "{}", "/tmp"),
        )
        store.db.commit()
        rows = store.unreflected_runs("2026-02-01T00:00:00+00:00", ())
    finally:
        store.close()

    assert rows == []
