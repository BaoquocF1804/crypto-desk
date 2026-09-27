"""Quét toàn thị trường bằng model rẻ để tìm đồng đáng đưa vào watchlist nghiên cứu.

Code tính mọi con số; Flash chỉ xếp hạng cả bảng trong một lần gọi. Kết quả chỉ phục
vụ nghiên cứu: không đồng nào đi qua đây được tạo ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from typing import Any

from .data import EvidenceError, _pct_change, _percentile_rank, _utc_from_ms, price_structure
from .indicators import atr, ema

UNIVERSE_SIZE = 30
MIN_SCAN_QUOTE_VOLUME = Decimal("20000000")
# Stablecoins, fiat and gold tokens: no 20-day crypto thesis to find there.
NON_CRYPTO_BASES = frozenset(
    {
        "USDC",
        "FDUSD",
        "TUSD",
        "USDP",
        "DAI",
        "BUSD",
        "USDE",
        "USD1",
        "BFUSD",
        "XUSD",
        "RLUSD",
        "EUR",
        "EURI",
        "AEUR",
        "PAXG",
        "XAUT",
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    base_asset: str
    coingecko_id: str
    quote_volume: Decimal


def discover_universe(
    tickers: list[dict[str, Any]],
    spot_info: dict[str, Any],
    futures_info: dict[str, Any],
    markets: list[dict[str, Any]],
    *,
    exclude: frozenset[str],
) -> tuple[Candidate, ...]:
    """Cặp USDT đủ điều kiện để committee phân tích được, xếp theo volume 24h."""
    perpetuals = {
        item["symbol"]
        for item in futures_info.get("symbols", [])
        if item.get("contractType") == "PERPETUAL" and item.get("status") == "TRADING"
    }
    coingecko: dict[str, str] = {}
    for market in markets:
        # coins/markets is sorted by market cap, so the first coin per ticker is the largest.
        coingecko.setdefault(str(market.get("symbol", "")).upper(), str(market["id"]))
    spot = {item["symbol"]: item for item in spot_info.get("symbols", [])}
    candidates: list[Candidate] = []
    for ticker in tickers:
        symbol = str(ticker.get("symbol", ""))
        meta = spot.get(symbol)
        if meta is None or symbol in exclude:
            continue
        base = str(meta.get("baseAsset", ""))
        if (
            meta.get("quoteAsset") != "USDT"
            or meta.get("status") != "TRADING"
            or not meta.get("isSpotTradingAllowed")
            or not meta.get("ocoAllowed")
            or not meta.get("otoAllowed")
            or base in NON_CRYPTO_BASES
            or symbol not in perpetuals
            or base not in coingecko
        ):
            continue
        try:
            volume = Decimal(str(ticker["quoteVolume"]))
        except (KeyError, InvalidOperation):
            continue
        if volume >= MIN_SCAN_QUOTE_VOLUME:
            candidates.append(Candidate(symbol, base, coingecko[base], volume))
    candidates.sort(key=lambda candidate: (-candidate.quote_volume, candidate.symbol))
    return tuple(candidates[:UNIVERSE_SIZE])


CENT = Decimal("0.01")
# Price levels stay unrounded here; only the ratios the model reads are quantized.
LEVEL_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class ScanFeatures:
    symbol: str
    quote_volume_musd: Decimal
    change_20d_pct: Decimal | None
    ema20_gap_pct: Decimal | None
    ema20_above_ema50: bool | None
    atr1d_pct: Decimal | None
    range_position_20d: Decimal | None
    range_position_55d: Decimal | None
    support_distance_atr: Decimal | None
    resistance_distance_atr: Decimal | None
    long_short_pctile_20d: Decimal | None
    taker_buy_sell_24h: Decimal | None
    oi_change_24h_pct: Decimal | None
    funding_rate: Decimal | None


FEATURE_FIELDS = tuple(field.name for field in fields(ScanFeatures) if field.name != "symbol")


def _gap_pct(value: Decimal, base: Decimal | None) -> Decimal | None:
    return None if not base else ((value / base - 1) * 100).quantize(CENT)


def _range_position(
    close: Decimal, highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], window: int
) -> Decimal | None:
    if len(highs) < window:
        return None
    high, low = max(highs[-window:]), min(lows[-window:])
    if high == low:
        return Decimal("50")
    return ((close - low) / (high - low) * 100).quantize(Decimal("1"))


def compute_features(
    candidate: Candidate,
    daily_rows: list[list[Any]],
    oi_rows: list[dict[str, Any]],
    ls_rows: list[dict[str, Any]],
    taker_rows: list[dict[str, Any]],
    funding_rate: Decimal | None,
) -> ScanFeatures:
    highs = tuple(Decimal(str(row[2])) for row in daily_rows)
    lows = tuple(Decimal(str(row[3])) for row in daily_rows)
    closes = tuple(Decimal(str(row[4])) for row in daily_rows)
    if len(closes) < 21 or not all(value > 0 for value in (*highs, *lows, *closes)):
        raise EvidenceError(f"{candidate.symbol} lacks 21 valid closed daily candles")
    close = closes[-1]
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    atr14 = atr(highs, lows, closes, 14)
    dates = tuple(_utc_from_ms(row[0]).date().isoformat() for row in daily_rows)
    structure = price_structure(highs, lows, dates, close, LEVEL_QUANTUM)
    supports, resistances = structure["swing_supports"], structure["swing_resistances"]
    support = supports[0]["price"] if supports else structure["low_20d"]
    resistance = resistances[0]["price"] if resistances else None
    sold = sum((Decimal(str(row["sellVol"])) for row in taker_rows), Decimal(0))
    bought = sum((Decimal(str(row["buyVol"])) for row in taker_rows), Decimal(0))
    open_interest = [Decimal(str(row["sumOpenInterest"])) for row in oi_rows]
    oi_change = (
        _pct_change(open_interest[0], open_interest[-1]) if len(open_interest) >= 25 else None
    )
    return ScanFeatures(
        symbol=candidate.symbol,
        quote_volume_musd=(candidate.quote_volume / 1_000_000).quantize(CENT),
        change_20d_pct=_gap_pct(close, closes[-21]),
        ema20_gap_pct=_gap_pct(close, ema20),
        ema20_above_ema50=None if ema20 is None or ema50 is None else ema20 > ema50,
        atr1d_pct=None if not atr14 else (atr14 / close * 100).quantize(CENT),
        range_position_20d=_range_position(close, highs, lows, 20),
        range_position_55d=_range_position(close, highs, lows, 55),
        support_distance_atr=(
            None if not atr14 or support is None else ((close - support) / atr14).quantize(CENT)
        ),
        resistance_distance_atr=(
            None
            if not atr14 or resistance is None
            else ((resistance - close) / atr14).quantize(CENT)
        ),
        long_short_pctile_20d=(
            _percentile_rank([Decimal(str(row["longShortRatio"])) for row in ls_rows])
            if ls_rows
            else None
        ),
        taker_buy_sell_24h=(bought / sold).quantize(Decimal("0.0001")) if sold else None,
        oi_change_24h_pct=None if oi_change is None else oi_change.quantize(CENT),
        funding_rate=funding_rate,
    )
