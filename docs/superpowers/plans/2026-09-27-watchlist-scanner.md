# Máy quét watchlist bằng model rẻ — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `desk scan` quét top 30 cặp USDT, Gemini Flash chọn tối đa 10 đồng có evidence mạnh vào watchlist nghiên cứu. Committee đầy đủ tự chạy cho 3 đồng điểm cao nhất; 7 đồng còn lại người dùng tự phân tích bằng `desk analyze SYMBOL --research`. Không bao giờ tạo ticket. Run nghiên cứu được chấm điểm thành nhóm riêng và hiện trên dashboard.

**Architecture:** Module mới `scanner.py` gồm ba hàm thuần (`discover_universe`, `compute_features`, `rank_candidates`) và `WatchlistScanner.run`, chỉ đọc qua `PublicDataClient`. `Store` lên schema v4 với bảng `watchlist`. `CryptoDeskService` có thêm `scan()` và `analyze(..., research=True)`. Reflection mang `cohort`, scorecard gộp theo `(benchmark, cohort)`. Dashboard có mục `watchlist` riêng, không đụng `KNOWN_SYMBOLS`.

**Tech Stack:** Python 3.12, `Decimal`, pydantic v2, httpx, SQLite, Typer, pytest; web: TypeScript/React (vinext), `node --test`.

**Spec:** `docs/superpowers/specs/2026-09-27-watchlist-scanner-design.md`

## Global Constraints

- **Trước Task 6, `src/crypto_desk/service.py` và `tests/test_service_cli.py` không được còn thay đổi ngoài plan.** Task 6–9 `git add` nguyên hai file này. Lúc sửa plan (27/09, 13:56) hai file còn phần ticket TTL chưa commit. Commit riêng phần đó trước, hoặc dừng lại hỏi người dùng.
- Python `>=3.12,<3.13`; ruff `line-length = 100`. Chạy: `uv run pytest -q`, `uv run ruff check src tests`.
- Mọi giá và tỉ lệ dùng `Decimal`; mọi file `.py` mở đầu bằng `from __future__ import annotations`.
- Không thêm dependency, cả Python lẫn npm.
- Prompt gửi model viết tiếng Anh. Văn bản cho người dùng viết tiếng Việt có dấu.
- **Run nghiên cứu không bao giờ tạo ticket.** Không đổi `V1_SYMBOLS`, `settings.symbols`, `execution.py`, `risk.py`, `broker.py`.
- Contract dashboard chỉ được **thêm**. `KNOWN_SYMBOLS` và các trường hiện có giữ nguyên.
- Hằng số:
  - `UNIVERSE_SIZE = 30`, `MIN_SCAN_QUOTE_VOLUME = 20 000 000`;
  - `WATCHLIST_MAX_ACTIVE = 10`, và `MAX_PICKS = WATCHLIST_MAX_ACTIVE`;
  - `AUTO_ANALYZE_PICKS = 3`, `WATCHLIST_TTL_DAYS = 7`.
- `WATCHLIST_TTL_DAYS` và `WATCHLIST_MAX_ACTIVE` được thêm vào `config.py` ở Task 5, `AUTO_ANALYZE_PICKS` ở Task 7. Task 4 viết `MAX_PICKS = 3` theo bản plan trước; Task 7 Step 0 đổi thành `WATCHLIST_MAX_ACTIVE`.
- Test web: `cd web && npm run test:unit`.
- Import mới trong test và trong `scanner.py` luôn gộp lên **đầu file** (ruff E402 bắt import giữa file); các đoạn test trong plan chỉ ghi import cạnh code cho dễ đọc.
- Commit message kết thúc bằng `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Baseline: `79f5b46`, **416 passed**, ruff sạch.

## Review Focus

- **Chỉ top 3 được phân tích tự động:** mọi pick vào watchlist, nhưng chỉ 3 pick điểm cao nhất tốn committee. Pick còn lại không có run và không tốn lượt gọi model. Task 7 ghim `committee.calls == 3` khi có 4 pick.
- **CoinGecko map sai ticker trùng tên:** map theo vốn hoá. Lúc viết, `GRAMUSDT` → `the-open-network` là đúng (TON đổi tên, giá lệch 0.06%). Nếu map sai, bộ kiểm tra lệch giá 0.5% sẵn có chặn ngay ở `builder.build`, trước mọi lời gọi model. Task 6 ghim rằng run nghiên cứu lỗi evidence ra NO_TRADE, không ticket, không gọi committee.
- **Nến ngày đang mở lọt vào bảng quét:** `WatchlistScanner._features` phải bỏ nến có close time sau `now`. Test trong Task 4 cho fake trả thêm một nến mở.
- **Hợp đồng delivery không có funding rate:** `/fapi/v1/premiumIndex` trả cả quarterly delivery futures có `lastFundingRate` rỗng hoặc `None`; `WatchlistScanner.run` phải lọc an toàn thay vì ném `InvalidOperation`. Task 4.
- **Watchlist entry hết hạn giữa hai lệnh:** `desk analyze X --research` phải từ chối rõ ràng. Task 6 (service raise) và Task 9 (CLI exit code 2, không traceback).
- **Người dùng thêm một đồng watchlist vào allowlist:** run nghiên cứu cũ vẫn chấm vào nhóm `watchlist`, vì cohort đọc từ `research_only` của decision chứ không từ allowlist hiện tại. Task 8.
- **Payload dashboard vượt giới hạn:**
  - tối đa 10 entry, và trường văn bản bị cắt theo giới hạn của parser; Task 11 test entry thứ 11 bị từ chối;
  - đo ngày 27/09: snapshot thật khoảng 70 KB, 10 entry thêm khoảng 25–35 KB, giới hạn là 128 KB.

---

### Task 1: Các endpoint toàn thị trường, và `coingecko_id` truyền theo lượt build

**Files:**
- Modify: `src/crypto_desk/data.py`: thêm 5 method vào `PublicDataClient` (sau `coingecko`); sửa `EvidenceBuilder.build` (dòng ~470)
- Test: `tests/test_data.py`

**Interfaces:**
- Produces:
  - `PublicDataClient.ticker_24h_all() -> Fetched`
  - `spot_exchange_info_all() -> Fetched`
  - `futures_exchange_info() -> Fetched`
  - `premium_index_all() -> Fetched`
  - `coingecko_markets() -> Fetched`
  - `EvidenceBuilder.build(symbol, cutoff, *, live=False, coingecko_id: str | None = None)`

- [ ] **Step 1: Viết test fail** — thêm vào cuối `tests/test_data.py`:

```python
def test_market_wide_endpoints_use_fixed_public_paths():
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, dict(request.url.params)))
        body = {"symbols": []} if request.url.path.endswith("exchangeInfo") else []
        return httpx.Response(200, json=body)

    client = PublicDataClient(
        (), client=httpx.Client(transport=httpx.MockTransport(handler)), now=lambda: CUTOFF
    )
    client.ticker_24h_all()
    client.spot_exchange_info_all()
    client.futures_exchange_info()
    client.premium_index_all()
    client.coingecko_markets()

    assert seen == [
        ("api.binance.com", "/api/v3/ticker/24hr", {}),
        ("api.binance.com", "/api/v3/exchangeInfo", {"permissions": "SPOT"}),
        ("fapi.binance.com", "/fapi/v1/exchangeInfo", {}),
        ("fapi.binance.com", "/fapi/v1/premiumIndex", {}),
        (
            "api.coingecko.com",
            "/api/v3/coins/markets",
            {"vs_currency": "usd", "order": "market_cap_desc", "per_page": "250", "page": "1"},
        ),
    ]


def test_evidence_builder_takes_a_coingecko_id_for_symbols_outside_its_map():
    builder = EvidenceBuilder(FakePublicClient(), {})

    with pytest.raises(EvidenceError, match="Missing CoinGecko id"):
        builder.build("BTCUSDT", CUTOFF)
    snapshot = builder.build("BTCUSDT", CUTOFF, coingecko_id="bitcoin")

    assert snapshot.reference_usdt == Decimal("100000")
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_data.py -k "market_wide or takes_a_coingecko" -v`
Expected: FAIL (`AttributeError: 'PublicDataClient' object has no attribute 'ticker_24h_all'`; `TypeError: ... unexpected keyword argument 'coingecko_id'`).

- [ ] **Step 3: Thêm method** — trong `PublicDataClient`, ngay sau method `coingecko`:

```python
    def ticker_24h_all(self) -> Fetched:
        source, payload, fetched_at = self._json(SPOT_PUBLIC, "/api/v3/ticker/24hr", params={})
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def spot_exchange_info_all(self) -> Fetched:
        source, payload, fetched_at = self._json(
            SPOT_PUBLIC, "/api/v3/exchangeInfo", params={"permissions": "SPOT"}
        )
        return Fetched("binance", source, fetched_at, fetched_at, payload)

    def futures_exchange_info(self) -> Fetched:
        source, payload, fetched_at = self._json(FUTURES_PUBLIC, "/fapi/v1/exchangeInfo", params={})
        return Fetched("binance-usdm", source, fetched_at, fetched_at, payload)

    def premium_index_all(self) -> Fetched:
        source, payload, fetched_at = self._json(FUTURES_PUBLIC, "/fapi/v1/premiumIndex", params={})
        return Fetched("binance-usdm", source, fetched_at, fetched_at, payload)

    def coingecko_markets(self) -> Fetched:
        headers = {"x-cg-demo-api-key": self.coingecko_key} if self.coingecko_key else None
        source, payload, fetched_at = self._json(
            COINGECKO_PUBLIC,
            "/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1},
            headers=headers,
        )
        return Fetched("coingecko", source, fetched_at, fetched_at, payload)
```

Trong `EvidenceBuilder.build`:
- Thêm keyword `coingecko_id: str | None = None` sau `live: bool = False`.
- Thay khối `if symbol not in self.coingecko_ids: raise EvidenceError(f"Missing CoinGecko id for {symbol}")` bằng:

```python
        coingecko_id = coingecko_id or self.coingecko_ids.get(symbol)
        if coingecko_id is None:
            raise EvidenceError(f"Missing CoinGecko id for {symbol}")
```

- Thay 3 chỗ `self.coingecko_ids[symbol]` còn lại trong `build` bằng `coingecko_id`: lúc gọi `self.client.coingecko(...)`, lúc đọc `reference.payload[...]["usd"]`, và trong `news_aliases(...)`.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_data.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/data.py tests/test_data.py
git commit -m "feat: read market-wide Binance and CoinGecko listings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Khám phá phạm vi quét

**Files:**
- Create: `src/crypto_desk/scanner.py`
- Test: `tests/test_scanner.py` (mới)

**Interfaces:**
- Produces:
  - `Candidate(symbol: str, base_asset: str, coingecko_id: str, quote_volume: Decimal)`, dataclass frozen.
  - `discover_universe(tickers: list[dict], spot_info: dict, futures_info: dict, markets: list[dict], *, exclude: frozenset[str]) -> tuple[Candidate, ...]`
  - Các hằng `UNIVERSE_SIZE`, `MIN_SCAN_QUOTE_VOLUME`, `NON_CRYPTO_BASES`.

- [ ] **Step 1: Viết test fail** — tạo `tests/test_scanner.py`:

```python
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
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_scanner.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'crypto_desk.scanner'`.

- [ ] **Step 3: Tạo `src/crypto_desk/scanner.py`**

```python
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
        "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "BFUSD",
        "XUSD", "RLUSD", "EUR", "EURI", "AEUR", "PAXG", "XAUT",
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
    candidates.sort(key=lambda candidate: candidate.quote_volume, reverse=True)
    return tuple(candidates[:UNIVERSE_SIZE])
```

Lưu ý: `ruff format` sẽ tách `NON_CRYPTO_BASES` thành mỗi phần tử một dòng. Chạy `uv run ruff format src/crypto_desk/scanner.py` sau bước này.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_scanner.py -v && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/scanner.py tests/test_scanner.py
git commit -m "feat: discover the liquid USDT universe the committee can analyse

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Tính chỉ số quét bằng code

**Files:**
- Modify: `src/crypto_desk/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `price_structure`, `_percentile_rank`, `_pct_change`, `_utc_from_ms`, `EvidenceError` từ `data.py`; `ema`, `atr` từ `indicators.py`.
- Produces:
  - `ScanFeatures`, dataclass frozen với các trường theo spec §1.2.
  - `FEATURE_FIELDS: tuple[str, ...]`: mọi trường trừ `symbol`.
  - `compute_features(candidate, daily_rows, oi_rows, ls_rows, taker_rows, funding_rate) -> ScanFeatures`, raise `EvidenceError` khi không đủ dữ liệu.

- [ ] **Step 1: Viết test fail** — thêm vào `tests/test_scanner.py`:

```python
from datetime import UTC, datetime

import pytest

from crypto_desk.data import EvidenceError
from crypto_desk.scanner import FEATURE_FIELDS, Candidate, compute_features

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
    closes = [100 + index for index in range(50)] + [149 - 2 * (index - 49) for index in range(50, 60)]

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
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_scanner.py -v`
Expected: FAIL với `ImportError: cannot import name 'FEATURE_FIELDS'`.

- [ ] **Step 3: Thêm vào `scanner.py`**

Thêm import ở đầu file:

```python
from dataclasses import dataclass, fields

from .data import EvidenceError, _pct_change, _percentile_rank, _utc_from_ms, price_structure
from .indicators import atr, ema
```

Thêm sau `discover_universe`:

```python
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
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_scanner.py -v && uv run ruff check src tests && uv run ruff format --check src/crypto_desk/scanner.py`
Expected: PASS. Nếu ruff báo cần format thì chạy `uv run ruff format src/crypto_desk/scanner.py tests/test_scanner.py`.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/scanner.py tests/test_scanner.py
git commit -m "feat: compute the scan table from closed candles and 24h positioning

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Flash xếp hạng, và `WatchlistScanner.run`

**Files:**
- Modify: `src/crypto_desk/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `BASE_OUTPUT_CONTRACT`, `ProviderError`, `StructuredClient` từ `committee.py`; `REFLECTION_HORIZON_DAYS` từ `config.py`; `to_jsonable`, `utcnow` từ `domain.py`.
- Produces:
  - `ScanPick(symbol, evidence_score, thesis, supporting_fields)` và `ScanRanking(picks, summary)`, pydantic.
  - `ScanError(RuntimeError)`.
  - `rank_candidates(llm, features, *, model: str, thinking: str) -> ScanRanking`
  - `ScanResult(universe, dropped, features, ranking: ScanRanking | None, error: str | None)`
  - `WatchlistScanner(client, llm, *, model, thinking, now=utcnow).run(exclude: frozenset[str]) -> ScanResult`
  - `MAX_PICKS = 3`.

- [ ] **Step 1: Viết test fail** — thêm vào `tests/test_scanner.py`:

```python
from crypto_desk.committee import ProviderError
from crypto_desk.data import Fetched
from crypto_desk.scanner import ScanError, ScanRanking, WatchlistScanner, rank_candidates


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
        [_pick("AUSDT"), _pick("AUSDT"), _pick("AUSDT"), _pick("AUSDT")],
    ],
)
def test_ranking_fails_after_two_invalid_answers(picks):
    llm = FakeScanLLM({"picks": picks, "summary": "Bad."}, {"picks": picks, "summary": "Bad."})

    with pytest.raises(ScanError):
        rank_candidates(llm, _features("AUSDT"), model="flash", thinking="low")


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
        return _fetched([{"id": "near", "symbol": "near"}, {"id": "avalanche-2", "symbol": "avax"}])

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
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_scanner.py -v`
Expected: FAIL với `ImportError: cannot import name 'ScanError'`.

- [ ] **Step 3: Thêm vào `scanner.py`**

Import ở đầu file (gộp với import có sẵn):

```python
from datetime import datetime
from typing import Annotated, Any, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .committee import BASE_OUTPUT_CONTRACT, ProviderError, StructuredClient
from .config import REFLECTION_HORIZON_DAYS
from .data import _aware
from .domain import to_jsonable, utcnow
```

Code, thêm ở cuối file:

```python
MAX_PICKS = 3

FIELD_NOTES = {
    "quote_volume_musd": "24h Spot quote volume in million USDT",
    "change_20d_pct": "percent change over 20 closed daily candles",
    "ema20_gap_pct": "percent distance of the last close above (+) or below (-) daily EMA20",
    "ema20_above_ema50": "daily EMA20 above EMA50",
    "atr1d_pct": "daily ATR14 as percent of the last close",
    "range_position_20d": "last close within the 20-day high-low range, 0 = low, 100 = high",
    "range_position_55d": "last close within the 55-day high-low range, 0 = low, 100 = high",
    "support_distance_atr": "distance down to the nearest daily swing support (or 20-day low), "
    "in daily ATRs",
    "resistance_distance_atr": "distance up to the nearest daily swing resistance, in daily "
    "ATRs; null when no swing high sits above price",
    "long_short_pctile_20d": "rank of the account long/short ratio within ~20 days, 0-100",
    "taker_buy_sell_24h": "24h taker buy volume over sell volume",
    "oi_change_24h_pct": "24h open interest change in percent",
    "funding_rate": "latest perpetual funding rate",
}

SCAN_ROLE = (
    "You are the screening analyst of a long-only Binance Spot research desk. Rank the "
    f"candidates by the strength of evidence for a {REFLECTION_HORIZON_DAYS}-day Spot long "
    f"thesis and return at most {MAX_PICKS} picks; return fewer, or none, when evidence is "
    "weak. Strong evidence: ema20_above_ema50 true with price above EMA20; "
    "resistance_distance_atr well above support_distance_atr, or no resistance at all, so "
    "a stop at least one daily ATR below entry still leaves room for gross R:R of 1.5; "
    "long_short_pctile_20d not high; taker_buy_sell_24h and oi_change_24h_pct supportive. "
    "Penalise extended moves (range_position_20d near 100 with a large ema20_gap_pct). "
    "Every figure is computed by code: name the fields you rely on in supporting_fields and "
    "never introduce numbers absent from the table. evidence_score runs from 0 to 10."
)
SCAN_PROMPT = f"{BASE_OUTPUT_CONTRACT}\n\n{SCAN_ROLE}"


class ScanPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    evidence_score: Annotated[Decimal, Field(ge=0, le=10)]
    thesis: str = Field(min_length=1, max_length=400)
    supporting_fields: list[str] = Field(min_length=1, max_length=6)


class ScanRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    picks: list[ScanPick] = Field(max_length=MAX_PICKS)
    summary: str = Field(min_length=1, max_length=600)


class ScanError(RuntimeError):
    pass


def _validate_ranking(ranking: ScanRanking, candidates: set[str]) -> None:
    seen: set[str] = set()
    for pick in ranking.picks:
        if pick.symbol not in candidates:
            raise ValueError(f"pick {pick.symbol} is not a scanned candidate")
        if pick.symbol in seen:
            raise ValueError(f"pick {pick.symbol} is duplicated")
        seen.add(pick.symbol)
        unknown = sorted(set(pick.supporting_fields) - set(FEATURE_FIELDS))
        if unknown:
            raise ValueError(f"unknown supporting fields {unknown}")


def rank_candidates(
    llm: StructuredClient,
    features: list[ScanFeatures],
    *,
    model: str,
    thinking: str,
) -> ScanRanking:
    payload = {
        "horizon_days": REFLECTION_HORIZON_DAYS,
        "fields": FIELD_NOTES,
        "candidates": [to_jsonable(row) for row in features],
    }
    last_error = ""
    for attempt in (1, 2):
        prompt = (
            SCAN_PROMPT
            if attempt == 1
            else f"{SCAN_PROMPT}\nThe previous ranking failed validation: {last_error}. "
            "Return a corrected full JSON object."
        )
        try:
            raw = llm.generate(
                stage="scan",
                model=model,
                thinking=thinking,
                response_model=ScanRanking,
                system_prompt=prompt,
                payload=payload,
            )
            ranking = raw if isinstance(raw, ScanRanking) else ScanRanking.model_validate(raw)
            _validate_ranking(ranking, {row.symbol for row in features})
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:300]
            continue
        picks = sorted(ranking.picks, key=lambda pick: pick.evidence_score, reverse=True)
        return ranking.model_copy(update={"picks": picks})
    raise ScanError(f"scan ranking rejected: {last_error}")


@dataclass(frozen=True, slots=True)
class ScanResult:
    universe: tuple[Candidate, ...]
    dropped: dict[str, str]
    features: tuple[ScanFeatures, ...]
    ranking: ScanRanking | None
    error: str | None


_FETCH_ERRORS = (
    EvidenceError,
    httpx.HTTPError,
    KeyError,
    IndexError,
    TypeError,
    ValueError,
    ArithmeticError,
)


class WatchlistScanner:
    def __init__(
        self,
        client: Any,
        llm: StructuredClient,
        *,
        model: str,
        thinking: str,
        now: Callable[[], datetime] = utcnow,
    ):
        self.client = client
        self.llm = llm
        self.model = model
        self.thinking = thinking
        self.now = now

    def run(self, exclude: frozenset[str]) -> ScanResult:
        try:
            universe = discover_universe(
                self.client.ticker_24h_all().payload,
                self.client.spot_exchange_info_all().payload,
                self.client.futures_exchange_info().payload,
                self.client.coingecko_markets().payload,
                exclude=exclude,
            )
            funding: dict[str, Decimal] = {}
            for row in self.client.premium_index_all().payload:
                rate = row.get("lastFundingRate")
                if rate not in (None, ""):
                    try:
                        funding[str(row["symbol"])] = Decimal(str(rate))
                    except (InvalidOperation, TypeError):
                        pass
        except _FETCH_ERRORS as exc:
            return ScanResult((), {}, (), None, f"universe:{type(exc).__name__}: {exc}"[:300])
        cutoff = _aware(self.now())
        features: list[ScanFeatures] = []
        dropped: dict[str, str] = {}
        for candidate in universe:
            try:
                features.append(self._features(candidate, funding, cutoff))
            except _FETCH_ERRORS as exc:
                dropped[candidate.symbol] = f"{type(exc).__name__}: {exc}"[:200]
        if not features:
            empty = ScanRanking(picks=[], summary="No candidate survived feature extraction.")
            return ScanResult(universe, dropped, (), empty, None)
        try:
            ranking = rank_candidates(self.llm, features, model=self.model, thinking=self.thinking)
        except ScanError as exc:
            return ScanResult(universe, dropped, tuple(features), None, str(exc))
        except ProviderError as exc:
            return ScanResult(universe, dropped, tuple(features), None, str(exc))
        return ScanResult(universe, dropped, tuple(features), ranking, None)

    def _features(
        self, candidate: Candidate, funding: dict[str, Decimal], cutoff: datetime
    ) -> ScanFeatures:
        daily = self.client.klines(candidate.symbol, "1d", 121).payload
        closed = [row for row in daily if _utc_from_ms(row[6]) <= cutoff]
        return compute_features(
            candidate,
            closed,
            self.client.open_interest_hist(candidate.symbol, period="1h", limit=25).payload,
            self.client.global_long_short_ratio(candidate.symbol, period="1h", limit=500).payload,
            self.client.taker_long_short_ratio(candidate.symbol, period="1h", limit=24).payload,
            funding.get(candidate.symbol),
        )
```

`str(ProviderError("rate_limit"))` là `"provider:rate_limit"`, xem `ProviderError.__init__` trong committee.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_scanner.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/scanner.py tests/test_scanner.py
git commit -m "feat: rank the scan table with one cheap-model call

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Bảng `watchlist` (Store schema v4)

**Files:**
- Modify: `src/crypto_desk/store.py`: migration sau khối `if version < 3:`, thêm method sau `list_reflections`
- Modify: `src/crypto_desk/config.py`: hằng số
- Test: `tests/test_domain_store.py`

**Interfaces:**
- Produces:
  - `WATCHLIST_TTL_DAYS = 7` và `WATCHLIST_MAX_ACTIVE = 10` trong `config.py`.
  - `Store.upsert_watchlist(symbol: str, coingecko_id: str, scan_id: str, picked_at: str, expires_at: str, payload: dict) -> None`
  - `Store.set_watchlist_run(symbol: str, run_id: str) -> None`
  - `Store.watchlist_entry(symbol: str) -> dict | None`
  - `Store.active_watchlist(now: str, limit: int = WATCHLIST_MAX_ACTIVE) -> list[dict]`
  - `Store.watchlist_symbols() -> tuple[str, ...]`
  - Mỗi dict có dạng `{symbol, coingecko_id, first_added_at, last_picked_at, expires_at, scan_id, payload}`, trong đó `payload` đã decode JSON.

- [ ] **Step 1: Viết test fail** — trong `tests/test_domain_store.py`:
  - Đổi `test_store_uses_schema_version_three` thành `test_store_uses_schema_version_four`, assert `== 4`.
  - Trong `test_existing_v2_database_migrates_to_v3`, đổi `assert migrated.schema_version() == 3` thành `assert migrated.schema_version() == 4` (vì Store mở lại sẽ chạy qua mọi migration lên phiên bản mới nhất).
  - Thêm vào cuối file:

```python
def _watch(store: Store, symbol: str, picked: str, expires: str, score: str = "7") -> None:
    store.upsert_watchlist(
        symbol, symbol.lower(), f"scan-{picked}", picked, expires, {"evidence_score": score}
    )


def test_watchlist_upsert_refreshes_a_pick_but_keeps_its_first_date(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    _watch(store, "NEARUSDT", "2026-09-20T00:00:00+00:00", "2026-09-27T00:00:00+00:00", "6")
    _watch(store, "NEARUSDT", "2026-09-25T00:00:00+00:00", "2026-10-02T00:00:00+00:00", "8")

    entry = store.watchlist_entry("NEARUSDT")

    assert entry["first_added_at"] == "2026-09-20T00:00:00+00:00"
    assert entry["last_picked_at"] == "2026-09-25T00:00:00+00:00"
    assert entry["payload"] == {"evidence_score": "8"}


def test_active_watchlist_skips_expired_entries_newest_first(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    _watch(store, "OLDUSDT", "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00")
    _watch(store, "AUSDT", "2026-09-24T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    _watch(store, "BUSDT", "2026-09-26T00:00:00+00:00", "2026-10-03T00:00:00+00:00")

    active = store.active_watchlist("2026-09-27T00:00:00+00:00")

    assert [entry["symbol"] for entry in active] == ["BUSDT", "AUSDT"]
    assert store.active_watchlist("2026-09-27T00:00:00+00:00", limit=1)[0]["symbol"] == "BUSDT"
    # Expired rows stay: their runs still need grading.
    assert store.watchlist_symbols() == ("AUSDT", "BUSDT", "OLDUSDT")


def test_watchlist_records_the_latest_research_run(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    _watch(store, "NEARUSDT", "2026-09-26T00:00:00+00:00", "2026-10-03T00:00:00+00:00")

    store.set_watchlist_run("NEARUSDT", "run-9")

    assert store.watchlist_entry("NEARUSDT")["payload"]["last_run_id"] == "run-9"


def test_a_version_three_database_migrates_to_four(tmp_path: Path):
    path = tmp_path / "crypto.db"
    store = Store(path)
    store.db.execute("DROP TABLE watchlist")
    store.db.execute("UPDATE schema_meta SET version=3")
    store.db.commit()
    store.close()

    migrated = Store(path)

    assert migrated.schema_version() == 4
    assert migrated.active_watchlist("2026-09-27T00:00:00+00:00") == []
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_domain_store.py -v`
Expected: FAIL, vì `schema_version() == 3` (cả test version mới lẫn test migration) và thiếu method `upsert_watchlist`.

- [ ] **Step 3: Sửa code**

Trong `config.py`, ngay dưới dòng `REFLECTION_HORIZON_DAYS = 20`:

```python
# Research watchlist from the cheap-model scanner: an entry lives a week unless re-picked.
WATCHLIST_TTL_DAYS = 7
WATCHLIST_MAX_ACTIVE = 10
```

Trong `Store.__init__`, ngay sau dòng `self.db.execute("UPDATE schema_meta SET version=3")` (vẫn nằm trong khối `if version < 3:`), thêm `version = 3`. Rồi thêm khối mới trước `self.db.commit()`:

```python
        if version < 4:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                  symbol TEXT PRIMARY KEY,
                  coingecko_id TEXT NOT NULL,
                  first_added_at TEXT NOT NULL,
                  last_picked_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  scan_id TEXT NOT NULL,
                  payload TEXT NOT NULL
                )
                """
            )
            self.db.execute("UPDATE schema_meta SET version=4")
```

Thêm import `from .config import WATCHLIST_MAX_ACTIVE` ở đầu `store.py` nếu chưa có import từ config. Thêm các method sau `list_reflections`:

```python
    def upsert_watchlist(
        self,
        symbol: str,
        coingecko_id: str,
        scan_id: str,
        picked_at: str,
        expires_at: str,
        payload: dict[str, Any],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO watchlist(
              symbol, coingecko_id, first_added_at, last_picked_at, expires_at, scan_id, payload
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
              coingecko_id=excluded.coingecko_id,
              last_picked_at=excluded.last_picked_at,
              expires_at=excluded.expires_at,
              scan_id=excluded.scan_id,
              payload=excluded.payload
            """,
            (symbol, coingecko_id, picked_at, picked_at, expires_at, scan_id, _json(payload)),
        )
        self.db.commit()

    def set_watchlist_run(self, symbol: str, run_id: str) -> None:
        entry = self.watchlist_entry(symbol)
        if entry is None:
            return
        self.db.execute(
            "UPDATE watchlist SET payload=? WHERE symbol=?",
            (_json({**entry["payload"], "last_run_id": run_id}), symbol),
        )
        self.db.commit()

    def watchlist_entry(self, symbol: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
        return None if row is None else self._watchlist_row(row)

    def active_watchlist(self, now: str, limit: int = WATCHLIST_MAX_ACTIVE) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM watchlist WHERE expires_at > ? ORDER BY last_picked_at DESC LIMIT ?",
            (now, limit),
        ).fetchall()
        return [self._watchlist_row(row) for row in rows]

    def watchlist_symbols(self) -> tuple[str, ...]:
        rows = self.db.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
        return tuple(row["symbol"] for row in rows)

    @staticmethod
    def _watchlist_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "symbol": row["symbol"],
            "coingecko_id": row["coingecko_id"],
            "first_added_at": row["first_added_at"],
            "last_picked_at": row["last_picked_at"],
            "expires_at": row["expires_at"],
            "scan_id": row["scan_id"],
            "payload": json.loads(row["payload"]),
        }
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_domain_store.py -v && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/store.py src/crypto_desk/config.py tests/test_domain_store.py
git commit -m "feat: persist the research watchlist in schema v4

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Phân tích chế độ nghiên cứu, không bao giờ có ticket

**Files:**
- Modify: `src/crypto_desk/domain.py`: `ResearchDecision`
- Modify: `src/crypto_desk/service.py`: `analyze`, `_create_ticket`, `_markdown_report`
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Consumes: `Store.watchlist_entry` (Task 5); `EvidenceBuilder.build(..., coingecko_id=)` (Task 1).
- Produces:
  - `ResearchDecision.research_only: bool = False`
  - `CryptoDeskService.analyze(symbol, cutoff=None, *, research: bool = False) -> AnalysisRun`
  - Lý do chặn ticket `"research_only"`.

- [ ] **Step 1: Viết test fail**

Trong `tests/test_service_cli.py`, sửa hai fake cho phù hợp:
- `FakeBuilder.build`: thêm keyword `coingecko_id=None`, ghi lại `self.coingecko_ids_seen = getattr(self, "coingecko_ids_seen", []) + [coingecko_id]`.
- `FakeCommittee.run`: thêm dòng `self.last_position_quantity = position_quantity` ngay sau `self.calls += 1`.

Thêm vào cuối file:

```python
def _watch(store: Store, symbol: str = "NEARUSDT", *, expires: datetime | None = None) -> None:
    store.upsert_watchlist(
        symbol,
        "near",
        "scan-1",
        NOW.isoformat(),
        (expires or NOW + timedelta(days=7)).isoformat(),
        {"evidence_score": "8", "thesis": "Trend holds.", "supporting_fields": ["change_20d_pct"]},
    )


def test_research_analysis_of_a_watchlisted_coin_never_mints_a_ticket(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.save_snapshot(
        PortfolioSnapshot(
            environment="testnet",
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("10000"),
            positions=(),
            open_orders=(),
            as_of=NOW.isoformat(),
        )
    )
    _watch(store)
    builder, committee = FakeBuilder(), FakeCommittee()
    service = CryptoDeskService(
        settings, store, evidence_builder=builder, committee=committee, now=lambda: NOW
    )

    result = service.analyze("NEARUSDT", research=True)

    assert result.decision.action == "ACCUMULATE"
    assert result.decision.research_only is True
    assert result.ticket_id is None
    blocked = json.loads((result.report_dir / "ticket_blocked.json").read_text(encoding="utf-8"))
    assert blocked["reason"] == "research_only"
    assert builder.coingecko_ids_seen == ["near"]
    assert committee.last_position_quantity == Decimal("0")
    assert "NGHIÊN CỨU — không giao dịch" in (result.report_dir / "report.md").read_text(
        encoding="utf-8"
    )


def test_research_analysis_needs_an_active_watchlist_entry(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    _watch(store, "OLDUSDT", expires=NOW - timedelta(minutes=1))
    service = CryptoDeskService(
        settings, store, evidence_builder=FakeBuilder(), committee=FakeCommittee(), now=lambda: NOW
    )

    for symbol in ("NEARUSDT", "OLDUSDT"):
        with pytest.raises(ValueError, match="active watchlist entry"):
            service.analyze(symbol, research=True)


def test_an_allowlisted_symbol_cannot_be_analysed_as_research(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    _watch(store, "BTCUSDT")
    service = CryptoDeskService(
        settings, store, evidence_builder=FakeBuilder(), committee=FakeCommittee(), now=lambda: NOW
    )

    with pytest.raises(ValueError, match="active watchlist entry"):
        service.analyze("BTCUSDT", research=True)


def test_ticket_creation_refuses_a_research_decision_even_for_an_allowlisted_symbol(tmp_path):
    settings = make_settings(tmp_path)
    service = CryptoDeskService(settings, Store(settings.database), now=lambda: NOW)
    decision = replace(FakeCommittee().run(make_snapshot("BTCUSDT")).decision, research_only=True)

    assert service._create_ticket(decision, make_snapshot("BTCUSDT"), NOW) == (
        None,
        "research_only",
    )


def test_bad_evidence_on_a_research_coin_is_no_trade_before_any_model_call(tmp_path: Path):
    # A wrong CoinGecko map or a stale feed fails in builder.build, before the committee.
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    _watch(store)
    committee = FakeCommittee()
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(error="Cross-source price deviation 1.20% exceeds 0.5%"),
        committee=committee,
        now=lambda: NOW,
    )

    result = service.analyze("NEARUSDT", research=True)

    assert (result.decision.action, result.decision.decided) == ("NO_TRADE", False)
    assert result.decision.research_only is True
    assert result.ticket_id is None
    assert committee.calls == 0
```

Nếu file test chưa import `replace` và `pytest` thì thêm `from dataclasses import replace` và `import pytest`.

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_service_cli.py -k "research" -v`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'research'`; `research_only` không phải field).

- [ ] **Step 3: Sửa code**

`domain.py`, trong `ResearchDecision`, thêm sau `decided: bool = True`:

```python
    # From the watchlist scanner: analysed for research, never tradable.
    research_only: bool = False
```

`service.py`:

(a) Chữ ký và phần đầu của `analyze`: thay

```python
    def analyze(
        self,
        symbol: str,
        cutoff: datetime | None = None,
    ) -> AnalysisRun:
        symbol = symbol.upper()
        if symbol not in self.settings.symbols:
            raise ValueError("Symbol is outside the configured allowlist")
```

bằng

```python
    def analyze(
        self,
        symbol: str,
        cutoff: datetime | None = None,
        *,
        research: bool = False,
    ) -> AnalysisRun:
        symbol = symbol.upper()
        build_kwargs: dict[str, Any] = {}
        if research:
            entry = self._active_watchlist_entry(symbol)
            if entry is None:
                raise ValueError(
                    "Research analysis needs an active watchlist entry outside the allowlist"
                )
            build_kwargs["coingecko_id"] = entry["coingecko_id"]
        elif symbol not in self.settings.symbols:
            raise ValueError("Symbol is outside the configured allowlist")
```

(b) Đổi `snapshot = builder.build(symbol, effective_cutoff, live=cutoff is None)` thành `snapshot = builder.build(symbol, effective_cutoff, live=cutoff is None, **build_kwargs)`.

(c) Ngay trước `committee = self._require_committee()` trong nhánh `else:`, thêm:

```python
            # A research coin is never held by the desk, whatever the account shows.
            position_quantity = Decimal("0") if research else self._position_quantity(symbol)
```

rồi thay cả hai chỗ `position_quantity=self._position_quantity(symbol),` trong nhánh đó bằng `position_quantity=position_quantity,`.

(d) Ngay trước dòng `run_id = str(uuid.uuid4())`, thêm:

```python
        if research:
            decision = replace(decision, research_only=True)
```

(e) Thêm method, đặt cạnh `_position_quantity`:

```python
    def _active_watchlist_entry(self, symbol: str) -> dict[str, Any] | None:
        entry = self.store.watchlist_entry(symbol)
        if entry is None or symbol in self.settings.symbols:
            return None
        return entry if entry["expires_at"] > iso(self._now()) else None
```

(f) Trong `_create_ticket`, ngay sau dòng `return None, None` đầu tiên (nhánh action không actionable hoặc evidence None):

```python
        # The watchlist is research only: refuse here even if a caller forgot research=True.
        if decision.research_only or decision.symbol not in self.settings.symbols:
            return None, "research_only"
```

(g) Trong `_markdown_report`, ngay sau khi list `lines = [...]` đầu tiên được tạo xong:

```python
        if decision.research_only:
            lines[1:1] = [
                "",
                "> **NGHIÊN CỨU — không giao dịch.** Đồng này đến từ watchlist của máy quét; "
                "hệ thống không tạo ticket cho nó.",
            ]
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_service_cli.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/domain.py src/crypto_desk/service.py tests/test_service_cli.py
git commit -m "feat: analyse watchlist coins as research that can never mint a ticket

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `CryptoDeskService.scan()`: 10 pick vào watchlist, top 3 tự phân tích

**Files:**
- Modify: `src/crypto_desk/scanner.py`, `src/crypto_desk/store.py` (Step 0)
- Modify: `src/crypto_desk/config.py`, `src/crypto_desk/service.py`
- Test: `tests/test_scanner.py`, `tests/test_domain_store.py` (Step 0), `tests/test_service_cli.py`

**Interfaces:**
- Consumes:
  - `WatchlistScanner.run(exclude) -> ScanResult` (Task 4)
  - `Store.upsert_watchlist` (Task 5), `Store.latest_run` (có sẵn)
  - `analyze(..., research=True)` (Task 6)
- Produces:
  - `MAX_PICKS = WATCHLIST_MAX_ACTIVE` (10) trong `scanner.py`
  - `AUTO_ANALYZE_PICKS = 3` trong `config.py`
  - `CryptoDeskService(..., scanner: Any | None = None)`
  - `CryptoDeskService.scan() -> dict[str, Any]` gồm: `status` (`COMPLETED` hoặc `FAILED`), `scan_id`, `universe_size`, `dropped`, `picks: [{symbol, evidence_score, thesis, run_id, action}]`, `summary`, `error`, `artifact`. `run_id` và `action` là `None` với pick chờ phân tích thủ công.

- [ ] **Step 0: Bù cho thiết kế 10 pick**

Task 4–5 đã làm theo bản plan trước (3 pick). Làm từng mục dưới đây; mục nào code đã đúng thì bỏ qua.

(a) `scanner.py`: gộp `WATCHLIST_MAX_ACTIVE` vào import từ `.config`, rồi thay `MAX_PICKS = 3` bằng:

```python
# A scan never picks more than the watchlist can show, so its picks always fit on screen.
MAX_PICKS = WATCHLIST_MAX_ACTIVE
```

Trong `SCAN_ROLE`, thay hai dòng

```python
    f"thesis and return at most {MAX_PICKS} picks; return fewer, or none, when evidence is "
    "weak. Strong evidence: ema20_above_ema50 true with price above EMA20; "
```

bằng

```python
    f"thesis and return at most {MAX_PICKS} picks, strongest first; return fewer, or none, "
    "when evidence is weak. Strong evidence: ema20_above_ema50 true with price above EMA20; "
```

(b) `tests/test_scanner.py`:
- Gộp `MAX_PICKS` vào import từ `crypto_desk.scanner`.
- Trong parametrize của `test_ranking_fails_after_two_invalid_answers`, bỏ ca 4 lần `_pick("AUSDT")`. Với 10 pick nó chỉ còn là ca trùng, đã có ca riêng.
- Thêm:

```python
def test_ranking_takes_up_to_max_picks():
    symbols = [f"C{index:02d}USDT" for index in range(MAX_PICKS + 1)]
    too_many = {"picks": [_pick(symbol) for symbol in symbols], "summary": "Eleven."}
    llm = FakeScanLLM(too_many, {**too_many, "picks": too_many["picks"][:MAX_PICKS]})

    ranking = rank_candidates(llm, _features(*symbols), model="flash", thinking="low")

    assert MAX_PICKS == 10
    assert len(ranking.picks) == MAX_PICKS
    assert "failed validation" in llm.calls[1]["system_prompt"]
```

(c) `store.py`: một lượt quét ghi mọi pick với cùng `last_picked_at`, nên `ORDER BY last_picked_at` không xếp được chúng. Giữ nguyên chữ ký `active_watchlist` mà Task 5 đã làm, thay thân hàm bằng:

```python
        rows = self.db.execute("SELECT * FROM watchlist WHERE expires_at > ?", (now,)).fetchall()
        entries = [self._watchlist_row(row) for row in rows]
        # One scan writes every pick with the same last_picked_at; the score orders them.
        entries.sort(
            key=lambda entry: (
                entry["last_picked_at"],
                Decimal(str(entry["payload"].get("evidence_score", "0"))),
            ),
            reverse=True,
        )
        return entries[:limit]
```

Thêm vào `tests/test_domain_store.py`, dùng helper `_watch` của Task 5:

```python
def test_picks_of_one_scan_are_listed_by_score(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    _watch(store, "OLDUSDT", "2026-09-25T00:00:00+00:00", "2026-10-02T00:00:00+00:00", "10")
    _watch(store, "NINEUSDT", "2026-09-26T00:00:00+00:00", "2026-10-03T00:00:00+00:00", "9")
    _watch(store, "TENUSDT", "2026-09-26T00:00:00+00:00", "2026-10-03T00:00:00+00:00", "10")

    active = store.active_watchlist("2026-09-27T00:00:00+00:00", limit=2)

    # Newest scan first, then score as a number: as text "9" would beat "10".
    assert [entry["symbol"] for entry in active] == ["TENUSDT", "NINEUSDT"]
```

(d) Nếu Task 5 đã thêm `Store.set_watchlist_run` và test `test_watchlist_records_the_latest_research_run`, xoá cả hai. Run gần nhất của một đồng đọc từ `research_runs` (`latest_run`, `latest_valid_run`). Một `last_run_id` lưu trong watchlist sẽ lệch ngay khi người dùng phân tích thủ công.

Chạy và commit riêng:

```bash
uv run pytest tests/test_scanner.py tests/test_domain_store.py -q && uv run pytest -q && uv run ruff check src tests
git add src/crypto_desk/scanner.py src/crypto_desk/store.py tests/test_scanner.py tests/test_domain_store.py
git commit -m "feat: let one scan fill the ten-entry watchlist, best score first

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 1: Viết test fail** — thêm vào cuối `tests/test_service_cli.py`. Ranking 4 pick chỉ hợp lệ sau Step 0:

```python
from crypto_desk.scanner import Candidate, ScanPick, ScanRanking, ScanResult


class FakeScanner:
    def __init__(self, result: ScanResult):
        self.result = result
        self.excluded: frozenset[str] | None = None

    def run(self, exclude: frozenset[str]) -> ScanResult:
        self.excluded = exclude
        return self.result


def _scan_result(*symbols: str, error: str | None = None) -> ScanResult:
    universe = tuple(
        Candidate(symbol, symbol[:-4], symbol[:-4].lower(), Decimal("160000000"))
        for symbol in symbols
    )
    ranking = (
        None
        if error
        else ScanRanking(
            picks=[
                ScanPick(
                    symbol=symbol,
                    evidence_score=Decimal(9 - index),
                    thesis="Trend holds above support.",
                    supporting_fields=["support_distance_atr"],
                )
                for index, symbol in enumerate(symbols)
            ],
            summary="Ranked picks.",
        )
    )
    return ScanResult(universe, {"ZECUSDT": "EvidenceError: no klines"}, (), ranking, error)


def test_scan_watchlists_every_pick_but_analyses_only_the_top_three(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    scanner = FakeScanner(_scan_result("NEARUSDT", "AVAXUSDT", "LINKUSDT", "DOGEUSDT"))
    committee = FakeCommittee()
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=committee,
        scanner=scanner,
        now=lambda: NOW,
    )

    result = service.scan()

    assert result["status"] == "COMPLETED"
    assert scanner.excluded == frozenset(settings.symbols)
    assert result["dropped"] == {"ZECUSDT": "EvidenceError: no klines"}
    assert committee.calls == 3
    assert [(pick["symbol"], pick["action"]) for pick in result["picks"]] == [
        ("NEARUSDT", "ACCUMULATE"),
        ("AVAXUSDT", "ACCUMULATE"),
        ("LINKUSDT", "ACCUMULATE"),
        ("DOGEUSDT", None),
    ]
    # The fourth pick waits for a manual run: on the watchlist, never analysed.
    assert result["picks"][3]["run_id"] is None
    assert store.watchlist_entry("DOGEUSDT") is not None
    assert store.latest_run("DOGEUSDT") is None
    near = store.watchlist_entry("NEARUSDT")
    assert near["coingecko_id"] == "near"
    assert near["expires_at"] == (NOW + timedelta(days=7)).isoformat()
    assert store.latest_run("NEARUSDT")["id"] == result["picks"][0]["run_id"]
    artifact = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
    assert len(artifact["ranking"]["picks"]) == 4
    assert artifact["run_ids"] == [pick["run_id"] for pick in result["picks"][:3]]


def test_a_failed_scan_leaves_the_watchlist_unchanged(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        scanner=FakeScanner(_scan_result("NEARUSDT", error="provider:rate_limit")),
        now=lambda: NOW,
    )

    result = service.scan()

    assert result["status"] == "FAILED"
    assert result["error"] == "provider:rate_limit"
    assert store.watchlist_symbols() == ()
    assert json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))["error"] == (
        "provider:rate_limit"
    )
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_service_cli.py -k "scan" -v`
Expected: FAIL với `TypeError: ... unexpected keyword argument 'scanner'`.

- [ ] **Step 3: Sửa code**

Trong `CryptoDeskService.__init__`: thêm keyword `scanner: Any | None = None` sau `execution`, và gán `self.scanner = scanner`.

`config.py`, ngay dưới `WATCHLIST_MAX_ACTIVE = 10`:

```python
# Only the best picks of a scan get a full committee; the rest wait for a manual run.
AUTO_ANALYZE_PICKS = 3
```

Import trong `service.py`:

```python
from .config import (
    AUTO_ANALYZE_PICKS,
    BENCHMARK_SYMBOL,
    REFLECTION_HORIZON_DAYS,
    WATCHLIST_TTL_DAYS,
    Settings,
)
```

Thêm method sau `screen`:

```python
    def scan(self) -> dict[str, Any]:
        """Đưa mọi pick của máy quét vào watchlist; chỉ các pick đầu được tự phân tích."""
        if self.scanner is None:
            raise ValueError("Watchlist scanner is required for scan")
        started = self._aware(self._now())
        scan_id = str(uuid.uuid4())
        result = self.scanner.run(frozenset(self.settings.symbols))
        picks: list[dict[str, Any]] = []
        if result.ranking is not None:
            by_symbol = {candidate.symbol: candidate for candidate in result.universe}
            expires = iso(started + timedelta(days=WATCHLIST_TTL_DAYS))
            for rank, pick in enumerate(result.ranking.picks):
                self.store.upsert_watchlist(
                    pick.symbol,
                    by_symbol[pick.symbol].coingecko_id,
                    scan_id,
                    iso(started),
                    expires,
                    {
                        "evidence_score": str(pick.evidence_score),
                        "thesis": pick.thesis,
                        "supporting_fields": list(pick.supporting_fields),
                    },
                )
                row: dict[str, Any] = {
                    "symbol": pick.symbol,
                    "evidence_score": pick.evidence_score,
                    "thesis": pick.thesis,
                    "run_id": None,
                    "action": None,
                }
                # Picks come best first. Only the top ones spend a committee; the rest wait
                # for `desk analyze SYMBOL --research`.
                if rank < AUTO_ANALYZE_PICKS:
                    run = self.analyze(pick.symbol, research=True)
                    row.update(run_id=run.run_id, action=run.decision.action)
                picks.append(row)
        artifact_path = (
            self.settings.artifacts / "scans" / started.date().isoformat() / f"{scan_id}.json"
        )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(
            artifact_path,
            {
                "scan_id": scan_id,
                "started_at": iso(started),
                "universe": result.universe,
                "dropped": result.dropped,
                "features": result.features,
                "ranking": None if result.ranking is None else result.ranking.model_dump(mode="json"),
                "error": result.error,
                "run_ids": [pick["run_id"] for pick in picks if pick["run_id"]],
            },
        )
        return {
            "status": "FAILED" if result.error else "COMPLETED",
            "scan_id": scan_id,
            "universe_size": len(result.universe),
            "dropped": result.dropped,
            "picks": picks,
            "summary": None if result.ranking is None else result.ranking.summary,
            "error": result.error,
            "artifact": str(artifact_path),
        }
```

`_write_json` gọi `to_jsonable` nên dataclass và `Decimal` đều serialize được. Xem `_write_json` để chắc rằng nó chuẩn hoá payload.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_service_cli.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/config.py src/crypto_desk/service.py tests/test_service_cli.py
git commit -m "feat: watchlist every scan pick and analyse the top three as research

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Chấm điểm watchlist thành nhóm riêng

**Files:**
- Modify: `src/crypto_desk/service.py`: `save_reflection`, `refresh_reflections`
- Modify: `src/crypto_desk/scorecard.py`
- Test: `tests/test_service_cli.py`, `tests/test_scorecard.py`

**Interfaces:**
- Consumes: `Store.watchlist_symbols()` (Task 5); `ResearchDecision.research_only` (Task 6).
- Produces:
  - `save_reflection(..., cohort: str | None = None)` ghi `payload["cohort"]`.
  - `BenchmarkGroup.cohort: str`, với giá trị `"allowlist"` hoặc `"watchlist"`.

- [ ] **Step 0: Bù test của Task 6**

Nếu `tests/test_service_cli.py` chưa có `test_bad_evidence_on_a_research_coin_is_no_trade_before_any_model_call`, chép nó từ Step 1 của Task 6 vào cuối file. Code không cần đổi, test phải pass ngay. Nó đi chung commit của Task 8.

- [ ] **Step 1: Viết test fail**

`tests/test_service_cli.py`, thêm vào cuối file:

```python
def test_research_runs_are_graded_as_the_watchlist_cohort(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    _watch(store, "NEARUSDT", expires=NOW - timedelta(days=1))  # expired, still graded
    for run_id, symbol, research in (("near-run", "NEARUSDT", True), ("btc-run", "BTCUSDT", False)):
        report_dir = tmp_path / run_id
        report_dir.mkdir()
        (report_dir / "evidence.json").write_text(
            json.dumps({"binance_mid": "100"}), encoding="utf-8"
        )
        store.save_run(
            run_id,
            (NOW - timedelta(days=21)).isoformat(),
            replace(_decided_hold(symbol), research_only=research),
            report_dir,
        )
    service = CryptoDeskService(settings, store, evidence_builder=FakeBuilder(), now=lambda: NOW)

    assert sorted(service.refresh_reflections(NOW)) == ["btc-run", "near-run"]
    cohorts = {row["symbol"]: row["payload"]["cohort"] for row in store.list_reflections()}
    assert cohorts == {"NEARUSDT": "watchlist", "BTCUSDT": "allowlist"}
```

`tests/test_scorecard.py`:
- Thêm tham số `cohort: str | None = None` vào `_reflection`, và `if cohort is not None: payload["cohort"] = cohort`.
- Thêm test:

```python
def test_watchlist_research_is_scored_apart_from_the_allowlist():
    card = build_scorecard(
        [
            _reflection("ETHUSDT", "ACCUMULATE", "0.05", benchmark="BTCUSDT"),
            _reflection("NEARUSDT", "ACCUMULATE", "-0.03", benchmark="BTCUSDT", cohort="watchlist"),
        ]
    )

    by = {group.cohort: group for group in card.groups}
    assert set(by) == {"allowlist", "watchlist"}
    assert by["watchlist"].scores[0].mean_return == Decimal("-0.03")
    assert by["allowlist"].scores[0].mean_return == Decimal("0.05")
    assert "watchlist (nghiên cứu)" in card.render()
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_service_cli.py -k cohort tests/test_scorecard.py -v`
Expected: FAIL. `near-run` chưa được chấm vì bị lọc symbol, và `BenchmarkGroup` chưa có trường `cohort`.

- [ ] **Step 3: Sửa code**

`service.py`:
- `save_reflection`: thêm keyword `cohort: str | None = None` sau `levels`; sau dòng `payload["benchmark_symbol"] = benchmark_symbol` thêm `if cohort is not None: payload["cohort"] = cohort`.
- `refresh_reflections`: đổi `self.store.unreflected_runs(completed_before, tuple(self.settings.symbols))` thành

```python
        symbols = tuple(self.settings.symbols) + self.store.watchlist_symbols()
        for run in self.store.unreflected_runs(completed_before, symbols):
```

- Ở lời gọi `self.save_reflection(...)` trong `refresh_reflections`, thêm:

```python
                    # Read from the decision, not today's allowlist: promoting a coin later
                    # must not move its research runs into the allowlist cohort.
                    cohort="watchlist" if decision.get("research_only") else "allowlist",
```

`scorecard.py`:
- `BenchmarkGroup`: thêm `cohort: str` sau `benchmark`.
- `render`: đổi `lines.append(f"Benchmark: {group.benchmark}")` thành

```python
            label = " · watchlist (nghiên cứu)" if group.cohort == "watchlist" else ""
            lines.append(f"Benchmark: {group.benchmark}{label}")
```

- Thay thân `build_scorecard` từ đầu hàm tới trước `counted = ...` bằng:

```python
    latest: dict[tuple[str, str, str, str, str], tuple[str, dict[str, Any]]] = {}
    inferred_flags: dict[tuple[str, str], bool] = {}
    skipped_no_action = 0
    skipped_unknown_action = 0
    for item in reflections:
        bench, inferred = _benchmark_of(item)
        payload = item["payload"]
        cohort = str(payload.get("cohort") or "allowlist")
        action = payload.get("decision_action")
        if not action:
            skipped_no_action += 1
            continue
        action = str(action)
        if action not in ACTIONS:
            skipped_unknown_action += 1
            continue
        # Hàng cũ không có decision_cutoff: lùi về created_at, thô hơn nhưng vẫn
        # gộp được các lần chạy cùng ngày của cùng symbol.
        when = str(payload.get("decision_cutoff") or item["created_at"])
        group = (bench, cohort)
        inferred_flags[group] = inferred_flags.get(group, False) or inferred
        key = (bench, cohort, action, item["symbol"], when[:10])
        if key not in latest or when > latest[key][0]:
            latest[key] = (when, item)
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (bench, cohort, action, _, _), (_, item) in latest.items():
        buckets.setdefault((bench, cohort, action), []).append(item)
    groups = tuple(
        BenchmarkGroup(
            benchmark=bench,
            cohort=cohort,
            inferred=inferred_flags[(bench, cohort)],
            scores=tuple(
                _score(action, bench, buckets[(bench, cohort, action)])
                for action in ACTIONS
                if (bench, cohort, action) in buckets
            ),
        )
        for bench, cohort in sorted(inferred_flags)
    )
```

- Thêm một câu vào docstring module: "Run nghiên cứu từ watchlist của máy quét được chấm thành nhóm riêng (`cohort`), để thành tích của máy quét không trộn vào allowlist."

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_scorecard.py tests/test_service_cli.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/service.py src/crypto_desk/scorecard.py tests/test_service_cli.py tests/test_scorecard.py
git commit -m "feat: grade watchlist research as its own scorecard cohort

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Lệnh CLI `scan`, `watchlist`, `analyze --research`

**Files:**
- Modify: `src/crypto_desk/cli.py`
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Consumes: `CryptoDeskService.scan()` (Task 7); `analyze(..., research=)` (Task 6); `WatchlistScanner` (Task 4); `Store.active_watchlist` (Task 5).
- Produces: `_service(settings, *, broker=False, committee=True, execution=False, scanner=False)`. Các lệnh `desk scan`, `desk watchlist`, `desk analyze SYMBOL --research`.

- [ ] **Step 1: Viết test fail**

Sửa fake sẵn có: trong `test_dashboard_hook_fires_after_analyze`, đổi `def analyze(self, symbol):` thành `def analyze(self, symbol, *, research=False):`. Lệnh `analyze` mới luôn truyền `research=`, nên fake cũ sẽ ném `TypeError`.

Trong `test_public_commands_exist`, thêm `"scan"` và `"watchlist"` vào danh sách lệnh.

Thêm vào cuối `tests/test_service_cli.py`:

```python
def test_scan_command_runs_the_scanner_and_emits_the_result(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    requested: dict[str, Any] = {}

    class _Service:
        def scan(self):
            return {"status": "COMPLETED", "picks": [{"symbol": "NEARUSDT"}]}

    def fake_service(settings, **kwargs):
        requested.update(kwargs)
        return _Service()

    monkeypatch.setattr("crypto_desk.cli._service", fake_service)

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "scan"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["picks"] == [{"symbol": "NEARUSDT"}]
    assert requested["scanner"] is True


def test_analyze_research_flag_skips_the_allowlist_gate(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)
    calls: list[tuple[str, bool]] = []

    class _Service:
        def analyze(self, symbol, *, research=False):
            calls.append((symbol, research))
            return {"run_id": "run-1"}

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: _Service())

    result = CliRunner().invoke(
        app, ["--config", str(config), "--json", "analyze", "nearusdt", "--research"]
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [("NEARUSDT", True)]


def test_analyze_research_without_an_active_entry_fails_cleanly(tmp_path: Path, monkeypatch):
    config = _write_config(tmp_path)

    class _Service:
        def analyze(self, symbol, *, research=False):
            raise ValueError("Research analysis needs an active watchlist entry")

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kwargs: _Service())

    result = CliRunner().invoke(app, ["--config", str(config), "analyze", "OLDUSDT", "--research"])

    # Exit code 2 from _fail, not 1 from an uncaught traceback.
    assert result.exit_code == 2
    assert "active watchlist entry" in result.output


def test_scan_service_shares_one_model_client_and_one_public_client(tmp_path: Path, monkeypatch):
    created: list[object] = []

    def fake_client(settings):
        created.append(object())
        return created[-1]

    monkeypatch.setattr("crypto_desk.cli._structured_client", fake_client)

    service = _service(make_settings(tmp_path), scanner=True)

    assert len(created) == 1
    assert service.scanner.llm is service.committee.llm
    assert service.scanner.client is service.evidence_builder.client


def test_watchlist_command_lists_active_entries(tmp_path: Path):
    config = _write_config(tmp_path)
    store = Store(tmp_path / "crypto.sqlite3")
    store.upsert_watchlist(
        "NEARUSDT",
        "near",
        "scan-1",
        "2026-09-27T05:00:00+00:00",
        "2999-01-01T00:00:00+00:00",
        {"evidence_score": "8", "thesis": "Trend holds."},
    )
    store.close()

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "watchlist"])

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert [(row["symbol"], row["latest_action"], row["latest_cutoff"]) for row in rows] == [
        ("NEARUSDT", None, None)
    ]
```

`_write_config` ghi `database: <tmp>/crypto.sqlite3`, nên Store trong test phải mở đúng đường dẫn đó.

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_service_cli.py -k "scan_command or research_flag or fails_cleanly or shares_one or watchlist_command or public_commands" -v`
Expected: FAIL. Chưa có lệnh `scan`, `watchlist`, option `--research`, và `_service` chưa nhận `scanner`.

- [ ] **Step 3: Sửa code**

Thêm import trong `cli.py`:

```python
from .config import WATCHLIST_MAX_ACTIVE
from .scanner import WatchlistScanner
```

Nếu `cli.py` đã import từ `.config` thì gộp vào dòng đó.

Lệnh `analyze`: thay thân hiện tại bằng

```python
@app.command()
def analyze(
    ctx: typer.Context,
    symbol: str,
    research: Annotated[bool, typer.Option("--research")] = False,
) -> None:
    settings = _load(ctx)
    normalized = symbol.upper()
    if not research and normalized not in settings.symbols:
        _fail("Symbol is outside the configured allowlist")
    service = _service(settings, broker=not research)
    try:
        result = service.analyze(normalized, research=research)
    except ValueError as exc:
        # A missing or expired watchlist entry is an operator error, not a crash.
        _fail(str(exc))
    _publish_dashboard_if_configured(settings)
    _emit(ctx, result)
```

Thêm hai lệnh, ngay sau `analyze`:

```python
@app.command()
def scan(ctx: typer.Context) -> None:
    """Quét top 30 cặp USDT bằng model rẻ: tối đa 10 đồng vào watchlist, top 3 tự phân tích."""
    settings = _load(ctx)
    result = _service(settings, scanner=True).scan()
    _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


@app.command()
def watchlist(ctx: typer.Context) -> None:
    """Watchlist nghiên cứu còn hạn, kèm quyết định committee gần nhất và thời điểm của nó."""
    settings = _load(ctx)
    store = Store(settings.database)
    try:
        rows = []
        for entry in store.active_watchlist(iso(_utcnow()), WATCHLIST_MAX_ACTIVE):
            latest = store.latest_valid_run(entry["symbol"])
            rows.append(
                {
                    **entry,
                    "latest_action": latest["decision"]["action"] if latest else None,
                    "latest_cutoff": latest["cutoff"] if latest else None,
                }
            )
    finally:
        store.close()
    _emit(ctx, rows)
```

`iso` phải được import từ `.domain` nếu `cli.py` chưa có.

`_service`: thay cả hàm bằng bản dưới. Committee và máy quét dùng chung một model client, vì khoảng cách tối thiểu giữa hai request của Gemini (`GEMINI_MIN_REQUEST_INTERVAL_SECONDS`) tính theo từng client. Máy quét cũng dùng chung public client với evidence builder.

```python
def _service(
    settings: Settings,
    *,
    broker: bool = False,
    committee: bool = True,
    execution: bool = False,
    scanner: bool = False,
) -> CryptoDeskService:
    store = Store(settings.database)
    public = PublicDataClient(settings.news_feeds)
    evidence_builder = EvidenceBuilder(public, settings.coingecko_ids)
    selected_broker = None
    if broker:
        try:
            selected_broker = _broker(settings)
        except (ValueError, BrokerError):
            if execution:
                raise
            selected_broker = None
    # One model client: Gemini spaces requests per client, so a second client would let the
    # scan call and the first committee call land back to back.
    llm = _structured_client(settings) if committee or scanner else None
    selected_committee = None
    if committee:
        selected_committee = CryptoCommittee(
            llm,
            provider=settings.models.provider,
            quick_model=settings.models.quick,
            deep_model=settings.models.deep,
            quick_thinking=settings.models.quick_thinking,
            deep_thinking=settings.models.deep_thinking,
            debate_rounds=settings.models.debate_rounds,
        )
    selected_execution = None
    if execution:
        assert selected_broker is not None
        selected_execution = ExecutionService(
            store,
            selected_broker,
            settings,
        )
    selected_scanner = None
    if scanner:
        selected_scanner = WatchlistScanner(
            public,
            llm,
            model=settings.models.quick,
            thinking=settings.models.quick_thinking,
        )
    return CryptoDeskService(
        settings,
        store,
        broker=selected_broker,
        evidence_builder=evidence_builder,
        committee=selected_committee,
        execution=selected_execution,
        scanner=selected_scanner,
    )
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_service_cli.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/cli.py tests/test_service_cli.py
git commit -m "feat: add desk scan, desk watchlist and analyze --research

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Mục `watchlist` trong snapshot dashboard (Python)

**Files:**
- Modify: `src/crypto_desk/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `Store.active_watchlist` (Task 5); `WATCHLIST_MAX_ACTIVE`.
- Produces:
  - `WatchlistSection` (pydantic, `extra="forbid"`)
  - `DashboardSnapshot.watchlist: list[WatchlistSection] = []`
  - Helper `_latest_valid_decision(row) -> LatestValidDecision | None`, tách ra từ `_build_symbols`.

- [ ] **Step 1: Viết test fail** — thêm vào cuối `tests/test_dashboard.py`:

```python
def test_snapshot_lists_active_watchlist_research_with_its_decision(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    settings = make_settings()
    store.upsert_watchlist(
        "NEARUSDT",
        "near",
        "scan-1",
        "2026-09-27T05:00:00+00:00",
        "2999-01-01T00:00:00+00:00",
        {"evidence_score": "8.5", "thesis": "Trend holds above support."},
    )
    store.upsert_watchlist(
        "OLDUSDT",
        "old",
        "scan-0",
        "2026-09-01T05:00:00+00:00",
        "2026-09-08T05:00:00+00:00",
        {"evidence_score": "9", "thesis": "Expired."},
    )
    report_dir = tmp_path / "near-run"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    store.save_run(
        "near-run",
        "2026-09-27T05:10:00+00:00",
        make_decision(symbol="NEARUSDT", action="ACCUMULATE"),
        report_dir,
    )

    snapshot = build_dashboard_snapshot(settings, store)

    assert [entry.symbol for entry in snapshot.watchlist] == ["NEARUSDT"]
    entry = snapshot.watchlist[0]
    assert entry.evidence_score == Decimal("8.5")
    assert entry.latest_valid_decision.action == "ACCUMULATE"
    assert all(item.symbol != "NEARUSDT" for item in snapshot.symbols)
    dumped = snapshot.model_dump(mode="json")
    assert dumped["watchlist"][0]["thesis"] == "Trend holds above support."
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_dashboard.py -k watchlist -v`
Expected: FAIL với `AttributeError: 'DashboardSnapshot' object has no attribute 'watchlist'`.

- [ ] **Step 3: Sửa code**

Thêm import: `from .config import WATCHLIST_MAX_ACTIVE` (gộp với import config có sẵn nếu có).

Thêm model trước `DashboardSnapshot`:

```python
class WatchlistSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    evidence_score: Decimal
    thesis: str
    last_picked_at: str
    expires_at: str
    mark_usdt: Decimal | None
    change_24h_pct: Decimal | None
    sparkline_closes: list[Decimal] = Field(default_factory=list)
    latest_valid_decision: LatestValidDecision | None = None
```

Trong `DashboardSnapshot`, thêm `watchlist: list[WatchlistSection] = Field(default_factory=list)` sau `symbols`.

Tách helper: cắt khối trong `_build_symbols` đang dựng `futures_setups` và `LatestValidDecision(...)` từ `latest_valid_row` ra thành

```python
def _latest_valid_decision(row: dict[str, Any] | None) -> LatestValidDecision | None:
    if row is None:
        return None
    decision = row["decision"]
    futures_setups = [
        DashboardFuturesSetup(
            direction=str(setup["direction"]),
            entry=Decimal(str(setup["entry"])),
            stop=Decimal(str(setup["stop"])),
            target=Decimal(str(setup["target"])),
            risk_reward_ratio=Decimal(str(setup.get("risk_reward_ratio", "1.5"))),
            rationale=str(setup.get("rationale", "")),
        )
        for setup in decision.get("futures_setups") or []
    ]
    return LatestValidDecision(
        run_id=row["id"],
        cutoff=row["cutoff"],
        action=decision["action"],
        conviction=Decimal(decision["conviction"]),
        bull_case=decision["bull_case"],
        bear_case=decision["bear_case"],
        catalysts=list(decision["catalysts"]),
        invalidation=decision["invalidation"],
        entry=Decimal(decision["entry"]) if decision["entry"] is not None else None,
        stop=Decimal(decision["stop"]) if decision["stop"] is not None else None,
        target=Decimal(decision["target"]) if decision["target"] is not None else None,
        futures_bias=decision.get("futures_bias"),
        futures_setups=futures_setups,
    )
```

Trong `_build_symbols`, thay khối đó bằng `latest_valid_decision = _latest_valid_decision(latest_valid_row)`, giữ nguyên phần còn lại. Nếu `Any` chưa được import trong `dashboard.py` thì dùng `dict[str, object]` như các helper xung quanh.

Thêm builder:

```python
def _build_watchlist(store: Store, current: datetime) -> list[WatchlistSection]:
    sections: list[WatchlistSection] = []
    for entry in store.active_watchlist(iso(current), WATCHLIST_MAX_ACTIVE):
        row = store.latest_valid_run(entry["symbol"])
        evidence = _evidence_payload(row)
        sections.append(
            WatchlistSection(
                symbol=entry["symbol"],
                evidence_score=Decimal(str(entry["payload"]["evidence_score"])),
                thesis=str(entry["payload"].get("thesis", ""))[:400],
                last_picked_at=entry["last_picked_at"],
                expires_at=entry["expires_at"],
                mark_usdt=_evidence_mark_usdt(evidence),
                change_24h_pct=_evidence_change_24h_pct(evidence),
                sparkline_closes=_evidence_sparkline_closes(evidence, limit=30),
                latest_valid_decision=_latest_valid_decision(row),
            )
        )
    return sections
```

Trong `build_dashboard_snapshot`, truyền `watchlist=_build_watchlist(store, current)` vào `DashboardSnapshot(...)`.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_dashboard.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/dashboard.py tests/test_dashboard.py
git commit -m "feat: publish the research watchlist in the dashboard snapshot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Web — parser và nhóm "Watchlist nghiên cứu"

**Files:**
- Modify: `web/lib/dashboard-contract.ts`
- Create: `web/components/research-watchlist.tsx`
- Modify: `web/components/watchlist-view.tsx`, `web/app/page.tsx`
- Test: `web/tests/unit/watchlist-contract.test.mjs` (mới)

**Interfaces:**
- Consumes: `watchlist` do Task 10 publish.
- Produces:
  - `DashboardWatchlistEntry`
  - `DashboardSnapshot.watchlist: DashboardWatchlistEntry[]`
  - `ResearchWatchlist({ entries })`
  - `WatchlistView` nhận thêm prop `research?: DashboardWatchlistEntry[]`

- [ ] **Step 1: Viết test fail** — tạo `web/tests/unit/watchlist-contract.test.mjs`:

```js
import assert from "node:assert/strict";
import test from "node:test";
import { parseDashboardSnapshot } from "../../lib/dashboard-contract.ts";

function payload(extra = {}) {
  return {
    schema_version: 1,
    generated_at: "2026-09-27T06:00:00+00:00",
    environment: "mainnet",
    health: {
      runner_state: "online",
      research_state: "healthy",
      last_health_at: null,
      stale_after_seconds: 1200,
      alerts: [],
    },
    portfolio: {
      as_of: null,
      nav_usdt: "0",
      free_usdt: "0",
      gross_exposure_usdt: "0",
      deployed_pct: "0",
      open_orders_count: 0,
      assets: [],
      configured_positions: [],
      external_assets_count: 0,
      external_value_usdt: "0",
      unpriced_assets_count: 0,
    },
    symbols: [],
    operations: {
      tickets_total: 0,
      tickets_actionable: 0,
      orders_total: 0,
      orders_open: 0,
      recent_events: [],
    },
    ...extra,
  };
}

function entry(symbol = "NEARUSDT") {
  return {
    symbol,
    evidence_score: "8.5",
    thesis: "Trend holds above support.",
    last_picked_at: "2026-09-27T05:00:00+00:00",
    expires_at: "2026-10-04T05:00:00+00:00",
    mark_usdt: "2.1",
    change_24h_pct: "-1.5",
    sparkline_closes: ["2.0", "2.1"],
    latest_valid_decision: null,
  };
}

test("an older publisher without watchlist parses to an empty list", () => {
  assert.deepEqual(parseDashboardSnapshot(payload()).watchlist, []);
});

test("watchlist entries parse outside the known symbol list", () => {
  const parsed = parseDashboardSnapshot(payload({ watchlist: [entry()] }));
  assert.equal(parsed.watchlist[0].symbol, "NEARUSDT");
  assert.equal(parsed.watchlist[0].evidence_score, "8.5");
});

test("a symbol that is not a USDT pair is rejected", () => {
  assert.throws(() => parseDashboardSnapshot(payload({ watchlist: [entry("near")] })));
});

test("more than ten entries are rejected", () => {
  const eleven = Array.from({ length: 11 }, (_, index) => entry(`C${index}USDT`));
  assert.throws(() => parseDashboardSnapshot(payload({ watchlist: eleven })));
});
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `cd web && node --experimental-strip-types --no-warnings --test tests/unit/watchlist-contract.test.mjs`
Expected: FAIL. `watchlist` là `undefined`, và symbol sai chưa bị ném lỗi.

Nếu payload tối giản không qua được các check hiện có (ví dụ `last_health_at: null` hay `as_of: null`), đối chiếu với `parseHealth`/`parsePortfolio` rồi sửa **fixture của test**, không sửa parser.

- [ ] **Step 3: Sửa contract** — trong `web/lib/dashboard-contract.ts`:

Sau `const MAX_LONG_STRING = 4096; ...` thêm:

```ts
const MAX_WATCHLIST = 10;
const WATCHLIST_SYMBOL = /^[A-Z0-9]{2,20}USDT$/;
```

Sau `export interface DashboardSymbol {...}` thêm:

```ts
// Research-only coins from the cheap-model scanner. They sit outside KNOWN_SYMBOLS on
// purpose: nothing here is tradable, so nothing here reaches the command deck.
export interface DashboardWatchlistEntry {
  symbol: string;
  evidence_score: string;
  thesis: string;
  last_picked_at: string;
  expires_at: string;
  mark_usdt: string | null;
  change_24h_pct: string | null;
  sparkline_closes: string[];
  latest_valid_decision: DashboardLatestValidDecision | null;
}
```

Trong `DashboardSnapshot`, thêm `watchlist: DashboardWatchlistEntry[];` sau `symbols`.

Thêm parser, đặt trước `parseRecentEvent`:

```ts
function parseWatchlistEntry(value: unknown, path: string): DashboardWatchlistEntry {
  if (!isPlainObject(value)) fail(path, "must be an object");
  const symbol = checkString(value.symbol, `${path}.symbol`, MAX_SHORT_STRING);
  if (!WATCHLIST_SYMBOL.test(symbol)) fail(`${path}.symbol`, "must be a USDT pair");
  const sparklineRaw = Array.isArray(value.sparkline_closes) ? value.sparkline_closes : [];
  return {
    symbol,
    evidence_score: checkDecimalString(value.evidence_score, `${path}.evidence_score`, {
      nonNegative: true,
    }),
    thesis: checkString(value.thesis, `${path}.thesis`, MAX_LONG_STRING),
    last_picked_at: checkIsoTimestamp(value.last_picked_at, `${path}.last_picked_at`),
    expires_at: checkIsoTimestamp(value.expires_at, `${path}.expires_at`),
    mark_usdt: checkNullableDecimalString(value.mark_usdt, `${path}.mark_usdt`, {
      nonNegative: true,
    }),
    change_24h_pct: checkNullableDecimalString(value.change_24h_pct, `${path}.change_24h_pct`, {
      nonNegative: false,
    }),
    sparkline_closes: sparklineRaw.map((close, index) =>
      checkDecimalString(close, `${path}.sparkline_closes[${index}]`, { nonNegative: true })
    ),
    latest_valid_decision: parseLatestValidDecision(
      value.latest_valid_decision ?? null,
      `${path}.latest_valid_decision`
    ),
  };
}
```

Trong `parseDashboardSnapshot`, thêm vào object trả về, ngay sau `symbols,`:

```ts
    watchlist:
      value.watchlist === undefined
        ? []
        : checkBoundedArray(value.watchlist, "$.watchlist", MAX_WATCHLIST).map((item, index) =>
            parseWatchlistEntry(item, `$.watchlist[${index}]`)
          ),
```

- [ ] **Step 4: Chạy test contract**

Run: `cd web && node --experimental-strip-types --no-warnings --test tests/unit/watchlist-contract.test.mjs && npm run test:unit`
Expected: PASS toàn bộ unit test. Test cũ vẫn xanh, vì payload thiếu `watchlist` được coi là `[]`.

- [ ] **Step 5: Component** — tạo `web/components/research-watchlist.tsx`:

```tsx
import type { DashboardWatchlistEntry } from "../lib/dashboard-contract";
import { formatVietnamDateTime } from "../lib/date-format";
import { CoinIcon, MiniSparkline } from "./coin-icon";

function grossRiskReward(entry: string | null, stop: string | null, target: string | null) {
  if (entry === null || stop === null || target === null) return null;
  const [e, s, t] = [Number(entry), Number(stop), Number(target)];
  return e > s ? ((t - e) / (e - s)).toFixed(2) : null;
}

export function ResearchWatchlist({ entries }: { entries: DashboardWatchlistEntry[] }) {
  if (entries.length === 0) return null;
  return (
    <div className="watchlist-table-panel" aria-label="Watchlist nghiên cứu">
      <h3 className="watchlist-section-title">Watchlist nghiên cứu — không giao dịch</h3>
      <p>Top 3 được committee tự phân tích; đồng khác chạy `desk analyze SYMBOL --research`.</p>
      <table className="watchlist-full-table">
        <thead>
          <tr>
            <th>TÀI SẢN</th>
            <th>GIÁ</th>
            <th>24H</th>
            <th>XU HƯỚNG</th>
            <th>ĐIỂM QUÉT</th>
            <th>COMMITTEE</th>
            <th>ENTRY / STOP / TARGET</th>
            <th>LUẬN ĐIỂM</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((item) => {
            const decision = item.latest_valid_decision;
            const change = Number(item.change_24h_pct ?? "0");
            const rr = decision
              ? grossRiskReward(decision.entry, decision.stop, decision.target)
              : null;
            return (
              <tr key={item.symbol} className="watchlist-row">
                <td>
                  <CoinIcon symbol={item.symbol} /> {item.symbol}
                </td>
                <td>{item.mark_usdt ?? "—"}</td>
                <td>{item.change_24h_pct === null ? "—" : `${change.toFixed(2)}%`}</td>
                <td>
                  <MiniSparkline closes={item.sparkline_closes} isPositive={change >= 0} />
                </td>
                <td>{item.evidence_score}/10</td>
                <td>
                  {decision ? (
                    <>
                      {`${decision.action} · ${decision.conviction}/10`}
                      <br />
                      {/* A manual coin may still carry a decision from an earlier pick. */}
                      <small>{formatVietnamDateTime(decision.cutoff)}</small>
                    </>
                  ) : (
                    "Chờ phân tích thủ công"
                  )}
                </td>
                <td>
                  {decision && decision.entry !== null
                    ? `${decision.entry} / ${decision.stop} / ${decision.target}${rr ? ` · R:R ${rr}` : ""}`
                    : "—"}
                </td>
                <td>{item.thesis}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 6: Gắn vào giao diện**

`web/components/watchlist-view.tsx`:
- Import `import type { DashboardWatchlistEntry } from "../lib/dashboard-contract";` (gộp với import type có sẵn) và `import { ResearchWatchlist } from "./research-watchlist";`.
- Thêm `research?: DashboardWatchlistEntry[];` vào `WatchlistViewProps`.
- Destructure `research` trong chữ ký `WatchlistView`.
- Chèn `<ResearchWatchlist entries={research ?? []} />` ngay trước `</div>` cuối cùng của `watchlist-view-container`, tức ngay trên các dòng `    </div>\n  );\n}` ở cuối file.

`web/app/page.tsx`: trong `<WatchlistView ...>`, thêm prop `research={snapshot.watchlist}` ngay sau `symbols={displaySymbols}`.

Nếu `globals.css` chưa có `.watchlist-section-title`, thêm:

```css
.watchlist-section-title {
  margin: 24px 0 8px;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
```

- [ ] **Step 7: Kiểm tra toàn bộ web**

Run: `cd web && npm run test:unit && npm run lint && npm run build`
Expected: unit test PASS, lint sạch, build thành công. Nếu `build` cần secret hoặc wrangler login thì ghi lại lỗi và chỉ yêu cầu `test:unit` và `lint` xanh.

- [ ] **Step 8: Commit**

```bash
git add web/lib/dashboard-contract.ts web/components/research-watchlist.tsx web/components/watchlist-view.tsx web/app/page.tsx web/app/globals.css web/tests/unit/watchlist-contract.test.mjs
git commit -m "feat(web): show the research watchlist beside the tracked symbols

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: README và chạy thử thật

**Files:**
- Modify: `README.md` (mục "Luồng sử dụng")

- [ ] **Step 1: README** — thêm vào khối lệnh của "Luồng sử dụng":

```bash
uv run desk --config config.yaml --json scan
uv run desk --config config.yaml --json watchlist
uv run desk --config config.yaml --json analyze NEARUSDT --research
```

Thêm đoạn giải thích ngay dưới đoạn về `desk scorecard`:

```markdown
`desk scan` quét top 30 cặp USDT (có futures USDⓈ-M, CoinGecko id, OCO/OTO, volume ≥ 20M
USDT, ngoài allowlist). Code tính bảng chỉ số, Gemini Flash (`models.quick`) chọn tối đa 10
đồng có evidence mạnh cho thesis Spot long 20 ngày. Cả 10 đồng vào watchlist 7 ngày;
committee tự phân tích 3 đồng điểm cao nhất, đồng còn lại chạy tay bằng
`desk analyze SYMBOL --research`. Run nghiên cứu không bao giờ tạo ticket và được chấm
thành nhóm `watchlist` riêng trong scorecard. Muốn giao dịch một đồng thì phải sửa code:
thêm nó vào `V1_SYMBOLS`, `coingecko_ids` và `KNOWN_SYMBOLS` của web, rồi mới đưa vào
`symbols` trong config.
```

- [ ] **Step 2: Chạy toàn bộ và commit**

Run: `uv run pytest -q && uv run ruff check src tests && (cd web && npm run test:unit)`

```bash
git add README.md
git commit -m "docs: document desk scan and the research watchlist

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Chạy thật (thủ công; gọi API public và khoảng 31 lần gọi Gemini: 1 Flash + 3 committee)**

Run: `uv run desk --config config.yaml --json scan`
Expected:
- `status: COMPLETED`. `universe_size` khoảng 20–30 (lúc viết plan là 22).
- Tối đa 10 pick. Đúng `min(3, số pick)` pick đầu có `run_id`; các pick còn lại có `run_id: null`.
- `desk watchlist` liệt kê mọi pick; đồng chưa phân tích có `latest_action: null`.
- Không có `ticket_*` mới: `desk --json tickets` không đổi.
- Mỗi run có `ticket_blocked.json` với lý do `research_only` khi action là ACCUMULATE.

Sau đó chạy tay một pick không được tự phân tích (khoảng 10 lần gọi Gemini):
`uv run desk --config config.yaml --json analyze <SYMBOL> --research`. Kết quả phải có `run_id`, không có ticket, và `desk watchlist` hiện `latest_action` của đồng đó.

Nếu `status: FAILED`, đọc `error` trong artifact và báo lại, không tự sửa prompt hay code.

## Ngoài phạm vi

Giữ đúng mục "Ngoài phạm vi" của spec: lịch tự động, nút quét và nút phân tích watchlist trên dashboard, tự lấy pick thứ 4 bù khi top 3 lỗi evidence, hạ ngưỡng volume xuống 10M, tự đưa đồng vào allowlist, percentile top trader, VN.
