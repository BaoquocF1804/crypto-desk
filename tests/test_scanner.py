from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_desk.data import EvidenceError
from crypto_desk.scanner import (
    FEATURE_FIELDS,
    UNIVERSE_SIZE,
    Candidate,
    compute_features,
    discover_universe,
)


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


NOW = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
DAY_MS = 86_400_000
NEAR = Candidate("NEARUSDT", "NEAR", "near", Decimal("160000000"))


def _rows(closes: list[int]) -> list[list]:
    """Closed daily candles ending six hours before NOW; high/low = close ± 2."""
    last_close = int(NOW.timestamp() * 1000) - 6 * 3_600_000
    count = len(closes)
    return [
        [
            last_close - (count - index) * DAY_MS + 1,
            str(close),
            str(close + 2),
            str(close - 2),
            str(close),
            "0",
            last_close - (count - 1 - index) * DAY_MS,
        ]
        for index, close in enumerate(closes)
    ]


OI_ROWS = [{"sumOpenInterest": "100"}] + [{"sumOpenInterest": "105"}] * 23 + [
    {"sumOpenInterest": "110"}
]
LS_ROWS = [{"longShortRatio": "1.0"}, {"longShortRatio": "2.0"}, {"longShortRatio": "1.5"}]
TAKER_ROWS = [{"buyVol": "60", "sellVol": "40"}, {"buyVol": "40", "sellVol": "60"}]


def test_features_of_a_steady_uptrend_with_no_overhead_resistance():
    closes = [100 + index for index in range(60)]

    features = compute_features(
        NEAR, _rows(closes), OI_ROWS, LS_ROWS, TAKER_ROWS, Decimal("0.0001")
    )

    assert features.change_20d_pct == Decimal("14.39")  # 159 / 139 - 1
    assert features.ema20_above_ema50 is True
    assert features.atr1d_pct == Decimal("2.52")  # every true range is 4 → 4 / 159
    assert features.range_position_20d == Decimal("91")  # (159 - 138) / (161 - 138)
    assert features.support_distance_atr == Decimal("5.25")  # no swing low → low_20d 138
    assert features.resistance_distance_atr is None
    assert features.long_short_pctile_20d == Decimal("67")
    assert features.taker_buy_sell_24h == Decimal("1.0000")
    assert features.oi_change_24h_pct == Decimal("10.00")
    assert features.quote_volume_musd == Decimal("160.00")


def test_features_measure_the_distance_to_a_swing_high_above_price():
    closes = [100 + index for index in range(50)] + [
        149 - 2 * (index - 49) for index in range(50, 60)
    ]

    features = compute_features(NEAR, _rows(closes), OI_ROWS, LS_ROWS, TAKER_ROWS, None)

    # Peak high 151 at index 49; last close 129; ATR 4.
    assert features.resistance_distance_atr == Decimal("5.50")
    assert features.support_distance_atr == Decimal("0.50")  # low_20d 127
    assert features.funding_rate is None


def test_too_little_history_is_an_evidence_error():
    with pytest.raises(EvidenceError):
        compute_features(NEAR, _rows(list(range(100, 110))), OI_ROWS, LS_ROWS, TAKER_ROWS, None)


def test_feature_fields_name_every_field_but_the_symbol():
    assert "symbol" not in FEATURE_FIELDS
    assert {"support_distance_atr", "long_short_pctile_20d"} <= set(FEATURE_FIELDS)
