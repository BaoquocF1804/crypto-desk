from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from crypto_desk.data import EvidenceSnapshot
from crypto_desk.domain import EvidenceItem, SymbolRules
from crypto_desk.screener import screen


def snapshot_factory(
    *,
    symbol: str = "BTCUSDT",
    quote_volume: str = "500000000",
    spread: str = "0.001",
    momentum_20d: str = "0.05",
    momentum_60d: str = "0.10",
    days: int = 120,
    stale: bool = False,
) -> EvidenceSnapshot:
    latest = Decimal("100")
    closes = [latest for _ in range(days)]
    if days >= 21:
        closes[-21] = latest / (Decimal("1") + Decimal(momentum_20d))
    if days >= 61:
        closes[-61] = latest / (Decimal("1") + Decimal(momentum_60d))
    item = EvidenceItem.create(
        kind="spot",
        provider="fixture",
        source="fixture://spot",
        fetched_at="2026-07-17T00:15:00+00:00",
        as_of="2026-07-17T00:15:00+00:00",
        delayed=False,
        stale=stale,
        payload={"symbol": symbol},
    )
    return EvidenceSnapshot(
        symbol=symbol,
        cutoff="2026-07-17T00:15:00+00:00",
        rules=SymbolRules(
            symbol=symbol,
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
        ),
        items=(item,),
        binance_mid=latest,
        reference_usdt=latest,
        spread=Decimal(spread),
        quote_volume=Decimal(quote_volume),
        daily_closes=tuple(closes),
        four_hour_closes=(latest,) * 90,
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("120000"),
        news_count=1,
    )


def test_screen_rejects_illiquid_or_wide_spread():
    illiquid = snapshot_factory(quote_volume="49999999", spread="0.001")
    wide = snapshot_factory(quote_volume="50000000", spread="0.0021")

    assert not screen(illiquid).passes
    assert "quote_volume" in screen(illiquid).reasons
    assert not screen(wide).passes
    assert "spread" in screen(wide).reasons


def test_screen_rejects_insufficient_or_non_positive_momentum():
    short_history = snapshot_factory(days=89)
    falling = snapshot_factory(momentum_20d="-0.01")

    assert not screen(short_history).passes
    assert "daily_history" in screen(short_history).reasons
    assert not screen(falling).passes
    assert "momentum_20d" in screen(falling).reasons


def test_screen_rejects_stale_evidence_and_symbols_outside_runtime_allowlist():
    stale = snapshot_factory(stale=True)
    excluded = snapshot_factory(symbol="SOLUSDT")

    assert not screen(stale).passes
    assert "stale_evidence" in screen(stale).reasons
    assert not screen(excluded, allowlist=("BTCUSDT", "ETHUSDT")).passes
    assert "allowlist" in screen(excluded, allowlist=("BTCUSDT", "ETHUSDT")).reasons


def test_screen_orders_passes_by_deterministic_score():
    weak = snapshot_factory(
        symbol="ETHUSDT",
        momentum_20d="0.02",
        momentum_60d="0.05",
    )
    strong = snapshot_factory(
        symbol="BTCUSDT",
        momentum_20d="0.08",
        momentum_60d="0.15",
    )

    results = sorted(
        (screen(weak), screen(strong)),
        key=lambda result: result.score,
        reverse=True,
    )

    assert all(result.passes for result in results)
    assert results[0].symbol == "BTCUSDT"
    assert results[0].score > results[1].score
    assert results[0].evidence_ids == strong.evidence_ids


def test_score_uses_exact_decimal_formula():
    snapshot = snapshot_factory(
        quote_volume="250000000",
        spread="0.001",
        momentum_20d="0.08",
        momentum_60d="0.12",
    )

    result = screen(snapshot)

    assert result.score == Decimal("0.1659")
    assert replace(result, score=Decimal("0")).symbol == "BTCUSDT"
