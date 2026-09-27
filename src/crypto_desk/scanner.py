"""Quét toàn thị trường bằng model rẻ để tìm đồng đáng đưa vào watchlist nghiên cứu.

Code tính mọi con số; Flash chỉ xếp hạng cả bảng trong một lần gọi. Kết quả chỉ phục
vụ nghiên cứu: không đồng nào đi qua đây được tạo ticket.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

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
