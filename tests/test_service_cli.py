from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from crypto_desk.broker import SpotQuote
from crypto_desk.cli import _hermes_installed, app
from crypto_desk.config import Settings
from crypto_desk.data import EvidenceError, EvidenceSnapshot
from crypto_desk.domain import (
    EvidenceItem,
    PortfolioSnapshot,
    ResearchDecision,
    SymbolRules,
)
from crypto_desk.service import (
    AnalysisRun,
    CryptoDeskService,
    calculate_reflection,
)
from crypto_desk.store import Store


NOW = datetime(2026, 7, 17, 0, 15, tzinfo=UTC)


def make_snapshot(symbol: str = "BTCUSDT") -> EvidenceSnapshot:
    items = tuple(
        EvidenceItem.create(
            kind=kind,
            provider="fixture",
            source=f"fixture://{kind}",
            fetched_at=NOW.isoformat(),
            as_of=NOW.isoformat(),
            delayed=False,
            stale=False,
            payload={"symbol": symbol, "value": value},
        )
        for kind, value in (
            ("spot", "100"),
            ("news", "current"),
            ("derivatives", "0.0001"),
            ("reference", "100"),
        )
    )
    closes = tuple(Decimal(index) for index in range(1, 121))
    return EvidenceSnapshot(
        symbol=symbol,
        cutoff=NOW.isoformat(),
        rules=SymbolRules(
            symbol=symbol,
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
        ),
        items=items,
        binance_mid=Decimal("100"),
        reference_usdt=Decimal("100"),
        spread=Decimal("0.001"),
        quote_volume=Decimal("500000000"),
        daily_closes=closes,
        four_hour_closes=closes,
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("120000"),
        news_count=1,
    )


class FakeBuilder:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls: list[str] = []

    def build(self, symbol: str, cutoff: datetime) -> EvidenceSnapshot:
        self.calls.append(symbol)
        if self.error:
            raise EvidenceError(self.error)
        return make_snapshot(symbol)


class FakeCommittee:
    def __init__(self):
        self.calls = 0

    def run(
        self,
        snapshot: EvidenceSnapshot,
        reflections=(),
        *,
        position_quantity=Decimal("0"),
    ):
        self.calls += 1
        decision = ResearchDecision(
            symbol=snapshot.symbol,
            action="ACCUMULATE",
            conviction=Decimal("7"),
            bull_case="Xu hướng tăng.",
            bear_case="Biến động cao.",
            catalysts=("Dòng tiền Spot",),
            invalidation="Đóng cửa dưới stop.",
            entry=Decimal("100.00"),
            stop=Decimal("90.00"),
            target=Decimal("120.00"),
            evidence_ids=snapshot.evidence_ids,
            reason="committee decision",
        )
        return type(
            "CommitteeResult",
            (),
            {"decision": decision, "reports": {}},
        )()


class FakeBroker:
    environment = "testnet"

    def __init__(self, *, unhealthy: bool = False):
        self.unhealthy = unhealthy
        self.mid = Decimal("100")
        self.open_orders: tuple[dict, ...] = ()

    def account_snapshot(self) -> PortfolioSnapshot:
        positions = (
            (
                {
                    "asset": "BTC",
                    "symbol": "BTCUSDT",
                    "free": "85",
                    "locked": "0",
                    "total": "85",
                    "mid_usdt": "100",
                    "value_usdt": "8500",
                },
            )
            if self.unhealthy
            else ()
        )
        return PortfolioSnapshot(
            environment="testnet",
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("1500" if self.unhealthy else "10000"),
            positions=positions,
            open_orders=self.open_orders,
            as_of=NOW.isoformat(),
        )

    def latest_quote(self, symbol: str) -> SpotQuote:
        return SpotQuote(
            symbol=symbol,
            bid=self.mid - Decimal("0.01"),
            ask=self.mid + Decimal("0.01"),
            mid=self.mid,
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )


def test_public_commands_exist():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "doctor",
        "sync",
        "screen",
        "analyze",
        "daily",
        "health",
        "tickets",
        "approve",
        "reject",
        "orders",
        "live-code",
        "reflections",
    ):
        assert command in result.stdout


def test_orders_reconcile_queries_only_nonterminal_submissions(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"database: {tmp_path / 'crypto.sqlite3'}\nsymbols: [BTCUSDT]\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "crypto.sqlite3")
    store.save_submission(
        "open-ticket",
        "testnet",
        "cdt-open-ticket",
        {"status": "RECONCILE_REQUIRED"},
    )
    store.save_submission(
        "done-ticket",
        "testnet",
        "cdt-done-ticket",
        {"status": "FILLED", "_reconciled": True},
    )
    store.close()

    class FakeExecution:
        def __init__(self):
            self.calls: list[str] = []

        def reconcile(self, ticket_id: str):
            self.calls.append(ticket_id)
            return {"ticket_id": ticket_id, "status": "SUBMITTED"}

    execution = FakeExecution()
    monkeypatch.setattr(
        "crypto_desk.cli._execution_service",
        lambda settings: execution,
    )

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "--json",
            "orders",
            "--reconcile",
        ],
    )

    assert result.exit_code == 0
    assert execution.calls == ["open-ticket"]
    assert json.loads(result.stdout)["reconciled"] == [
        {"status": "SUBMITTED", "ticket_id": "open-ticket"}
    ]


def test_analyze_rejects_symbol_outside_allowlist(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("symbols: [BTCUSDT]\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["--config", str(config), "analyze", "DOGEUSDT"],
    )

    assert result.exit_code != 0
    assert "allowlist" in result.output


def test_analyze_writes_complete_artifact_set_and_store_record(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    committee = FakeCommittee()
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=committee,
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert isinstance(result, AnalysisRun)
    assert result.decision.action == "ACCUMULATE"
    assert committee.calls == 1
    for name in (
        "evidence.json",
        "analysts.json",
        "decision.json",
        "report.md",
    ):
        assert (result.report_dir / name).is_file()
    decision = json.loads((result.report_dir / "decision.json").read_text(encoding="utf-8"))
    assert decision["entry"] == "100.00"
    assert store.latest_run("BTCUSDT")["id"] == result.run_id


def test_evidence_error_forces_no_trade_without_calling_committee(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    committee = FakeCommittee()
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder("stale price"),
        committee=committee,
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.decision.action == "NO_TRADE"
    assert "stale price" in result.decision.reason
    assert committee.calls == 0
    assert "NO_TRADE" in (result.report_dir / "report.md").read_text(encoding="utf-8")


def test_committee_transport_failure_forces_no_trade_without_secret_leak(
    tmp_path,
):
    class ErrorCommittee:
        def run(self, *args, **kwargs):
            raise RuntimeError("provider detail that must not be serialized")

    settings = make_settings(tmp_path)
    service = CryptoDeskService(
        settings,
        Store(settings.database),
        evidence_builder=FakeBuilder(),
        committee=ErrorCommittee(),
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.decision.action == "NO_TRADE"
    assert result.decision.reason == "committee:RuntimeError"
    assert "provider detail" not in (result.report_dir / "report.md").read_text(encoding="utf-8")


def test_daily_and_health_buckets_are_idempotent(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    builder = FakeBuilder()
    committee = FakeCommittee()
    service = CryptoDeskService(
        settings,
        store,
        broker=FakeBroker(unhealthy=True),
        evidence_builder=builder,
        committee=committee,
        now=lambda: NOW,
    )

    first_daily = service.daily(due=True)
    second_daily = service.daily(due=True)
    first_health = service.health(due=True)
    second_health = service.health(due=True)

    assert first_daily["status"] == "COMPLETED"
    assert second_daily["status"] == "ALREADY_DONE"
    assert first_health["status"] == "COMPLETED"
    assert second_health["status"] == "ALREADY_DONE"
    assert "max_gross" in first_health["alerts"]
    assert "usdt_reserve" in first_health["alerts"]


def test_daily_catch_up_uses_most_recent_missing_utc_day(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.mark_scheduled("daily", "2026-07-17")
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.daily(catch_up=True)

    assert result["status"] == "COMPLETED"
    assert result["bucket"] == "2026-07-16"


def test_health_detects_stop_breach_even_when_protection_exists(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    broker = FakeBroker(unhealthy=True)
    broker.mid = Decimal("89")
    broker.open_orders = (
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_LOSS",
            "stopPrice": "90",
        },
    )
    service = CryptoDeskService(
        settings,
        store,
        broker=broker,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health(due=True)

    assert "stop_breach:BTCUSDT" in result["alerts"]
    assert "missing_protection:BTCUSDT" not in result["alerts"]


def test_health_detects_stale_core_evidence(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    report_dir = tmp_path / "prior-run"
    report_dir.mkdir()
    stale_time = datetime(2026, 7, 15, 0, 0, tzinfo=UTC).isoformat()
    (report_dir / "evidence.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "spot",
                        "as_of": stale_time,
                        "stale": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store.save_run(
        "prior-run",
        NOW.isoformat(),
        ResearchDecision(
            symbol="BTCUSDT",
            action="HOLD",
            conviction=Decimal("5"),
            bull_case="Bull",
            bear_case="Bear",
            catalysts=(),
            invalidation="Invalidation",
            entry=None,
            stop=None,
            target=None,
            evidence_ids=("evidence-1",),
            reason="prior",
        ),
        report_dir,
    )
    broker = FakeBroker(unhealthy=True)
    broker.open_orders = (
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_LOSS",
            "stopPrice": "90",
        },
    )
    service = CryptoDeskService(
        settings,
        store,
        broker=broker,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health(due=True)

    assert "stale_evidence:BTCUSDT" in result["alerts"]


def test_health_reports_broker_failure_without_leaking_exception_text(tmp_path):
    class ErrorBroker:
        def account_snapshot(self):
            raise RuntimeError("sensitive upstream detail")

    settings = make_settings(tmp_path)
    service = CryptoDeskService(
        settings,
        Store(settings.database),
        broker=ErrorBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health(due=True)

    assert result["status"] == "COMPLETED"
    assert result["alerts"] == ["broker_sync:RuntimeError"]
    assert "sensitive upstream detail" not in json.dumps(result)


def test_reflection_calculates_return_excursions_and_benchmark_alpha():
    result = calculate_reflection(
        entry=Decimal("100"),
        closes=(
            Decimal("100"),
            Decimal("90"),
            Decimal("120"),
            Decimal("110"),
        ),
        benchmark_closes=(Decimal("100"), Decimal("105")),
    )

    assert result["realized_return"] == Decimal("0.10")
    assert result["maximum_adverse_excursion"] == Decimal("-0.10")
    assert result["maximum_favorable_excursion"] == Decimal("0.20")
    assert result["benchmark_return"] == Decimal("0.05")
    assert result["alpha"] == Decimal("0.05")


def test_json_doctor_reports_secret_presence_without_values(
    tmp_path,
    monkeypatch,
):
    config = tmp_path / "config.yaml"
    config.write_text("symbols: [BTCUSDT]\n", encoding="utf-8")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "never-print-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "never-print-secret")

    result = CliRunner().invoke(
        app,
        ["--config", str(config), "--json", "doctor"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["binance"]["credentials_present"] is True
    assert "never-print-key" not in result.stdout
    assert "never-print-secret" not in result.stdout


def test_doctor_finds_hermes_in_user_local_bin_when_path_is_minimal(
    tmp_path: Path,
    monkeypatch,
):
    binary = tmp_path / ".local" / "bin" / "hermes"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("crypto_desk.cli.shutil.which", lambda command: None)

    assert _hermes_installed(home=tmp_path) is True
