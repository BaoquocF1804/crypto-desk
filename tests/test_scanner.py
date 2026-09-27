from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_desk.committee import ProviderError
from crypto_desk.data import EvidenceError, Fetched
from crypto_desk.scanner import (
    FEATURE_FIELDS,
    MAX_PICKS,
    UNIVERSE_SIZE,
    Candidate,
    ScanError,
    WatchlistScanner,
    compute_features,
    discover_universe,
    rank_candidates,
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


class FakeScanLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _pick(symbol: str, score: str = "7", fields=("support_distance_atr",)) -> dict:
    return {
        "symbol": symbol,
        "evidence_score": score,
        "thesis": f"{symbol} trend holds above support.",
        "supporting_fields": list(fields),
    }


def _features(*symbols: str):
    closes = [100 + index for index in range(60)]
    return [
        compute_features(
            Candidate(symbol, symbol[:-4], symbol.lower(), Decimal("50000000")),
            _rows(closes),
            OI_ROWS,
            LS_ROWS,
            TAKER_ROWS,
            None,
        )
        for symbol in symbols
    ]


def test_ranking_returns_valid_picks_highest_score_first():
    llm = FakeScanLLM({"picks": [_pick("AUSDT", "6"), _pick("BUSDT", "8")], "summary": "Two."})

    ranking = rank_candidates(llm, _features("AUSDT", "BUSDT"), model="flash", thinking="low")

    assert [pick.symbol for pick in ranking.picks] == ["BUSDT", "AUSDT"]
    assert llm.calls[0]["stage"] == "scan"
    assert llm.calls[0]["model"] == "flash"
    assert [row["symbol"] for row in llm.calls[0]["payload"]["candidates"]] == ["AUSDT", "BUSDT"]


def test_ranking_retries_once_with_the_validation_error():
    llm = FakeScanLLM(
        {"picks": [_pick("AUSDT", fields=("sharpe_ratio",))], "summary": "Invented field."},
        {"picks": [_pick("AUSDT")], "summary": "Corrected."},
    )

    ranking = rank_candidates(llm, _features("AUSDT"), model="flash", thinking="low")

    assert ranking.picks[0].symbol == "AUSDT"
    assert "failed validation" in llm.calls[1]["system_prompt"]


@pytest.mark.parametrize(
    "picks",
    [
        [_pick("ZZZUSDT")],
        [_pick("AUSDT"), _pick("AUSDT")],
    ],
)
def test_ranking_fails_after_two_invalid_answers(picks):
    llm = FakeScanLLM({"picks": picks, "summary": "Bad."}, {"picks": picks, "summary": "Bad."})

    with pytest.raises(ScanError):
        rank_candidates(llm, _features("AUSDT"), model="flash", thinking="low")


def test_ranking_takes_up_to_max_picks():
    symbols = [f"C{index:02d}USDT" for index in range(MAX_PICKS + 1)]
    too_many = {"picks": [_pick(symbol) for symbol in symbols], "summary": "Eleven."}
    llm = FakeScanLLM(too_many, {**too_many, "picks": too_many["picks"][:MAX_PICKS]})

    ranking = rank_candidates(llm, _features(*symbols), model="flash", thinking="low")

    assert MAX_PICKS == 10
    assert len(ranking.picks) == MAX_PICKS
    assert "failed validation" in llm.calls[1]["system_prompt"]


def test_ranking_may_pick_nothing():
    llm = FakeScanLLM({"picks": [], "summary": "No strong evidence today."})

    assert rank_candidates(llm, _features("AUSDT"), model="flash", thinking="low").picks == []


def _fetched(payload):
    return Fetched("fixture", "fixture://scan", NOW, NOW, payload)


class FakeMarket:
    def __init__(self, *, fail_symbol: str | None = None, broken_listing: bool = False):
        self.fail_symbol = fail_symbol
        self.broken_listing = broken_listing

    def ticker_24h_all(self):
        if self.broken_listing:
            raise EvidenceError("HTTP transport error fetching ticker")
        return _fetched(
            [
                {"symbol": "NEARUSDT", "quoteVolume": "160000000"},
                {"symbol": "AVAXUSDT", "quoteVolume": "44000000"},
            ]
        )

    def spot_exchange_info_all(self):
        return _fetched({"symbols": [_spot("NEARUSDT", "NEAR"), _spot("AVAXUSDT", "AVAX")]})

    def futures_exchange_info(self):
        return _fetched(
            {
                "symbols": [
                    {"symbol": symbol, "contractType": "PERPETUAL", "status": "TRADING"}
                    for symbol in ("NEARUSDT", "AVAXUSDT")
                ]
            }
        )

    def coingecko_markets(self):
        return _fetched(
            [{"id": "near", "symbol": "near"}, {"id": "avalanche-2", "symbol": "avax"}]
        )

    def premium_index_all(self):
        return _fetched(
            [
                {"symbol": "NEARUSDT", "lastFundingRate": "0.0001"},
                {"symbol": "BTCUSDT_260925", "lastFundingRate": ""},
                {"symbol": "UNKNOWN", "lastFundingRate": None},
            ]
        )

    def klines(self, symbol, interval, limit):
        if symbol == self.fail_symbol:
            raise EvidenceError(f"No {interval} klines for {symbol}")
        rows = _rows([100 + index for index in range(60)])
        # Binance appends the still-open candle; it must not reach the table.
        open_candle = [rows[-1][6] + 1, "999", "999", "999", "999", "0", rows[-1][6] + DAY_MS]
        return _fetched(rows + [open_candle])

    def open_interest_hist(self, symbol, period="1h", limit=25):
        return _fetched(OI_ROWS)

    def global_long_short_ratio(self, symbol, period="1h", limit=500):
        return _fetched(LS_ROWS)

    def taker_long_short_ratio(self, symbol, period="1h", limit=24):
        return _fetched(TAKER_ROWS)


def test_scanner_drops_a_failing_coin_and_ranks_the_rest():
    llm = FakeScanLLM({"picks": [_pick("NEARUSDT")], "summary": "One."})
    scanner = WatchlistScanner(
        FakeMarket(fail_symbol="AVAXUSDT"), llm, model="flash", thinking="low", now=lambda: NOW
    )

    result = scanner.run(frozenset())

    assert result.error is None
    assert [candidate.symbol for candidate in result.universe] == ["NEARUSDT", "AVAXUSDT"]
    assert list(result.dropped) == ["AVAXUSDT"]
    assert [row["symbol"] for row in llm.calls[0]["payload"]["candidates"]] == ["NEARUSDT"]
    near = result.features[0]
    assert near.funding_rate == Decimal("0.0001")
    assert near.change_20d_pct == Decimal("14.39")  # the open 999 candle was dropped
    assert result.ranking.picks[0].symbol == "NEARUSDT"


def test_scanner_reports_a_listing_failure_without_ranking():
    llm = FakeScanLLM()
    scanner = WatchlistScanner(
        FakeMarket(broken_listing=True), llm, model="flash", thinking="low", now=lambda: NOW
    )

    result = scanner.run(frozenset())

    assert result.error.startswith("universe:")
    assert result.ranking is None
    assert llm.calls == []


def test_scanner_reports_a_provider_failure():
    llm = FakeScanLLM(ProviderError("rate_limit"))
    scanner = WatchlistScanner(FakeMarket(), llm, model="flash", thinking="low", now=lambda: NOW)

    result = scanner.run(frozenset())

    assert result.error == "provider:rate_limit"
    assert len(result.features) == 2
