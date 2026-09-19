from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from crypto_desk.config import Settings
from crypto_desk.dashboard import (
    CurrentAsset,
    DashboardPublishError,
    build_dashboard_snapshot,
    publish_dashboard,
    publish_dashboard_from_env,
)
from crypto_desk.domain import PortfolioSnapshot, ResearchDecision, iso
from crypto_desk.store import Store


FORBIDDEN_KEY_FRAGMENTS = {
    "api_key",
    "api_secret",
    "token",
    "telegram",
    "actor",
    "confirmation",
    "report_dir",
    "client_order_id",
    "payload",
}


def assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            assert not any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS), (
                f"forbidden key fragment found in key: {key}"
            )
            assert_no_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_forbidden_keys(item)


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"symbols": ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")}
    defaults.update(overrides)
    return Settings(**defaults)


def make_decision(
    *,
    symbol: str = "BTCUSDT",
    evidence_ids: tuple[str, ...] = ("evidence-1",),
    action: str = "HOLD",
    reason: str = "committee decision",
) -> ResearchDecision:
    return ResearchDecision(
        symbol=symbol,
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
        reason=reason,
    )


def unpriced_position(asset: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "symbol": f"{asset}USDT",
        "free": "1",
        "locked": "0",
        "total": "1",
        "mid_usdt": "0",
        "value_usdt": "0",
        "unpriced": True,
        "unpriced_reason": "pricing_unavailable",
    }


def external_position(asset: str, value_usdt: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "symbol": f"{asset}USDT",
        "free": "1",
        "locked": "0",
        "total": "1",
        "mid_usdt": value_usdt,
        "value_usdt": value_usdt,
        "external": True,
    }


def full_snapshot() -> PortfolioSnapshot:
    positions = [
        {
            "asset": "BTC",
            "symbol": "BTCUSDT",
            "free": "1.00000000",
            "locked": "0",
            "total": "1.00000000",
            "mid_usdt": "63963.28500000",
            "value_usdt": "63963.28500000",
        },
        {
            "asset": "ETH",
            "symbol": "ETHUSDT",
            "free": "10.00000000",
            "locked": "0",
            "total": "10.00000000",
            "mid_usdt": "2500.00000000",
            "value_usdt": "25000.00000000",
        },
        external_position("ADA", "1000"),
        external_position("XRP", "500"),
        *[unpriced_position(f"SHITCOIN{i}") for i in range(26)],
    ]
    return PortfolioSnapshot(
        environment="testnet",
        nav_usdt=Decimal("100000"),
        free_usdt=Decimal("10963.285"),
        positions=tuple(positions),
        open_orders=(),
        as_of=iso(),
    )


def test_snapshot_with_configured_and_external_assets(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.save_snapshot(full_snapshot())

    snapshot = build_dashboard_snapshot(settings, store)

    positions_by_symbol = {
        position.symbol: position for position in snapshot.portfolio.configured_positions
    }
    assert set(positions_by_symbol) == {"BTCUSDT", "ETHUSDT"}
    assert positions_by_symbol["BTCUSDT"].value_usdt == Decimal("63963.28500000")
    assert snapshot.portfolio.external_assets_count == 2
    assert snapshot.portfolio.external_value_usdt == Decimal("1500")
    assets = {asset.asset: asset for asset in snapshot.portfolio.assets}
    assert assets["USDT"].state == "cash"
    assert assets["BTC"].state == "managed"
    assert assets["ADA"].state == "external"
    assert assets["ADA"].mark == Decimal("1000")


def test_unpriced_assets_are_listed_without_fake_prices(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.save_snapshot(full_snapshot())

    snapshot = build_dashboard_snapshot(settings, store)
    assert snapshot.portfolio.unpriced_assets_count == 26
    unpriced = [asset for asset in snapshot.portfolio.assets if asset.state == "unpriced"]
    assert len(unpriced) == 26
    assert {asset.asset for asset in unpriced} == {f"SHITCOIN{i}" for i in range(26)}
    assert all(asset.mark is None and asset.value is None for asset in unpriced)


def test_current_asset_json_uses_plain_decimal_not_scientific_notation():
    asset = CurrentAsset(
        asset="TINY",
        quantity=Decimal("1"),
        mark=Decimal("9.75E-8"),
        value=Decimal("9.75E-8"),
        state="external",
    )

    payload = asset.model_dump(mode="json")

    assert payload["mark"] == "0.0000000975"
    assert payload["value"] == "0.0000000975"


def test_nav_gross_and_deployed_pct_use_decimal(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.save_snapshot(full_snapshot())

    snapshot = build_dashboard_snapshot(settings, store)

    assert isinstance(snapshot.portfolio.nav_usdt, Decimal)
    assert isinstance(snapshot.portfolio.gross_exposure_usdt, Decimal)
    assert isinstance(snapshot.portfolio.deployed_pct, Decimal)
    expected_gross = max(Decimal("100000") - Decimal("10963.285"), Decimal("0"))
    assert snapshot.portfolio.gross_exposure_usdt == expected_gross
    assert snapshot.portfolio.deployed_pct == expected_gross / Decimal("100000") * 100


def test_latest_blocked_attempt_and_latest_valid_fallback_both_present(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.save_snapshot(full_snapshot())
    valid = make_decision(evidence_ids=("evidence-1",))
    blocked = make_decision(evidence_ids=(), reason="binance evidence is from the future")
    store.save_run("run-1", "2026-07-18T05:14:11+00:00", valid, Path("artifacts/run-1"))
    store.save_run("run-2", "2026-07-18T05:45:23+00:00", blocked, Path("artifacts/run-2"))

    snapshot = build_dashboard_snapshot(settings, store)

    btc = next(item for item in snapshot.symbols if item.symbol == "BTCUSDT")
    assert btc.latest_attempt is not None
    assert btc.latest_attempt.state == "blocked"
    assert btc.latest_attempt.run_id == "run-2"
    assert btc.latest_attempt.reason == "binance evidence is from the future"
    assert btc.latest_valid_decision is not None
    assert btc.latest_valid_decision.run_id == "run-1"
    assert btc.latest_valid_decision.action == "HOLD"


def test_symbol_24h_change_comes_from_latest_valid_evidence(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    report_dir = tmp_path / "run-1"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "spot",
                        "payload": {"change_24h_pct": "2.5"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store.save_run(
        "run-1",
        "2026-07-18T05:14:11+00:00",
        make_decision(evidence_ids=("evidence-1",)),
        report_dir,
    )

    snapshot = build_dashboard_snapshot(settings, store)

    btc = next(item for item in snapshot.symbols if item.symbol == "BTCUSDT")
    assert btc.change_24h_pct == Decimal("2.5")


def test_legitimate_no_trade_with_evidence_is_valid_research(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    no_trade = make_decision(evidence_ids=("evidence-1",), action="NO_TRADE")
    store.save_run("run-1", "2026-07-18T05:14:11+00:00", no_trade, Path("artifacts/run-1"))

    snapshot = build_dashboard_snapshot(settings, store)

    btc = next(item for item in snapshot.symbols if item.symbol == "BTCUSDT")
    assert btc.latest_attempt is not None
    assert btc.latest_attempt.state == "valid"
    assert btc.latest_valid_decision is not None
    assert btc.latest_valid_decision.action == "NO_TRADE"


def test_no_snapshot_yields_safe_zeroed_portfolio(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()

    snapshot = build_dashboard_snapshot(settings, store)

    assert snapshot.portfolio.as_of is None
    assert snapshot.portfolio.nav_usdt == Decimal("0")
    assert snapshot.portfolio.gross_exposure_usdt == Decimal("0")
    assert snapshot.portfolio.deployed_pct == Decimal("0")
    assert snapshot.portfolio.assets == []
    assert snapshot.portfolio.configured_positions == []
    assert snapshot.portfolio.external_assets_count == 0
    assert snapshot.portfolio.unpriced_assets_count == 0


def test_no_research_for_bnb_and_sol(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    valid = make_decision(evidence_ids=("evidence-1",))
    store.save_run("run-1", "2026-07-18T05:14:11+00:00", valid, Path("artifacts/run-1"))

    snapshot = build_dashboard_snapshot(settings, store)

    for symbol in ("BNBUSDT", "SOLUSDT"):
        item = next(entry for entry in snapshot.symbols if entry.symbol == symbol)
        assert item.latest_attempt is None
        assert item.latest_valid_decision is None


def test_health_never_run(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()

    snapshot = build_dashboard_snapshot(settings, store)

    assert snapshot.health.last_health_at is None
    assert snapshot.health.runner_state == "offline"


def test_health_freshness_from_latest_scheduled_run(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.mark_scheduled("health", "2026-07-18T06:00Z")

    from datetime import UTC, datetime

    snapshot = build_dashboard_snapshot(
        settings, store, now=lambda: datetime(2026, 7, 18, 6, 5, tzinfo=UTC)
    )

    assert snapshot.health.last_health_at is not None
    assert snapshot.health.runner_state == "online"
    assert snapshot.health.stale_after_seconds == 1200


def test_serialized_payload_has_no_forbidden_keys(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.save_snapshot(full_snapshot())
    valid = make_decision(evidence_ids=("evidence-1",))
    store.save_run("run-1", "2026-07-18T05:14:11+00:00", valid, Path("artifacts/run-1"))
    store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})
    store.record_order_event("ticket-1", "FILLED", {"executedQty": "0.00025"})
    store.mark_scheduled("health", "2026-07-18T06:00Z")

    snapshot = build_dashboard_snapshot(settings, store)
    serialized = snapshot.model_dump(mode="json")

    assert_no_forbidden_keys(serialized)


def test_symbols_are_returned_in_configured_order(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings(
        symbols=("ETHUSDT", "BTCUSDT"),
        coingecko_ids={"ETHUSDT": "ethereum", "BTCUSDT": "bitcoin"},
        vn_symbols=("FPT", "MBB"),
    )

    snapshot = build_dashboard_snapshot(settings, store)

    assert [item.symbol for item in snapshot.symbols] == ["ETHUSDT", "BTCUSDT", "FPT", "MBB"]


def test_dashboard_snapshot_includes_vn_equities_with_evidence(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings(
        symbols=("BTCUSDT",),
        vn_symbols=("FPT",),
    )
    report_dir = tmp_path / "reports" / "r1"
    report_dir.mkdir(parents=True)
    (report_dir / "evidence.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "spot",
                        "payload": {
                            "symbol": "FPT",
                            "mid": "71700",
                            "ref_price": "74300",
                            "daily_closes": ["70000", "71000", "71700"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "analysts.json").write_text(
        json.dumps(
            {
                "flow": {
                    "stance": "bullish",
                    "confidence": "8",
                    "observations": ["Khối ngoại mua ròng mạnh"],
                },
                "fundamentals": {
                    "stance": "bullish",
                    "confidence": "7.5",
                    "observations": ["Biên lợi nhuận gộp cao"],
                },
            }
        ),
        encoding="utf-8",
    )
    store.save_run(
        "r1",
        "2026-09-20T00:00:00+00:00",
        make_decision(symbol="FPT", action="HOLD"),
        report_dir,
    )

    snapshot = build_dashboard_snapshot(settings, store)

    fpt = next(s for s in snapshot.symbols if s.symbol == "FPT")
    assert fpt.mark_usdt == Decimal("71700")
    assert fpt.change_24h_pct == Decimal("-3.50")
    assert fpt.sparkline_closes == [Decimal("70000"), Decimal("71000"), Decimal("71700")]
    assert fpt.committee_evaluation is not None
    assert any(vote.role == "flow" for vote in fpt.committee_evaluation.member_votes)
    assert any(vote.role == "fundamentals" for vote in fpt.committee_evaluation.member_votes)



def make_ticket(symbol: str = "BTCUSDT") -> Any:
    from crypto_desk.domain import TradeTicket

    return TradeTicket.create(
        environment="testnet",
        symbol=symbol,
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000"),
        stop_price=Decimal("90000"),
        target_price=Decimal("120000"),
        notional_usdt=Decimal("25"),
        risk_snapshot={},
        ttl_minutes=30,
    )


def test_operations_counts_tickets_and_orders(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    pending_ticket = make_ticket()
    store.save_ticket(pending_ticket)

    submitted_ticket = make_ticket()
    store.save_ticket(submitted_ticket)
    store.save_submission(submitted_ticket.id, "testnet", "desk_ticket_1", {"status": "NEW"})
    store.record_order_event(submitted_ticket.id, "FILLED", {"executedQty": "0.00025"})

    snapshot = build_dashboard_snapshot(settings, store)

    assert snapshot.operations.tickets_total == 2
    assert snapshot.operations.tickets_actionable == 1
    assert snapshot.operations.orders_total == 1
    assert snapshot.operations.orders_open == 0
    assert len(snapshot.operations.recent_events) == 1
    assert snapshot.operations.recent_events[0].kind == "order"
    assert snapshot.operations.recent_events[0].summary == "Order filled"


DASHBOARD_URL = "https://dashboard.example/api/ingest"


def make_dashboard_snapshot(tmp_path: Path) -> Any:
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    snapshot = build_dashboard_snapshot(settings, store)
    store.close()
    return snapshot


class _HugeSnapshotStub:
    def model_dump(self, mode: str) -> dict[str, Any]:
        return {"padding": "x" * (70 * 1024)}


@pytest.mark.parametrize("status", [200, 204])
def test_publish_dashboard_succeeds_on_2xx(tmp_path: Path, status: int):
    snapshot = make_dashboard_snapshot(tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    publish_dashboard(
        snapshot,
        url=DASHBOARD_URL,
        ingest_token="ingest-secret",
        sites_bypass_token="bypass-secret",
        client=client,
    )

    assert captured["method"] == "POST"
    assert captured["url"] == DASHBOARD_URL
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Authorization"] == "Bearer ingest-secret"
    assert captured["headers"]["OAI-Sites-Authorization"] == "Bearer bypass-secret"
    assert json.loads(captured["body"])["schema_version"] == 1


@pytest.mark.parametrize("status", [401, 403])
def test_publish_dashboard_authentication_failure_never_logs_tokens(tmp_path: Path, status: int):
    snapshot = make_dashboard_snapshot(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="remote debug body must never surface")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DashboardPublishError) as exc_info:
        publish_dashboard(
            snapshot,
            url=DASHBOARD_URL,
            ingest_token="ingest-secret",
            sites_bypass_token="bypass-secret",
            client=client,
        )

    message = str(exc_info.value)
    assert "ingest-secret" not in message
    assert "bypass-secret" not in message
    assert "remote debug body" not in message
    assert str(status) in message


def test_publish_dashboard_network_error_never_leaks_url_query_or_secrets(tmp_path: Path):
    snapshot = make_dashboard_snapshot(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DashboardPublishError) as exc_info:
        publish_dashboard(
            snapshot,
            url=f"{DASHBOARD_URL}?token=leaked-secret",
            ingest_token="ingest-secret",
            sites_bypass_token="bypass-secret",
            client=client,
        )

    message = str(exc_info.value)
    assert "leaked-secret" not in message
    assert "ingest-secret" not in message
    assert "bypass-secret" not in message
    assert "dashboard.example" not in message


def test_publish_dashboard_blocks_oversized_payload_before_request():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DashboardPublishError):
        publish_dashboard(
            _HugeSnapshotStub(),
            url=DASHBOARD_URL,
            ingest_token="ingest-secret",
            sites_bypass_token="bypass-secret",
            client=client,
        )

    assert calls["count"] == 0


def test_publish_dashboard_from_env_raises_clearly_when_a_var_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    snapshot = make_dashboard_snapshot(tmp_path)
    monkeypatch.delenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", raising=False)
    monkeypatch.delenv("CRYPTO_DESK_DASHBOARD_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", raising=False)

    with pytest.raises(DashboardPublishError) as exc_info:
        publish_dashboard_from_env(snapshot, strict=True)

    assert "CRYPTO_DESK_DASHBOARD_INGEST_URL" in str(exc_info.value)


def test_publish_dashboard_from_env_is_noop_when_not_strict_and_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    snapshot = make_dashboard_snapshot(tmp_path)
    monkeypatch.delenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", raising=False)

    publish_dashboard_from_env(snapshot, strict=False)


def test_publish_dashboard_from_env_sends_request_with_configured_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    snapshot = make_dashboard_snapshot(tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200)

    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", DASHBOARD_URL)
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_TOKEN", "ingest-secret")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "bypass-secret")
    real_client = httpx.Client
    monkeypatch.setattr(
        "crypto_desk.dashboard.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    publish_dashboard_from_env(snapshot, strict=True)

    assert captured["headers"]["Authorization"] == "Bearer ingest-secret"


def test_local_publish_does_not_require_sites_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    snapshot = make_dashboard_snapshot(tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200)

    monkeypatch.setenv(
        "CRYPTO_DESK_DASHBOARD_INGEST_URL",
        "http://localhost:3001/api/ingest",
    )
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_TOKEN", "ingest-secret")
    monkeypatch.delenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", raising=False)
    real_client = httpx.Client
    monkeypatch.setattr(
        "crypto_desk.dashboard.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    publish_dashboard_from_env(snapshot, strict=True)

    assert "OAI-Sites-Authorization" not in captured["headers"]
