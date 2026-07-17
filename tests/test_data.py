from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from crypto_desk.data import (
    EvidenceBuilder,
    EvidenceError,
    Fetched,
    PublicDataClient,
    _parse_feed,
)


CUTOFF = datetime(2026, 7, 17, 0, 15, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


def rss_xml(title: str, published: datetime) -> str:
    return (
        "<rss><channel><item>"
        f"<title>{title}</title>"
        f"<link>https://example.test/{title}</link>"
        f"<pubDate>{published.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        "</item></channel></rss>"
    )


def fetched(
    payload,
    *,
    provider: str = "binance",
    source: str = "https://api.binance.com/test",
    as_of: datetime | None = None,
) -> Fetched:
    return Fetched(
        provider=provider,
        source=source,
        fetched_at=CUTOFF,
        as_of=as_of or CUTOFF - timedelta(minutes=5),
        payload=payload,
    )


class FakePublicClient:
    def __init__(self):
        daily_close = int((CUTOFF - timedelta(minutes=15)).timestamp() * 1000)
        four_hour_close = daily_close
        self.exchange = fetched(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                        "ocoAllowed": True,
                        "otoAllowed": True,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.00001",
                                "minQty": "0.00001",
                            },
                            {"filterType": "NOTIONAL", "minNotional": "5"},
                        ],
                    }
                ]
            }
        )
        self.daily = fetched(
            [
                [0, "0", "0", "0", str(90_000 + index * 100), "0", daily_close]
                for index in range(120)
            ],
            as_of=CUTOFF - timedelta(minutes=15),
        )
        self.four_hour = fetched(
            [
                [0, "0", "0", "0", str(98_000 + index * 10), "0", four_hour_close]
                for index in range(180)
            ],
            as_of=CUTOFF - timedelta(minutes=15),
        )
        self.depth_result = fetched(
            {"bids": [["99990", "2"]], "asks": [["100010", "2"]]},
            as_of=CUTOFF - timedelta(seconds=10),
        )
        self.book = fetched(
            {"bidPrice": "99990", "askPrice": "100010"},
            as_of=CUTOFF - timedelta(seconds=10),
        )
        self.ticker = fetched(
            {"quoteVolume": "750000000"},
            as_of=CUTOFF - timedelta(seconds=10),
        )
        self.funding_result = fetched(
            {"lastFundingRate": "0.0001", "time": int(CUTOFF.timestamp() * 1000)},
            provider="binance-usdm",
            source="https://fapi.binance.com/fapi/v1/premiumIndex",
        )
        self.open_interest_result = fetched(
            {"openInterest": "120000", "time": int(CUTOFF.timestamp() * 1000)},
            provider="binance-usdm",
            source="https://fapi.binance.com/fapi/v1/openInterest",
        )
        self.reference = fetched(
            {"bitcoin": {"usd": 100_000}, "tether": {"usd": 1}},
            provider="coingecko",
            source="https://api.coingecko.com/api/v3/simple/price",
        )
        self.news_result = fetched(
            [
                {
                    "title": "Institutional crypto flows rise",
                    "url": "https://example.test/story",
                    "published_at": (CUTOFF - timedelta(hours=1)).isoformat(),
                }
            ],
            provider="rss",
            source="https://example.test/feed.xml",
            as_of=CUTOFF - timedelta(hours=1),
        )

    def exchange_info(self, symbol: str) -> Fetched:
        return self.exchange

    def klines(self, symbol: str, interval: str, limit: int) -> Fetched:
        return self.daily if interval == "1d" else self.four_hour

    def depth(self, symbol: str) -> Fetched:
        return self.depth_result

    def book_ticker(self, symbol: str) -> Fetched:
        return self.book

    def ticker_24h(self, symbol: str) -> Fetched:
        return self.ticker

    def funding(self, symbol: str) -> Fetched:
        return self.funding_result

    def open_interest(self, symbol: str) -> Fetched | None:
        return self.open_interest_result

    def coingecko(self, coin_id: str) -> Fetched:
        return self.reference

    def news(self) -> Fetched:
        return self.news_result


def test_valid_snapshot_contains_all_evidence_kinds_and_symbol_rules():
    snapshot = EvidenceBuilder(FakePublicClient(), {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)

    assert {item.kind for item in snapshot.items} == {
        "spot",
        "news",
        "derivatives",
        "reference",
    }
    assert snapshot.rules.step_size.as_tuple().exponent == -5
    assert snapshot.binance_mid == Decimal("100000")
    assert snapshot.reference_usdt == Decimal("100000")
    assert len(snapshot.daily_closes) == 120
    assert len(snapshot.evidence_ids) == 4


def test_price_deviation_over_half_percent_blocks_trade():
    client = FakePublicClient()
    client.reference = replace(
        client.reference,
        payload={"bitcoin": {"usd": 100_503}, "tether": {"usd": 1}},
    )

    with pytest.raises(EvidenceError, match="price deviation"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_future_timestamp_is_rejected():
    client = FakePublicClient()
    client.funding_result = replace(
        client.funding_result,
        as_of=CUTOFF + timedelta(seconds=1),
    )

    with pytest.raises(EvidenceError, match="future"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_open_trailing_candles_are_dropped_before_time_leakage_check():
    client = FakePublicClient()
    open_close = int((CUTOFF + timedelta(hours=23, minutes=45)).timestamp() * 1000)
    client.daily = replace(
        client.daily,
        as_of=CUTOFF + timedelta(hours=23, minutes=45),
        payload=[*client.daily.payload, [0, "0", "0", "0", "999999", "0", open_close]],
    )

    snapshot = EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)

    assert len(snapshot.daily_closes) == 120
    assert snapshot.daily_closes[-1] != Decimal("999999")


def test_missing_open_interest_blocks_evidence():
    client = FakePublicClient()
    client.open_interest_result = None

    with pytest.raises(EvidenceError, match="open interest"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_stale_book_ticker_blocks_evidence():
    client = FakePublicClient()
    client.book = replace(
        client.book,
        fetched_at=CUTOFF - timedelta(seconds=61),
        as_of=CUTOFF - timedelta(seconds=10),
    )

    with pytest.raises(EvidenceError, match="book ticker"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_news_older_than_48_hours_blocks_evidence():
    client = FakePublicClient()
    client.news_result = replace(
        client.news_result,
        payload=[
            {
                "title": "Old story",
                "url": "https://example.test/old",
                "published_at": (CUTOFF - timedelta(hours=49)).isoformat(),
            }
        ],
        as_of=CUTOFF - timedelta(hours=49),
    )

    with pytest.raises(EvidenceError, match="news"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_symbol_without_native_contingent_orders_is_rejected():
    client = FakePublicClient()
    client.exchange.payload["symbols"][0]["otoAllowed"] = False

    with pytest.raises(EvidenceError, match="OTO"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_non_usdt_exchange_symbol_is_rejected_as_evidence_error():
    client = FakePublicClient()
    client.exchange.payload["symbols"][0]["quoteAsset"] = "FDUSD"

    with pytest.raises(EvidenceError, match="USDT"):
        EvidenceBuilder(client, {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)


def test_rss_parser_keeps_metadata_and_content_hash():
    items = _parse_feed((FIXTURES / "news.xml").read_text(encoding="utf-8"))

    assert items[0]["title"] == "Institutional crypto flows rise"
    assert items[0]["url"] == "https://example.test/story"
    assert len(items[0]["content_hash"]) == 64
    assert "description" not in items[0]


def test_public_client_uses_only_fixed_public_endpoints_and_offline_fixtures():
    files = {
        "/api/v3/exchangeInfo": "binance_exchange_info.json",
        "/api/v3/klines": "binance_klines.json",
        "/api/v3/depth": "binance_depth.json",
        "/api/v3/ticker/bookTicker": "binance_book_ticker.json",
        "/api/v3/ticker/24hr": "binance_ticker_24h.json",
        "/fapi/v1/premiumIndex": "binance_funding.json",
        "/fapi/v1/openInterest": "binance_open_interest.json",
        "/api/v3/simple/price": "coingecko_price.json",
    }
    requested_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.add(request.url.host)
        if request.url.path == "/feed.xml":
            return httpx.Response(
                200,
                text=(FIXTURES / "news.xml").read_text(encoding="utf-8"),
            )
        payload = json.loads((FIXTURES / files[request.url.path]).read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload)

    client = PublicDataClient(
        ("https://example.test/feed.xml",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )
    assert client.exchange_info("BTCUSDT").payload["symbols"][0]["symbol"] == "BTCUSDT"
    assert client.klines("BTCUSDT", "1d", 120).payload[-1][4] == "100200"
    assert client.depth("BTCUSDT").payload["bids"]
    assert client.book_ticker("BTCUSDT").payload["bidPrice"] == "99990.00"
    assert client.ticker_24h("BTCUSDT").payload["quoteVolume"] == "750000000.00"
    assert client.funding("BTCUSDT").provider == "binance-usdm"
    assert client.open_interest("BTCUSDT").payload["openInterest"] == "120000.000"
    assert client.coingecko("bitcoin").payload["tether"]["usd"] == 1
    assert client.news().payload[0]["content_hash"]
    assert requested_hosts == {
        "api.binance.com",
        "fapi.binance.com",
        "api.coingecko.com",
        "example.test",
    }


def test_one_dead_feed_does_not_block_news_collection():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dead.test":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text=rss_xml("alive", CUTOFF - timedelta(hours=1)))

    client = PublicDataClient(
        ("https://dead.test/feed.xml", "https://example.test/feed.xml"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )

    result = client.news()

    assert len(result.payload) == 1
    assert result.payload[0]["title"] == "alive"
    assert result.source == "https://example.test/feed.xml"


def test_future_dated_item_is_dropped_and_does_not_poison_as_of():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "future.test":
            return httpx.Response(200, text=rss_xml("future", CUTOFF + timedelta(days=2)))
        return httpx.Response(200, text=rss_xml("current", CUTOFF - timedelta(hours=1)))

    client = PublicDataClient(
        ("https://future.test/feed.xml", "https://example.test/feed.xml"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )

    result = client.news()

    assert [item["title"] for item in result.payload] == ["current"]
    assert result.as_of <= CUTOFF


def test_default_client_follows_redirects():
    assert PublicDataClient(()).client.follow_redirects is True


def test_news_is_cached_within_a_run():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, text=rss_xml("cached", CUTOFF - timedelta(hours=1)))

    current = {"now": CUTOFF}
    client = PublicDataClient(
        ("https://example.test/feed.xml",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: current["now"],
    )

    first = client.news()
    second = client.news()
    assert calls["count"] == 1
    assert second is first

    current["now"] = CUTOFF + timedelta(seconds=301)
    third = client.news()
    assert calls["count"] == 2
    assert third is not first


def test_total_feed_failure_is_not_cached():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("down")

    client = PublicDataClient(
        ("https://example.test/feed.xml",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )

    assert client.news().payload == []
    first = calls["count"]
    assert client.news().payload == []
    assert calls["count"] > first


def test_transient_transport_error_is_retried_once(monkeypatch):
    monkeypatch.setattr("crypto_desk.data.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("transient")
        payload = json.loads((FIXTURES / "binance_book_ticker.json").read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload)

    client = PublicDataClient(
        (),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )

    assert client.book_ticker("BTCUSDT").payload["bidPrice"] == "99990.00"
    assert attempts["count"] == 2


def test_server_error_is_retried_then_raised(monkeypatch):
    monkeypatch.setattr("crypto_desk.data.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500, json={"error": "upstream"})

    client = PublicDataClient(
        (),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: CUTOFF,
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.book_ticker("BTCUSDT")
    assert attempts["count"] == 2
