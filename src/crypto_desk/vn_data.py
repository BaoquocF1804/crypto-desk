"""Evidence thị trường cho cổ phiếu HOSE, lấy từ SSI iboard.

Hai quy tắc chi phối module này, cả hai đến từ dữ liệu thật chứ không từ suy luận:

1. Giá điều chỉnh và giá thô không được trộn. Phiên 18/09/2026 của FPT có
   ``close`` 65182.47 trong khi ``floorPrice`` là 69100 — giá điều chỉnh nằm
   *dưới* giá sàn. Lợi nhuận tính từ giá điều chỉnh; mọi so sánh với biên độ,
   mọi đối chiếu, mọi hiển thị giá khớp dùng giá thô.
2. Giá khớp phải khớp giữa hai service SSI. Yếu hơn cross-vendor vì cùng nhà
   cung cấp — giới hạn này được ghi vào báo cáo, không giấu.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import time
from typing import Any, Callable

import httpx

from .data import EvidenceError, Fetched, _aware, _parse_feed
from .domain import EvidenceItem, iso, utcnow

MAX_SOURCE_DEVIATION = Decimal("0.005")
SSI_IBOARD_API = "https://iboard-api.ssi.com.vn"
SSI_IBOARD_QUERY = "https://iboard-query.ssi.com.vn"


def session_mid(session: dict[str, Any]) -> Decimal:
    """Giá khớp của phiên, luôn là giá THÔ.

    ``close`` là giá đã điều chỉnh theo sự kiện quyền và có thể nằm ngoài biên
    độ của chính phiên đó, nên nó không bao giờ được dùng làm giá khớp.
    """
    return Decimal(str(session["closeRaw"]))


def adjusted_closes(rows: list[dict[str, Any]]) -> tuple[Decimal, ...]:
    """Chuỗi giá đóng cửa ĐÃ ĐIỀU CHỈNH, dùng để tính lợi nhuận và alpha."""
    return tuple(Decimal(str(row["close"])) for row in rows)


def assert_sources_agree(matched_price: Decimal, close_raw: Decimal) -> None:
    """Hai service SSI phải báo cùng một giá khớp.

    Cùng nhà cung cấp nên yếu hơn cross-vendor: SSI sai đồng bộ cả hai thì
    không phát hiện được. Bù lại nó bắt được đúng cái bẫy giá điều chỉnh, vì
    nhầm ``close`` thay ``closeRaw`` lệch tới ~9% trên một mã vừa có sự kiện quyền.
    """
    if matched_price <= 0 or close_raw <= 0:
        raise EvidenceError("Giá khớp phải dương")
    deviation = abs(matched_price - close_raw) / close_raw
    if deviation > MAX_SOURCE_DEVIATION:
        raise EvidenceError(
            f"Hai nguồn SSI lệch {deviation:.4%}, vượt ngưỡng {MAX_SOURCE_DEVIATION:.2%}"
        )


def assert_latest_session_closed(latest_session_date: str, cutoff: datetime) -> None:
    """Phiên giao dịch gần nhất phải đã đóng trước hoặc vào ngày cutoff.

    So ngày của phiên với ngày của cutoff: cuối tuần và lễ tết khiến phiên gần
    nhất có thể cách cutoff nhiều ngày mà vẫn là phiên hợp lệ gần nhất.
    """
    session_dt = datetime.strptime(latest_session_date, "%d/%m/%Y").date()
    cutoff_date = _aware(cutoff).date()
    if session_dt > cutoff_date:
        raise EvidenceError(
            f"Phiên giao dịch {latest_session_date} xảy ra sau ngày cutoff {cutoff_date}"
        )


class SSIClient:
    def __init__(
        self,
        news_feeds: tuple[str, ...] = (),
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.news_feeds = news_feeds
        timeout = httpx.Timeout(30.0, connect=10.0, read=30.0)
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
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
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, Any, datetime]:
        url = f"{base}{path}"
        response = self._get(url, params=params, headers=headers)
        fetched_at = _aware(self.now())
        return str(response.url), response.json(), fetched_at

    def charts_history(self, symbol: str, from_epoch: int, to_epoch: int) -> Fetched:
        source, payload, fetched_at = self._json(
            SSI_IBOARD_API,
            "/statistics/charts/history",
            params={
                "resolution": "1D",
                "symbol": symbol,
                "from": str(from_epoch),
                "to": str(to_epoch),
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not data or not isinstance(data, dict) or not data.get("t"):
            raise EvidenceError(f"No charts history for {symbol}")
        as_of = datetime.fromtimestamp(data["t"][-1], tz=UTC)
        return Fetched("ssi", source, fetched_at, as_of, data)

    def stock_info(self, symbol: str, from_date: str, to_date: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SSI_IBOARD_API,
            "/statistics/company/ssmi/stock-info",
            params={"symbol": symbol, "fromDate": from_date, "toDate": to_date},
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not data or not isinstance(data, list):
            raise EvidenceError(f"No stock-info for {symbol}")
        latest = data[0]
        try:
            as_of = datetime.strptime(latest["tradingDate"], "%d/%m/%Y").replace(tzinfo=UTC)
        except (KeyError, ValueError, TypeError):
            as_of = fetched_at
        return Fetched("ssi", source, fetched_at, as_of, data)

    def company_profile(self, symbol: str) -> Fetched:
        source, payload, fetched_at = self._json(
            SSI_IBOARD_API,
            "/statistics/company/ssmi/company-profile",
            params={"symbol": symbol},
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not data:
            raise EvidenceError(f"No company-profile for {symbol}")
        return Fetched("ssi", source, fetched_at, fetched_at, data)

    def board_snapshot(self) -> Fetched:
        source, payload, fetched_at = self._json(
            SSI_IBOARD_QUERY,
            "/stock/exchange/hose",
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not data or not isinstance(data, list):
            raise EvidenceError("No hose board snapshot data")
        return Fetched("ssi", source, fetched_at, fetched_at, data)

    def news(self, cutoff: datetime) -> Fetched:
        cutoff = _aware(cutoff)
        fetched_at = _aware(self.now())
        if self._news_cache:
            cache_time, cached = self._news_cache
            if fetched_at - cache_time < timedelta(minutes=15):
                return cached
        collected: list[tuple[datetime, dict[str, str]]] = []
        sources: list[str] = []
        for url in self.news_feeds:
            try:
                response = self._get(url)
                parsed = _parse_feed(response.text)
            except Exception:
                continue
            sources.append(str(response.url))
            for item in parsed:
                published = datetime.fromisoformat(item["published_at"]).astimezone(UTC)
                if published <= cutoff and cutoff - published <= timedelta(hours=48):
                    collected.append((published, item))
        if not collected:
            raise EvidenceError("Không có tin tức mới trong 48 giờ gần nhất")
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


@dataclass(frozen=True, slots=True)
class VNEvidenceSnapshot:
    symbol: str
    cutoff: str
    items: tuple[EvidenceItem, ...]
    mid: Decimal
    industry: str

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)


class VNEvidenceBuilder:
    def __init__(self, client: SSIClient):
        self.client = client

    def build(self, symbol: str, cutoff: datetime | None = None) -> VNEvidenceSnapshot:
        cutoff_dt = _aware(cutoff or self.client.now())
        cutoff_iso = iso(cutoff_dt)

        # 1. stock_info phiên gần nhất
        from_date = (cutoff_dt - timedelta(days=30)).strftime("%d/%m/%Y")
        to_date = cutoff_dt.strftime("%d/%m/%Y")
        stock_info_fetched = self.client.stock_info(symbol, from_date, to_date)
        sessions = stock_info_fetched.payload
        if not sessions:
            raise EvidenceError(f"Không có dữ liệu phiên cho {symbol}")
        latest_session = sessions[0]
        assert_latest_session_closed(latest_session["tradingDate"], cutoff_dt)

        # 2. charts_history cho daily_closes (giá điều chỉnh)
        from_epoch = int((cutoff_dt - timedelta(days=90)).timestamp())
        to_epoch = int(cutoff_dt.timestamp())
        charts_fetched = self.client.charts_history(symbol, from_epoch, to_epoch)
        charts_data = charts_fetched.payload
        daily_closes = tuple(Decimal(str(c)) for c in charts_data.get("c", []))
        if not daily_closes:
            raise EvidenceError(f"Không có dữ liệu chuỗi giá cho {symbol}")

        # 3. board_snapshot lấy matchedPrice và kiểm chéo
        board_fetched = self.client.board_snapshot()
        board_rows = board_fetched.payload
        board_row = next(
            (row for row in board_rows if row.get("stockSymbol") == symbol),
            None,
        )
        if board_row is None:
            raise EvidenceError(f"Mã {symbol} không tìm thấy trên bảng giá HOSE")
        session_dt_str = datetime.strptime(
            latest_session["tradingDate"], "%d/%m/%Y"
        ).strftime("%Y%m%d")
        board_date_str = str(board_row.get("tradingDate") or "")
        if board_date_str > session_dt_str and board_row.get("priorClosePrice"):
            matched_price = Decimal(str(board_row["priorClosePrice"]))
        else:
            matched_price = Decimal(str(board_row["matchedPrice"]))
        raw_mid = session_mid(latest_session)
        assert_sources_agree(matched_price, raw_mid)

        # 4. company_profile lấy industry
        profile_fetched = self.client.company_profile(symbol)
        industry = profile_fetched.payload.get("industryName")
        if not industry:
            raise EvidenceError(f"Không xác định được ngành cho {symbol}")

        # 5. news RSS
        news_fetched = self.client.news(cutoff_dt)

        # Dựng 4 EvidenceItem bắt buộc
        spot_item = EvidenceItem.create(
            kind="spot",
            provider=stock_info_fetched.provider,
            source=stock_info_fetched.source,
            fetched_at=iso(stock_info_fetched.fetched_at),
            as_of=iso(stock_info_fetched.as_of),
            delayed=False,
            stale=False,
            payload={
                "symbol": symbol,
                "mid": raw_mid,
                "ref_price": Decimal(str(latest_session["refPrice"])),
                "ceiling_price": Decimal(str(latest_session["ceilingPrice"])),
                "floor_price": Decimal(str(latest_session["floorPrice"])),
                "avg_price": Decimal(str(latest_session["avgPrice"])),
                "total_match_vol": Decimal(str(latest_session["totalMatchVol"])),
                "total_match_val": Decimal(str(latest_session["totalMatchVal"])),
                "buy_trades": Decimal(str(latest_session.get("totalBuyTrade") or 0)),
                "sell_trades": Decimal(str(latest_session.get("totalSellTrade") or 0)),
                "daily_closes": daily_closes,
                "close_raw": raw_mid,
                "trading_date": latest_session["tradingDate"],
            },
        )

        flow_item = EvidenceItem.create(
            kind="flow",
            provider=stock_info_fetched.provider,
            source=stock_info_fetched.source,
            fetched_at=iso(stock_info_fetched.fetched_at),
            as_of=iso(stock_info_fetched.as_of),
            delayed=False,
            stale=False,
            payload={
                "symbol": symbol,
                "foreign_buy_vol": Decimal(str(latest_session.get("foreignBuyVolTotal") or 0)),
                "foreign_sell_vol": Decimal(str(latest_session.get("foreignSellVolTotal") or 0)),
                "foreign_room": Decimal(str(latest_session.get("foreignCurrentRoom") or 0)),
                "net_buy_sell_vol": Decimal(str(latest_session.get("netBuySellVol") or 0)),
            },
        )

        news_item = EvidenceItem.create(
            kind="news",
            provider=news_fetched.provider,
            source=news_fetched.source,
            fetched_at=iso(news_fetched.fetched_at),
            as_of=iso(news_fetched.as_of),
            delayed=False,
            stale=False,
            payload={
                "symbol": symbol,
                "items": news_fetched.payload,
            },
        )

        ref_item = EvidenceItem.create(
            kind="reference",
            provider=board_fetched.provider,
            source=board_fetched.source,
            fetched_at=iso(board_fetched.fetched_at),
            as_of=iso(board_fetched.as_of),
            delayed=False,
            stale=False,
            payload={
                "symbol": symbol,
                "matched_price": matched_price,
                "close_raw": raw_mid,
                "deviation": abs(matched_price - raw_mid) / raw_mid,
            },
        )

        return VNEvidenceSnapshot(
            symbol=symbol,
            cutoff=cutoff_iso,
            items=(spot_item, news_item, flow_item, ref_item),
            mid=raw_mid,
            industry=industry,
        )

    def reflection_closes(
        self,
        symbol: str,
        start: str | datetime,
        periods: int = 20,
    ) -> tuple[Decimal, ...]:
        if isinstance(start, str):
            start_dt = datetime.fromisoformat(start).astimezone(UTC)
        else:
            start_dt = _aware(start)
        start_epoch = int(start_dt.timestamp())
        to_epoch = int((start_dt + timedelta(days=periods * 3)).timestamp())
        charts = self.client.charts_history(symbol, start_epoch, to_epoch)
        data = charts.payload
        times = data.get("t", [])
        closes = data.get("c", [])
        pairs = [(t, c) for t, c in zip(times, closes) if t >= start_epoch]
        if len(pairs) < periods:
            raise EvidenceError(
                f"Cần {periods} phiên giao dịch đã đóng cho {symbol} nhưng chỉ có {len(pairs)}"
            )
        return tuple(Decimal(str(c)) for _, c in pairs[:periods])
