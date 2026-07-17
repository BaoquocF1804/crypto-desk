from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from .domain import EvidenceItem, SymbolRules, iso, to_jsonable, utcnow


SPOT_PUBLIC = "https://api.binance.com"
FUTURES_PUBLIC = "https://fapi.binance.com"
COINGECKO_PUBLIC = "https://api.coingecko.com/api/v3"


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Fetched:
    provider: str
    source: str
    fetched_at: datetime
    as_of: datetime
    payload: Any


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    symbol: str
    cutoff: str
    rules: SymbolRules
    items: tuple[EvidenceItem, ...]
    binance_mid: Decimal
    reference_usdt: Decimal
    spread: Decimal
    quote_volume: Decimal
    daily_closes: tuple[Decimal, ...]
    four_hour_closes: tuple[Decimal, ...]
    funding_rate: Decimal
    open_interest: Decimal
    news_count: int

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)


def _utc_from_ms(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PublicDataClient:
    def __init__(
        self,
        news_feeds: tuple[str, ...],
        *,
        coingecko_key: str | None = None,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.news_feeds = news_feeds
        self.coingecko_key = coingecko_key or os.getenv("COINGECKO_DEMO_API_KEY")
        self.client = client or httpx.Client(timeout=10, follow_redirects=True)
        self.now = now

    def _json(
        self,
        base: str,
        path: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[str, Any, datetime]:
        url = f"{base}{path}"
        fetched_at = _aware(self.now())
        response = self.client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return str(response.url), response.json(), fetched_at

    def exchange_info(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/exchangeInfo", params={"symbol": symbol}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def klines(self, symbol: str, interval: str, limit: int) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC,
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not payload:
            raise EvidenceError(f"No {interval} klines for {symbol}")
        return Fetched(
            "binance",
            source,
            fetched_at,
            _utc_from_ms(payload[-1][6]),
            payload,
        )

    def depth(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/depth", params={"symbol": symbol, "limit": 20}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def book_ticker(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/ticker/bookTicker", params={"symbol": symbol}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def ticker_24h(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/ticker/24hr", params={"symbol": symbol}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def funding(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC, "/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        return Fetched(
            "binance-usdm",
            source,
            fetched_at,
            _utc_from_ms(payload["time"]),
            payload,
        )

    def open_interest(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC, "/fapi/v1/openInterest", params={"symbol": symbol}
        )
        return Fetched(
            "binance-usdm",
            source,
            fetched_at,
            _utc_from_ms(payload["time"]),
            payload,
        )

    def coingecko(self, coin_id: str) -> Fetched:
        headers = {"x-cg-demo-api-key": self.coingecko_key} if self.coingecko_key else None
        source, payload, fetched_at = self._json(
            COINGECKO_PUBLIC,
            "/simple/price",
            params={"ids": f"{coin_id},tether", "vs_currencies": "usd"},
            headers=headers,
        )
        return Fetched("coingecko", source, fetched_at, fetched_at, payload)

    def news(self) -> Fetched:
        fetched_at = _aware(self.now())
        collected: list[tuple[datetime, dict[str, str]]] = []
        sources: list[str] = []
        for url in self.news_feeds:
            try:
                response = self.client.get(url)
                response.raise_for_status()
                parsed = _parse_feed(response.text)
            except (httpx.HTTPError, ET.ParseError, ValueError, TypeError):
                continue
            sources.append(str(response.url))
            for item in parsed:
                published = datetime.fromisoformat(item["published_at"]).astimezone(UTC)
                if published <= fetched_at:
                    collected.append((published, item))
        as_of = max((published for published, _ in collected), default=fetched_at)
        return Fetched(
            "rss",
            ",".join(sources),
            fetched_at,
            as_of,
            [item for _, item in collected],
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def _parse_feed(text: str) -> list[dict[str, str]]:
    root = ET.fromstring(text)
    result: list[dict[str, str]] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _child_text(element, "title")
        url = _child_text(element, "link")
        if not url:
            link = next(
                (child for child in element if _local_name(child.tag) == "link"),
                None,
            )
            url = link.attrib.get("href", "") if link is not None else ""
        published_text = _child_text(element, "pubDate", "published", "updated")
        if not title or not url or not published_text:
            continue
        try:
            published = parsedate_to_datetime(published_text)
        except (TypeError, ValueError):
            published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        published_at = iso(_aware(published))
        content_hash = hashlib.sha256(f"{title}\n{url}\n{published_at}".encode()).hexdigest()
        result.append(
            {
                "title": title,
                "url": url,
                "published_at": published_at,
                "content_hash": content_hash,
            }
        )
    return result


class EvidenceBuilder:
    def __init__(self, client: Any, coingecko_ids: dict[str, str]):
        self.client = client
        self.coingecko_ids = coingecko_ids

    def build(self, symbol: str, cutoff: datetime) -> EvidenceSnapshot:
        cutoff = _aware(cutoff)
        if symbol not in self.coingecko_ids:
            raise EvidenceError(f"Missing CoinGecko id for {symbol}")

        exchange = self.client.exchange_info(symbol)
        daily = self._closed_klines(
            self.client.klines(symbol, "1d", 121),
            cutoff,
            "daily",
        )
        four_hour = self._closed_klines(
            self.client.klines(symbol, "4h", 181),
            cutoff,
            "4h",
        )
        depth = self.client.depth(symbol)
        book = self.client.book_ticker(symbol)
        ticker = self.client.ticker_24h(symbol)
        funding = self.client.funding(symbol)
        open_interest = self.client.open_interest(symbol)
        if open_interest is None:
            raise EvidenceError("Missing open interest")
        reference = self.client.coingecko(self.coingecko_ids[symbol])
        news = self.client.news()

        fetched = (
            exchange,
            daily,
            four_hour,
            depth,
            book,
            ticker,
            funding,
            open_interest,
            reference,
            news,
        )
        for item in fetched:
            if _aware(item.as_of) > cutoff:
                raise EvidenceError(f"{item.provider} evidence is from the future")

        self._fresh(
            cutoff,
            book,
            "book ticker",
            timedelta(seconds=60),
            use_fetched_at=True,
        )
        self._fresh(cutoff, four_hour, "4h candle", timedelta(hours=5))
        self._fresh(cutoff, daily, "daily candle", timedelta(hours=26))
        self._fresh(
            cutoff,
            funding,
            "funding",
            timedelta(minutes=15),
            use_fetched_at=True,
        )
        self._fresh(
            cutoff,
            open_interest,
            "open interest",
            timedelta(minutes=15),
            use_fetched_at=True,
        )

        rules = self._rules(exchange.payload, symbol)
        bid = Decimal(str(book.payload["bidPrice"]))
        ask = Decimal(str(book.payload["askPrice"]))
        if bid <= 0 or ask <= bid:
            raise EvidenceError("Invalid Binance book ticker")
        mid = (bid + ask) / Decimal("2")
        spread = (ask - bid) / mid

        coin = Decimal(str(reference.payload[self.coingecko_ids[symbol]]["usd"]))
        tether = Decimal(str(reference.payload["tether"]["usd"]))
        if coin <= 0 or tether <= 0:
            raise EvidenceError("Invalid CoinGecko reference price")
        reference_usdt = coin / tether
        deviation = abs(mid - reference_usdt) / reference_usdt
        if deviation > Decimal("0.005"):
            raise EvidenceError(f"Cross-source price deviation {deviation:.4%} exceeds 0.5%")

        recent_news = [
            item
            for item in news.payload
            if cutoff - timedelta(hours=48)
            <= datetime.fromisoformat(item["published_at"]).astimezone(UTC)
            <= cutoff
        ]
        if not recent_news:
            raise EvidenceError("No current news evidence in the previous 48 hours")

        daily_closes = tuple(Decimal(str(row[4])) for row in daily.payload)
        four_hour_closes = tuple(Decimal(str(row[4])) for row in four_hour.payload)
        funding_rate = Decimal(str(funding.payload["lastFundingRate"]))
        open_interest_value = Decimal(str(open_interest.payload["openInterest"]))
        quote_volume = Decimal(str(ticker.payload["quoteVolume"]))

        spot_payload = {
            "symbol": symbol,
            "mid": mid,
            "spread": spread,
            "quote_volume": quote_volume,
            "daily_closes": daily_closes,
            "four_hour_closes": four_hour_closes,
            "depth": depth.payload,
            "rules": asdict(rules),
        }
        derivatives_payload = {
            "symbol": symbol,
            "funding_rate": funding_rate,
            "open_interest": open_interest_value,
        }
        reference_payload = {
            "symbol": symbol,
            "coin_usd": coin,
            "tether_usd": tether,
            "reference_usdt": reference_usdt,
            "deviation": deviation,
        }
        news_payload = {"symbol": symbol, "items": recent_news}
        items = (
            self._evidence("spot", book, spot_payload),
            self._evidence("derivatives", open_interest, derivatives_payload),
            self._evidence("reference", reference, reference_payload),
            self._evidence("news", news, news_payload),
        )
        return EvidenceSnapshot(
            symbol=symbol,
            cutoff=iso(cutoff),
            rules=rules,
            items=items,
            binance_mid=mid,
            reference_usdt=reference_usdt,
            spread=spread,
            quote_volume=quote_volume,
            daily_closes=daily_closes,
            four_hour_closes=four_hour_closes,
            funding_rate=funding_rate,
            open_interest=open_interest_value,
            news_count=len(recent_news),
        )

    @staticmethod
    def _closed_klines(
        fetched: Fetched,
        cutoff: datetime,
        label: str,
    ) -> Fetched:
        rows = [row for row in fetched.payload if _utc_from_ms(row[6]) <= cutoff]
        if not rows:
            raise EvidenceError(f"No closed {label} candles at cutoff")
        return Fetched(
            provider=fetched.provider,
            source=fetched.source,
            fetched_at=fetched.fetched_at,
            as_of=_utc_from_ms(rows[-1][6]),
            payload=rows,
        )

    @staticmethod
    def _fresh(
        cutoff: datetime,
        item: Fetched,
        label: str,
        maximum_age: timedelta,
        *,
        use_fetched_at: bool = False,
    ) -> None:
        timestamp = item.fetched_at if use_fetched_at else item.as_of
        if cutoff - _aware(timestamp) > maximum_age:
            raise EvidenceError(f"{label} is stale")

    @staticmethod
    def _rules(payload: dict[str, Any], symbol: str) -> SymbolRules:
        symbols = [item for item in payload.get("symbols", []) if item.get("symbol") == symbol]
        if len(symbols) != 1:
            raise EvidenceError(f"Missing or ambiguous exchange info for {symbol}")
        item = symbols[0]
        if item.get("status") != "TRADING" or not item.get("isSpotTradingAllowed"):
            raise EvidenceError(f"{symbol} is not available for Spot trading")
        if not item.get("ocoAllowed"):
            raise EvidenceError(f"{symbol} does not support OCO")
        if not item.get("otoAllowed"):
            raise EvidenceError(f"{symbol} does not support OTO")
        filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
        try:
            notional = filters.get("NOTIONAL") or filters["MIN_NOTIONAL"]
            return SymbolRules(
                symbol=symbol,
                base_asset=str(item["baseAsset"]),
                quote_asset=str(item["quoteAsset"]),
                tick_size=Decimal(str(filters["PRICE_FILTER"]["tickSize"])),
                step_size=Decimal(str(filters["LOT_SIZE"]["stepSize"])),
                min_qty=Decimal(str(filters["LOT_SIZE"]["minQty"])),
                min_notional=Decimal(str(notional["minNotional"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError(f"Missing Binance symbol filters for {symbol}") from exc

    @staticmethod
    def _evidence(kind: str, fetched: Fetched, payload: dict[str, Any]) -> EvidenceItem:
        normalized = to_jsonable(payload)
        return EvidenceItem.create(
            kind=kind,
            provider=fetched.provider,
            source=fetched.source,
            fetched_at=iso(fetched.fetched_at),
            as_of=iso(fetched.as_of),
            delayed=False,
            stale=False,
            payload=normalized,
        )
