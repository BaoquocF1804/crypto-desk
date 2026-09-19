from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import crypto_desk.cli as cli
import httpx
from typer.testing import CliRunner

from crypto_desk.broker import SpotQuote
from crypto_desk.cli import _hermes_installed, _structured_client, app, doctor_report
from crypto_desk.committee import GeminiStructuredClient, OpenAIStructuredClient
from crypto_desk.config import ModelSettings, Settings
from crypto_desk.data import EvidenceError, EvidenceSnapshot
from crypto_desk.domain import (
    EvidenceItem,
    FuturesTradeSetup,
    PortfolioSnapshot,
    ResearchDecision,
    SymbolRules,
)
from crypto_desk.service import (
    AnalysisRun,
    CryptoDeskService,
    calculate_reflection,
)
from crypto_desk.execution import telegram_approval_proof
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

    def build(
        self,
        symbol: str,
        cutoff: datetime,
        *,
        live: bool = False,
    ) -> EvidenceSnapshot:
        self.calls.append(symbol)
        if self.error:
            raise EvidenceError(self.error)
        return make_snapshot(symbol)

    def reflection_closes(
        self,
        symbol: str,
        start: datetime,
        periods: int = 20,
    ) -> tuple[Decimal, ...]:
        del start
        base = Decimal("100") if symbol == "BTCUSDT" else Decimal("50")
        return tuple(base + index for index in range(periods))


class FakeCommittee:
    def __init__(self):
        self.calls = 0
        self.last_prior_thesis = None

    def run(
        self,
        snapshot: EvidenceSnapshot,
        reflections=(),
        prior_thesis=None,
        *,
        position_quantity=Decimal("0"),
    ):
        self.calls += 1
        self.last_prior_thesis = prior_thesis
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
        "publish-dashboard",
        "runner",
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


def test_hermes_process_secret_creates_internal_telegram_proof(
    tmp_path: Path,
    monkeypatch,
):
    config = tmp_path / "config.yaml"
    config.write_text("symbols: [BTCUSDT]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_TELEGRAM_INGRESS_SECRET", "ingress-secret")

    class FakeExecution:
        kwargs = None

        def approve(self, ticket_id: str, **kwargs):
            self.kwargs = kwargs
            return {"ticket_id": ticket_id, "status": "APPROVED"}

    execution = FakeExecution()
    monkeypatch.setattr("crypto_desk.cli._execution_service", lambda settings: execution)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "--json",
            "approve",
            "ticket-1",
            "--actor",
            "998877",
            "--channel",
            "telegram",
            "--code",
            "654321",
        ],
    )

    assert result.exit_code == 0
    assert execution.kwargs["telegram_proof"] == telegram_approval_proof(
        "ingress-secret",
        "ticket-1",
        "998877",
        "654321",
    )


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
    store.save_snapshot(
        PortfolioSnapshot(
            environment="testnet",
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("10000"),
            positions=(),
            open_orders=(),
            as_of=NOW.isoformat(),
        )
    )
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
        "llm.json",
        "decision.json",
        "report.md",
    ):
        assert (result.report_dir / name).is_file()
    decision = json.loads((result.report_dir / "decision.json").read_text(encoding="utf-8"))
    llm = json.loads((result.report_dir / "llm.json").read_text(encoding="utf-8"))
    assert decision["entry"] == "100.00"
    assert llm["provider"] == "gemini"
    assert llm["calls"] == []
    assert store.latest_run("BTCUSDT")["id"] == result.run_id
    assert result.ticket_id is not None
    assert store.ticket(result.ticket_id).status == "PENDING"


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


def test_health_flags_unpriced_positions(tmp_path: Path):
    class UnpricedBroker(FakeBroker):
        def account_snapshot(self) -> PortfolioSnapshot:
            return PortfolioSnapshot(
                environment="testnet",
                nav_usdt=Decimal("10000"),
                free_usdt=Decimal("10000"),
                positions=(
                    {
                        "asset": "AIRDROP",
                        "symbol": "AIRDROPUSDT",
                        "free": "5",
                        "locked": "0",
                        "total": "5",
                        "mid_usdt": "0",
                        "value_usdt": "0",
                        "unpriced": True,
                        "unpriced_reason": "no_usdt_pair",
                    },
                ),
                open_orders=(),
                as_of=NOW.isoformat(),
            )

    settings = make_settings(tmp_path)
    store = Store(settings.database)
    service = CryptoDeskService(
        settings,
        store,
        broker=UnpricedBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health()

    # Unpriced dust is aggregated into one count, never one alert per asset.
    assert "unpriced_assets:1" in result["alerts"]
    assert not any(a.startswith("unpriced_asset:") for a in result["alerts"])


def test_health_aggregates_external_assets_instead_of_per_symbol_spam(tmp_path: Path):
    class DustBroker(FakeBroker):
        def account_snapshot(self) -> PortfolioSnapshot:
            external = tuple(
                {
                    "asset": f"DUST{i}",
                    "symbol": f"DUST{i}USDT",
                    "free": "10",
                    "locked": "0",
                    "total": "10",
                    "mid_usdt": "1",
                    "value_usdt": "10",
                }
                for i in range(3)
            )
            unpriced = (
                {
                    "asset": "AIRDROP",
                    "symbol": "AIRDROPUSDT",
                    "free": "5",
                    "locked": "0",
                    "total": "5",
                    "mid_usdt": "0",
                    "value_usdt": "0",
                    "unpriced": True,
                },
            )
            return PortfolioSnapshot(
                environment="testnet",
                nav_usdt=Decimal("10000"),
                free_usdt=Decimal("10000"),
                positions=external + unpriced,
                open_orders=(),
                as_of=NOW.isoformat(),
            )

    settings = make_settings(tmp_path)
    service = CryptoDeskService(
        settings,
        Store(settings.database),
        broker=DustBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health()
    alerts = result["alerts"]

    # Non-allowlist assets are counted, never expanded into per-symbol alerts.
    assert "external_unmanaged:3" in alerts
    assert "unpriced_assets:1" in alerts
    assert not any(a.startswith("missing_protection:DUST") for a in alerts)
    assert not any(a.startswith("missing_thesis:DUST") for a in alerts)
    # And no analyze follow-up is triggered for unmanaged symbols.
    assert result["run_ids"] == []


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


def test_daily_schedules_due_reflections_once(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    report_dir = tmp_path / "old-run"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(
        json.dumps({"binance_mid": "100"}),
        encoding="utf-8",
    )
    old_decision = ResearchDecision(
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
        reason="committee decision",
    )
    store.save_run(
        "old-run",
        (NOW - timedelta(days=21)).isoformat(),
        old_decision,
        report_dir,
    )
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.daily(due=True)

    assert result["reflection_run_ids"] == ["old-run"]
    reflection = store.list_reflections("BTCUSDT")[0]
    assert reflection["run_id"] == "old-run"
    assert reflection["payload"]["decision_action"] == "HOLD"
    assert reflection["payload"]["alpha"] == "0"


def test_json_doctor_reports_secret_presence_without_values(
    tmp_path,
    monkeypatch,
):
    config = tmp_path / "config.yaml"
    config.write_text("symbols: [BTCUSDT]\n", encoding="utf-8")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "never-print-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "never-print-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "never-print-gemini-key")

    result = CliRunner().invoke(
        app,
        ["--config", str(config), "--json", "doctor"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["binance"]["credentials_present"] is True
    assert payload["gemini"] == {
        "active": True,
        "key_present": True,
        "online_smoke_requested": False,
    }
    assert "never-print-key" not in result.stdout
    assert "never-print-secret" not in result.stdout
    assert "never-print-gemini-key" not in result.stdout


def test_provider_factory_selects_gemini_v1_or_openai(monkeypatch):
    captured: dict[str, object] = {}

    class FakeGeminiClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(cli.genai, "Client", FakeGeminiClient)

    gemini = _structured_client(Settings())

    assert isinstance(gemini, GeminiStructuredClient)
    assert captured == {
        "api_key": "gemini-test-key",
        "http_options": {"api_version": "v1"},
    }

    _structured_client(
        Settings(
            models=ModelSettings(
                provider="gemini",
                quick="gemini-3-flash-preview",
                deep="gemini-3-flash-preview",
            )
        )
    )
    assert captured["http_options"] == {"api_version": "v1beta"}

    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    openai = _structured_client(
        Settings(
            models=ModelSettings(
                provider="openai",
                quick="gpt-5.4-mini",
                deep="gpt-5.5",
            )
        )
    )

    assert isinstance(openai, OpenAIStructuredClient)


def test_provider_factory_selects_vertex_ai_when_credentials_provided(tmp_path: Path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeVertexClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    key_file = tmp_path / "vertex-key.json"
    key_file.write_text('{"type": "service_account"}', encoding="utf-8")

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project-123")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr(cli.genai, "Client", FakeVertexClient)

    client = _structured_client(
        Settings(
            models=ModelSettings(
                provider="vertexai",
                quick="gemini-2.5-flash",
                deep="gemini-2.5-flash",
            )
        )
    )

    assert isinstance(client, GeminiStructuredClient)
    assert captured == {
        "vertexai": True,
        "project": "test-project-123",
        "location": "us-central1",
    }


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


def test_doctor_reports_news_feed_visibility(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    report = doctor_report(settings, store, online=False)

    assert report["news"] == {"feeds_configured": 0, "analyze_possible": False}


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"database: {tmp_path / 'crypto.sqlite3'}\nsymbols: [BTCUSDT]\n",
        encoding="utf-8",
    )
    return config


def _install_publish_hook_spy(monkeypatch) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(
        "crypto_desk.cli._publish_dashboard_if_configured",
        lambda settings: calls.append(settings),
    )
    return calls


DASHBOARD_URL = "https://dashboard.example/api/ingest"


def test_publish_dashboard_command_posts_snapshot_and_emits_published_status(
    tmp_path: Path, monkeypatch
):
    config = _write_config(tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(204)

    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", DASHBOARD_URL)
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_TOKEN", "ingest-secret")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "bypass-secret")
    real_client = httpx.Client
    monkeypatch.setattr(
        "crypto_desk.dashboard.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "publish-dashboard"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "PUBLISHED"
    assert captured["url"] == DASHBOARD_URL
    assert captured["headers"]["Authorization"] == "Bearer ingest-secret"
    assert "ingest-secret" not in result.output
    assert "bypass-secret" not in result.output


def test_publish_dashboard_command_fails_clearly_when_env_var_missing(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    # Force-empty (not delenv): the command calls load_dotenv(), which would
    # otherwise repopulate these from a developer's real .env and mask the
    # missing-config path. Empty strings are treated as missing and survive
    # load_dotenv(override=False).
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", "")
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_TOKEN", "")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "")

    result = CliRunner().invoke(app, ["--config", str(config), "publish-dashboard"])

    assert result.exit_code == 2
    assert "CRYPTO_DESK_DASHBOARD_INGEST_URL" in result.output


def test_dashboard_hook_fires_after_sync(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls = _install_publish_hook_spy(monkeypatch)

    class FakeService:
        def sync(self):
            return {"nav_usdt": "1"}

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: FakeService())

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "sync"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_dashboard_hook_fires_after_analyze(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls = _install_publish_hook_spy(monkeypatch)

    class FakeService:
        def analyze(self, symbol):
            return {"symbol": symbol, "action": "HOLD"}

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: FakeService())

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "analyze", "BTCUSDT"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_dashboard_hook_fires_after_daily_only_when_completed(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)

    for status, expected_calls in (("COMPLETED", 1), ("ALREADY_DONE", 0), ("NOT_DUE", 0)):
        calls = _install_publish_hook_spy(monkeypatch)

        class FakeService:
            def daily(self, *, due=False, catch_up=False):
                return {"status": status, "bucket": "2026-07-18"}

        monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: FakeService())

        result = CliRunner().invoke(app, ["--config", str(config), "--json", "daily"])

        assert result.exit_code == 0
        assert len(calls) == expected_calls, status


def test_dashboard_hook_fires_after_health_only_when_completed(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)

    for status, expected_calls in (("COMPLETED", 1), ("ALREADY_DONE", 0)):
        calls = _install_publish_hook_spy(monkeypatch)

        class FakeService:
            def health(self, *, due=False):
                return {"status": status, "alerts": []}

        monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: FakeService())

        result = CliRunner().invoke(app, ["--config", str(config), "--json", "health"])

        assert result.exit_code == 0
        assert len(calls) == expected_calls, status


def test_dashboard_hook_fires_after_approve(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls = _install_publish_hook_spy(monkeypatch)

    class FakeExecution:
        def approve(self, ticket_id: str, **kwargs):
            return {"ticket_id": ticket_id, "status": "APPROVED"}

    monkeypatch.setattr("crypto_desk.cli._execution_service", lambda settings: FakeExecution())

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "approve", "ticket-1"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_dashboard_hook_fires_after_reject(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls = _install_publish_hook_spy(monkeypatch)

    class FakeExecution:
        def reject(self, ticket_id: str, **kwargs):
            return {"ticket_id": ticket_id, "status": "REJECTED"}

    monkeypatch.setattr("crypto_desk.cli._execution_service", lambda settings: FakeExecution())

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "reject", "ticket-1"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_dashboard_hook_fires_after_orders_reconcile_only_when_results_present(
    tmp_path: Path, monkeypatch
):
    config = _write_config(tmp_path)
    store = Store(tmp_path / "crypto.sqlite3")
    store.save_submission(
        "open-ticket", "testnet", "cdt-open-ticket", {"status": "RECONCILE_REQUIRED"}
    )
    store.close()
    calls = _install_publish_hook_spy(monkeypatch)

    class FakeExecution:
        def reconcile(self, ticket_id: str):
            return {"ticket_id": ticket_id, "status": "SUBMITTED"}

    monkeypatch.setattr("crypto_desk.cli._execution_service", lambda settings: FakeExecution())

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "orders", "--reconcile"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_dashboard_hook_does_not_fire_after_orders_reconcile_with_no_results(
    tmp_path: Path, monkeypatch
):
    config = _write_config(tmp_path)
    store = Store(tmp_path / "crypto.sqlite3")
    store.close()
    calls = _install_publish_hook_spy(monkeypatch)
    monkeypatch.delenv("BINANCE_ENV", raising=False)
    monkeypatch.setattr("crypto_desk.cli._execution_service", lambda settings: None)

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "orders", "--reconcile"])

    assert result.exit_code == 0
    assert len(calls) == 0


def test_dashboard_hook_does_not_fire_after_readonly_doctor(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls = _install_publish_hook_spy(monkeypatch)

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "doctor"])

    assert result.exit_code == 0
    assert len(calls) == 0


def test_dashboard_hook_swallows_store_open_failure(tmp_path: Path, monkeypatch):
    """A Store-open failure inside the best-effort hook must not crash a
    command that already succeeded. Exercises the real hook body (not the
    spy) so the fix covering the Store(...) construction itself is proven."""
    config = _write_config(tmp_path)
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", DASHBOARD_URL)

    class FakeService:
        def sync(self):
            return {"nav_usdt": "1"}

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: FakeService())

    def _raise_on_open(path):
        raise OSError("disk full")

    monkeypatch.setattr("crypto_desk.cli.Store", _raise_on_open)

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "sync"])

    assert result.exit_code == 0
    assert "nav_usdt" in result.output
    assert "Warning: dashboard publish failed (OSError)" in result.output


def test_runner_command_builds_and_runs_the_runner(
    tmp_path,
    monkeypatch,
):
    config = _write_config(tmp_path)
    monkeypatch.setenv(
        "CRYPTO_DESK_COMMAND_API_URL",
        "https://sites.example/api",
    )
    monkeypatch.setenv(
        "CRYPTO_DESK_RUNNER_TOKEN",
        "runner-token",
    )
    monkeypatch.setenv(
        "CRYPTO_DESK_SITES_BYPASS_TOKEN",
        "bypass-token",
    )
    monkeypatch.setenv(
        "CRYPTO_DESK_COMMAND_RUNNER_ENABLED",
        "1",
    )
    captured = {}

    class FakeRunner:
        def __init__(self, settings, **kwargs):
            captured["kwargs"] = kwargs

        def run_forever(self):
            captured["ran"] = True

        def stop(self):
            pass

    monkeypatch.setattr("crypto_desk.cli.CommandRunner", FakeRunner)
    result = CliRunner().invoke(
        app,
        ["--config", str(config), "runner"],
    )

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["kwargs"]["base_url"] == "https://sites.example/api"
    assert captured["kwargs"]["runner_token"] == "runner-token"
    assert captured["kwargs"]["sites_bypass_token"] == "bypass-token"
    assert captured["kwargs"]["enabled"] is True
    assert "runner-token" not in result.output


def test_runner_command_requires_all_env_vars(
    tmp_path,
    monkeypatch,
):
    config = _write_config(tmp_path)
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_API_URL", "")
    monkeypatch.setenv("CRYPTO_DESK_RUNNER_TOKEN", "")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "")

    result = CliRunner().invoke(
        app,
        ["--config", str(config), "runner"],
    )

    assert result.exit_code == 2
    assert "CRYPTO_DESK_COMMAND_API_URL" in result.output


def test_runner_command_allows_loopback_without_sites_bypass(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_API_URL", "http://localhost:3001/api")
    monkeypatch.setenv("CRYPTO_DESK_RUNNER_TOKEN", "runner-token")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "")
    captured: dict[str, Any] = {}

    class FakeRunner:
        def __init__(self, settings, **kwargs):
            captured.update(kwargs)

        def run_forever(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr("crypto_desk.cli.CommandRunner", FakeRunner)

    result = CliRunner().invoke(app, ["--config", str(config), "runner"])

    assert result.exit_code == 0
    assert captured["sites_bypass_token"] == ""


def test_markdown_report_includes_futures_setups_when_present(tmp_path: Path):
    store = Store(tmp_path / "crypto.sqlite3")
    settings = Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )
    service = CryptoDeskService(store=store, settings=settings)
    snapshot = make_snapshot("BTCUSDT")
    decision = ResearchDecision(
        symbol="BTCUSDT",
        action="NO_TRADE",
        conviction=Decimal("6.5"),
        bull_case="Tăng tốt.",
        bear_case="Cản mạnh.",
        catalysts=(),
        invalidation="Thủng hỗ trợ.",
        entry=None,
        stop=None,
        target=None,
        evidence_ids=("ev-1",),
        reason="Hội đồng quan sát.",
        futures_bias="BULLISH",
        futures_setups=(
            FuturesTradeSetup(
                direction="LONG",
                entry=Decimal("100000"),
                stop=Decimal("98000"),
                target=Decimal("105000"),
                risk_reward_ratio=Decimal("2.5"),
                rationale="Quét thanh lý Long xong bật tăng.",
            ),
            FuturesTradeSetup(
                direction="SHORT",
                entry=Decimal("105000"),
                stop=Decimal("107000"),
                target=Decimal("100000"),
                risk_reward_ratio=Decimal("2.5"),
                rationale="Kháng cự Daily.",
            ),
        ),
    )
    report = service._markdown_report(
        decision=decision,
        cutoff=NOW,
        reports={},
        snapshot=snapshot,
    )
    assert "Kịch bản giao dịch Phái sinh (Futures Setups — Tham khảo, Non-executing)" in report
    assert "**Thiên hướng Futures (Bias):** **BULLISH**" in report
    assert "100000" in report
    assert "98000" in report
    assert "105000" in report
    assert "SHORT" in report
    assert "LONG" in report


def test_analyze_links_with_prior_valid_run(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.save_snapshot(
        PortfolioSnapshot(
            environment="testnet",
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("10000"),
            positions=(),
            open_orders=(),
            as_of=NOW.isoformat(),
        )
    )
    committee = FakeCommittee()
    current_time = NOW
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=committee,
        now=lambda: current_time,
    )

    # First analyze run (no prior thesis)
    run_1 = service.analyze("BTCUSDT")
    assert run_1.decision.thesis_continuity == "NEW"
    assert committee.last_prior_thesis is None

    # Second analyze run 4 hours later
    current_time = NOW + timedelta(hours=4)
    run_2 = service.analyze("BTCUSDT")

    assert committee.last_prior_thesis is not None
    assert committee.last_prior_thesis["run_id"] == run_1.run_id
    assert committee.last_prior_thesis["hours_ago"] == "4.0"
    assert committee.last_prior_thesis["prior_price"] == "100"
    assert committee.last_prior_thesis["current_price"] == "100"
    assert committee.last_prior_thesis["action"] == "ACCUMULATE"

    # Verify artifacts
    assert (run_2.report_dir / "prior_thesis.json").is_file()
    prior_json = json.loads((run_2.report_dir / "prior_thesis.json").read_text(encoding="utf-8"))
    assert prior_json["run_id"] == run_1.run_id

    report_text = (run_2.report_dir / "report.md").read_text(encoding="utf-8")
    assert "## Đối soát Luận điểm Trước (Thesis Tracking)" in report_text
    assert run_1.run_id in report_text


def test_latest_valid_run_respects_before_cutoff(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    t1 = NOW
    t2 = NOW + timedelta(hours=2)
    t3 = NOW + timedelta(hours=4)

    decision_1 = ResearchDecision(
        symbol="BTCUSDT",
        action="ACCUMULATE",
        conviction=Decimal("7"),
        bull_case="Bull 1",
        bear_case="Bear 1",
        catalysts=(),
        invalidation="Inv 1",
        entry=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        evidence_ids=("ev1",),
        reason="reason 1",
    )
    decision_2 = ResearchDecision(
        symbol="BTCUSDT",
        action="HOLD",
        conviction=Decimal("6"),
        bull_case="Bull 2",
        bear_case="Bear 2",
        catalysts=(),
        invalidation="Inv 2",
        entry=Decimal("105"),
        stop=Decimal("95"),
        target=Decimal("125"),
        evidence_ids=("ev2",),
        reason="reason 2",
    )

    store.save_run("run-1", t1.isoformat(), decision_1, tmp_path / "run-1")
    store.save_run("run-2", t2.isoformat(), decision_2, tmp_path / "run-2")

    # At t3, latest before t3 is run-2
    latest_at_t3 = store.latest_valid_run("BTCUSDT", before_cutoff=t3.isoformat())
    assert latest_at_t3 is not None
    assert latest_at_t3["id"] == "run-2"

    # At t2, latest strictly before t2 is run-1
    latest_at_t2 = store.latest_valid_run("BTCUSDT", before_cutoff=t2.isoformat())
    assert latest_at_t2 is not None
    assert latest_at_t2["id"] == "run-1"

    # Before t1, there is no prior run
    latest_before_t1 = store.latest_valid_run("BTCUSDT", before_cutoff=t1.isoformat())
    assert latest_before_t1 is None
