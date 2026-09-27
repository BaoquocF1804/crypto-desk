from __future__ import annotations

from decimal import Decimal

from crypto_desk.scanner import UNIVERSE_SIZE, discover_universe


def _spot(symbol: str, base: str, **overrides) -> dict:
    meta = {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": "USDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "ocoAllowed": True,
        "otoAllowed": True,
    }
    return meta | overrides


def _universe(rows, *, exclude=frozenset()):
    """rows: (symbol, base, quote_volume, spot_overrides, has_perpetual, coingecko_id)."""
    tickers = [{"symbol": row[0], "quoteVolume": str(row[2])} for row in rows]
    spot = {"symbols": [_spot(row[0], row[1], **row[3]) for row in rows]}
    futures = {
        "symbols": [
            {"symbol": row[0], "contractType": "PERPETUAL", "status": "TRADING"}
            for row in rows
            if row[4]
        ]
    }
    markets = [{"id": row[5], "symbol": row[1].lower()} for row in rows if row[5]]
    return discover_universe(tickers, spot, futures, markets, exclude=exclude)


def test_universe_keeps_only_liquid_usdt_pairs_with_perpetuals_and_a_coingecko_id():
    universe = _universe(
        [
            ("NEARUSDT", "NEAR", 160_000_000, {}, True, "near"),
            ("USDCUSDT", "USDC", 900_000_000, {}, True, "usd-coin"),
            ("PEPEUSDT", "PEPE", 23_000_000, {}, False, "pepe"),
            ("RAREUSDT", "RARE", 28_000_000, {}, True, None),
            ("LOWUSDT", "LOW", 19_000_000, {}, True, "low"),
            ("NOOCOUSDT", "NOOCO", 90_000_000, {"ocoAllowed": False}, True, "nooco"),
            ("HALTUSDT", "HALT", 90_000_000, {"status": "BREAK"}, True, "halt"),
            ("ETHBTC", "ETH", 900_000_000, {"quoteAsset": "BTC"}, True, "ethereum"),
            ("BTCUSDT", "BTC", 600_000_000, {}, True, "bitcoin"),
        ],
        exclude=frozenset({"BTCUSDT"}),
    )

    assert [candidate.symbol for candidate in universe] == ["NEARUSDT"]
    assert universe[0].coingecko_id == "near"
    assert universe[0].quote_volume == Decimal("160000000")


def test_universe_keeps_the_top_thirty_by_volume():
    rows = [
        (f"C{index:02d}USDT", f"C{index:02d}", 100_000_000 - index, {}, True, f"coin-{index}")
        for index in range(35)
    ]

    universe = _universe(rows)

    assert len(universe) == UNIVERSE_SIZE
    assert (universe[0].symbol, universe[-1].symbol) == ("C00USDT", "C29USDT")


def test_a_shared_ticker_maps_to_the_largest_coin():
    # coins/markets comes sorted by market cap, so the first coin per ticker is the largest.
    universe = discover_universe(
        [{"symbol": "GRAMUSDT", "quoteVolume": "50000000"}],
        {"symbols": [_spot("GRAMUSDT", "GRAM")]},
        {"symbols": [{"symbol": "GRAMUSDT", "contractType": "PERPETUAL", "status": "TRADING"}]},
        [{"id": "gram-big", "symbol": "gram"}, {"id": "gram-small", "symbol": "gram"}],
        exclude=frozenset(),
    )

    assert universe[0].coingecko_id == "gram-big"
