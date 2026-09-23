from __future__ import annotations

import hashlib
import os
import re
import time
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
NEWS_CACHE_SECONDS = 300


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
    long_short_ratio: Decimal | None = None
    top_trader_ratio: Decimal | None = None
    taker_buy_sell_ratio: Decimal | None = None
    oi_change_1h_pct: Decimal | None = None
    funding_rate_trend: str = "stable"

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    @property
    def mid(self) -> Decimal:
        """Giá tham chiếu chung cho committee, không phụ thuộc sàn nào."""
        return self.binance_mid


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
        timeout = httpx.Timeout(30.0, connect=10.0, read=30.0)
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.now = now
        self._news_cache: tuple[datetime, Fetched] | None = None

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in (0, 1):
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt:
                    raise EvidenceError(f"HTTP transport error fetching {url}: {exc}") from exc
                time.sleep(0.5)
                continue
            if (response.status_code == 429 or response.status_code >= 500) and not attempt:
                retry_after = response.headers.get("Retry-After")
                delay = min(float(retry_after), 60) if retry_after else 0.5
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response
        if last_error:
            raise EvidenceError(
                f"HTTP transport error fetching {url}: {last_error}"
            ) from last_error
        raise EvidenceError(f"unreachable retry state for {url}")

    def _json(
        self,
        base: str,
        path: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[str, Any, datetime]:
        url = f"{base}{path}"
        response = self._get(url, params=params, headers=headers)
        fetched_at = _aware(self.now())
        return str(response.url), response.json(), fetched_at

    def exchange_info(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/exchangeInfo", params={"symbol": symbol}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> Fetched:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(_aware(start_time).timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(_aware(end_time).timestamp() * 1000)
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC,
            "/api/v3/klines",
            params=params,
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

    def funding_history(self, symbol: str, limit: int = 3) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC, "/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit}
        )
        as_of = _utc_from_ms(payload[-1]["fundingTime"]) if payload else fetched_at
        return Fetched("binance-usdm", source, fetched_at, as_of, payload)

    def open_interest_hist(self, symbol: str, period: str = "1h", limit: int = 3) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC,
            "/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        as_of = _utc_from_ms(payload[-1]["timestamp"]) if payload else fetched_at
        return Fetched("binance-usdm", source, fetched_at, as_of, payload)

    def global_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 1) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC,
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        as_of = _utc_from_ms(payload[-1]["timestamp"]) if payload else fetched_at
        return Fetched("binance-usdm", source, fetched_at, as_of, payload)

    def top_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 1) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC,
            "/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        as_of = _utc_from_ms(payload[-1]["timestamp"]) if payload else fetched_at
        return Fetched("binance-usdm", source, fetched_at, as_of, payload)

    def taker_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 1) -> Fetched:
        source, payload, fetched_at = self._json(
            FUTURES_PUBLIC,
            "/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        as_of = _utc_from_ms(payload[-1]["timestamp"]) if payload else fetched_at
        return Fetched("binance-usdm", source, fetched_at, as_of, payload)

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
        if self._news_cache is not None:
            cached_at, cached = self._news_cache
            if (fetched_at - cached_at).total_seconds() < NEWS_CACHE_SECONDS:
                return cached
        collected: list[tuple[datetime, dict[str, str]]] = []
        sources: list[str] = []
        for url in self.news_feeds:
            try:
                response = self._get(url)
                parsed = _parse_feed(response.text)
            except (httpx.HTTPError, ET.ParseError, ValueError, TypeError, EvidenceError):
                continue
            sources.append(str(response.url))
            for item in parsed:
                published = datetime.fromisoformat(item["published_at"]).astimezone(UTC)
                if published <= fetched_at:
                    collected.append((published, item))
        as_of = max((published for published, _ in collected), default=fetched_at)
        result = Fetched(
            "rss",
            ",".join(sources),
            fetched_at,
            as_of,
            [item for _, item in collected],
        )
        if sources:
            self._news_cache = (fetched_at, result)
        return result


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


def tag_news_relevance(
    items: list[dict[str, str]],
    aliases: tuple[str, ...],
) -> list[dict[str, str]]:
    """Gắn nhãn mỗi tin là ``symbol`` hay ``market``, không bỏ tin nào.

    Lọc cứng sẽ biến một ngày không có tin riêng thành ``EvidenceError`` và kéo
    cả desk về NO_TRADE — đổi một khiếm khuyết lấy một khiếm khuyết tệ hơn. Rổ
    tin chung vẫn là bối cảnh thị trường hợp lệ, chỉ là model cần biết đâu là
    tin của chính mã nó đang xét.

    Biên từ được viết tay thay vì ``\\b`` để ``BTC`` không trúng trong
    ``BTCUSDT`` và ``sui`` không trúng trong ``suit``, nhưng vẫn trúng ``BTC's``.
    """
    patterns = [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
        for alias in aliases
        if alias
    ]
    tagged: list[dict[str, str]] = []
    for item in items:
        title = str(item.get("title") or "")
        relevance = "symbol" if any(pattern.search(title) for pattern in patterns) else "market"
        tagged.append({**item, "relevance": relevance})
    tagged.sort(key=lambda item: item["relevance"] != "symbol")
    return tagged


class EvidenceBuilder:
    def __init__(self, client: Any, coingecko_ids: dict[str, str]):
        self.client = client
        self.coingecko_ids = coingecko_ids
        self._cache: dict[tuple[str, datetime, bool], EvidenceSnapshot] = {}

    def build(
        self,
        symbol: str,
        cutoff: datetime,
        *,
        live: bool = False,
    ) -> EvidenceSnapshot:
        cutoff = _aware(cutoff)
        cache_key = (symbol, cutoff, live)
        if cache_key in self._cache:
            return self._cache[cache_key]
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
        future_cutoff = max(cutoff, *(item.fetched_at for item in fetched)) if live else cutoff
        tolerance = timedelta(seconds=5) if live else timedelta(0)
        for item in fetched:
            if _aware(item.as_of) > future_cutoff + tolerance:
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
        change_24h_pct = Decimal(str(ticker.payload["priceChangePercent"]))

        spot_payload = {
            "symbol": symbol,
            "mid": mid,
            "spread": spread,
            "quote_volume": quote_volume,
            "change_24h_pct": change_24h_pct,
            "daily_closes": daily_closes,
            "four_hour_closes": four_hour_closes,
            "depth": depth.payload,
            "rules": asdict(rules),
        }
        long_short_ratio: Decimal | None = None
        top_trader_ratio: Decimal | None = None
        taker_buy_sell_ratio: Decimal | None = None
        oi_change_1h_pct: Decimal | None = None
        funding_rate_trend: str = "stable"

        try:
            if hasattr(self.client, "funding_history"):
                fh = self.client.funding_history(symbol, limit=3)
                if fh and fh.payload and len(fh.payload) >= 2:
                    rates = [Decimal(str(item["fundingRate"])) for item in fh.payload]
                    if rates[-1] > rates[0] + Decimal("0.00005"):
                        funding_rate_trend = "rising"
                    elif rates[-1] < rates[0] - Decimal("0.00005"):
                        funding_rate_trend = "falling"
                    elif rates[-1] < 0:
                        funding_rate_trend = "negative"
                    else:
                        funding_rate_trend = "stable"
        except Exception:
            pass

        try:
            if hasattr(self.client, "open_interest_hist"):
                oih = self.client.open_interest_hist(symbol, period="1h", limit=2)
                if oih and oih.payload and len(oih.payload) >= 2:
                    oi_prev = Decimal(str(oih.payload[0]["sumOpenInterest"]))
                    oi_curr = Decimal(str(oih.payload[-1]["sumOpenInterest"]))
                    if oi_prev > 0:
                        oi_change_1h_pct = ((oi_curr - oi_prev) / oi_prev) * Decimal("100")
        except Exception:
            pass

        try:
            if hasattr(self.client, "global_long_short_ratio"):
                glsr = self.client.global_long_short_ratio(symbol, period="1h", limit=1)
                if glsr and glsr.payload:
                    long_short_ratio = Decimal(str(glsr.payload[-1]["longShortRatio"]))
        except Exception:
            pass

        try:
            if hasattr(self.client, "top_long_short_ratio"):
                tlsr = self.client.top_long_short_ratio(symbol, period="1h", limit=1)
                if tlsr and tlsr.payload:
                    top_trader_ratio = Decimal(str(tlsr.payload[-1]["longShortRatio"]))
        except Exception:
            pass

        try:
            if hasattr(self.client, "taker_long_short_ratio"):
                tklsr = self.client.taker_long_short_ratio(symbol, period="1h", limit=1)
                if tklsr and tklsr.payload:
                    taker_buy_sell_ratio = Decimal(str(tklsr.payload[-1]["buySellRatio"]))
        except Exception:
            pass

        derivatives_payload = {
            "symbol": symbol,
            "funding_rate": funding_rate,
            "open_interest": open_interest_value,
            "funding_rate_trend": funding_rate_trend,
            "oi_change_1h_pct": oi_change_1h_pct,
            "long_short_ratio": long_short_ratio,
            "top_trader_ratio": top_trader_ratio,
            "taker_buy_sell_ratio": taker_buy_sell_ratio,
        }
        reference_payload = {
            "symbol": symbol,
            "coin_usd": coin,
            "tether_usd": tether,
            "reference_usdt": reference_usdt,
            "deviation": deviation,
        }
        tagged_news = tag_news_relevance(
            recent_news,
            (rules.base_asset, self.coingecko_ids[symbol]),
        )
        news_payload = {
            "symbol": symbol,
            "items": tagged_news,
            "symbol_news_count": sum(
                1 for item in tagged_news if item["relevance"] == "symbol"
            ),
        }
        items = (
            self._evidence("spot", book, spot_payload),
            self._evidence("derivatives", open_interest, derivatives_payload),
            self._evidence("reference", reference, reference_payload),
            self._evidence("news", news, news_payload),
        )
        snapshot = EvidenceSnapshot(
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
            long_short_ratio=long_short_ratio,
            top_trader_ratio=top_trader_ratio,
            taker_buy_sell_ratio=taker_buy_sell_ratio,
            oi_change_1h_pct=oi_change_1h_pct,
            funding_rate_trend=funding_rate_trend,
        )
        self._cache[cache_key] = snapshot
        return snapshot

    def reflection_closes(
        self,
        symbol: str,
        start: datetime,
        periods: int = 20,
    ) -> tuple[Decimal, ...]:
        end = _aware(start) + timedelta(days=periods)
        fetched = self.client.klines(
            symbol,
            "1d",
            periods,
            start_time=start,
            end_time=end - timedelta(milliseconds=1),
        )
        if len(fetched.payload) != periods:
            raise EvidenceError(f"Reflection for {symbol} needs {periods} completed daily candles")
        return tuple(Decimal(str(row[4])) for row in fetched.payload)

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
