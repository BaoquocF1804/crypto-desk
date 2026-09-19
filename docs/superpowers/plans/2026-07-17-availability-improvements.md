# Availability Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Rev 2 (2026-07-17):** cập nhật theo code review `docs/superpowers/plans/review.md` — Task 2 chuyển sang batch pricing + phân biệt `unpriced_reason`; Task 3 chỉ mở khóa SELL, BUY vẫn fail-closed khi còn unpriced; Task 4 thêm lọc future-dated + follow redirects; Task 5 không cache thất bại toàn phần; Task 7 bắt buộc đủ giá cho mọi actionable action; Task 10 thêm 4 test chặn-resume + check approval record; Task 1 bỏ hardcode đường dẫn.

**Goal:** Loại bỏ các điểm fail-closed quá mức đang khiến Crypto Desk NO_TRADE hoặc chặn approve trong vận hành thực tế, mà không nới lỏng bất kỳ cổng an toàn Mainnet nào.

**Architecture:** Giữ nguyên pipeline data → screener → committee → risk → execution. Broker định giá tài sản ngoài allowlist bằng MỘT lời gọi batch bookTicker (weight 4); BUY vẫn bị chặn khi còn tài sản chưa định giá (vì thiếu giá trị U làm max_gross/usdt_reserve room bị tính dư 0.2·U), chỉ SELL (giảm rủi ro) được phép. Data layer cô lập lỗi từng RSS feed, lọc item future-dated, cache có điều kiện, retry. Committee thay regex numeric-claim bằng hai chốt tất định: đủ entry/stop/target cho mọi actionable action và entry lệch ≤2% so với mid.

**Tech Stack:** Python 3.12, uv, httpx, pydantic v2, typer, sqlite3, pytest, ruff. Không thêm dependency mới.

## Global Constraints

- Python `>=3.12,<3.13`, quản lý bằng `uv`; chạy test bằng `uv run pytest -q` (sau Task 1).
- Tiền và giá luôn dùng `Decimal`, tuyệt đối không dùng `float`.
- KHÔNG được nới lỏng cổng Mainnet: `BINANCE_ENV=mainnet` + `LIVE_EXECUTION_ENABLED=1` + actor trong `telegram_allowlist` + confirmation code HMAC + Telegram ingress proof — cả năm điều kiện giữ nguyên.
- Giữ contract fail-closed: evidence thiếu/stale/lệch giá >0.5% → `NO_TRADE`; timeout/kết quả mơ hồ → `RECONCILE_REQUIRED`, không bao giờ tự resubmit; BUY bị chặn khi còn tài sản chưa định giá.
- Chuỗi hiển thị cho người dùng (CLI, README, config mẫu) viết tiếng Việt; tên test/code tiếng Anh.
- `pyproject.toml` không đổi (không thêm dependency).
- ruff line-length 100; sau mỗi task `uv run ruff check .` và `uv run ruff format --check .` phải sạch.
- Baseline: 110 test đang pass. Mỗi task kết thúc với toàn bộ suite xanh (`uv run pytest -q`).
- Ngoài phạm vi (chủ động bỏ, YAGNI): parallel hóa fetch per-symbol trong `screen()` (httpx sync client không đảm bảo thread-safe; cache news ở Task 5 đã loại phần fetch trùng lặp lớn nhất).

## Nhóm PR đề xuất

Mỗi task một commit; khi mở PR nên gom theo subsystem để review độc lập:

- **PR A — portfolio/risk:** Task 2, 3.
- **PR B — data reliability:** Task 4, 5, 6, 11.
- **PR C — committee validation:** Task 7.
- **PR D — execution/operations:** Task 8, 9, 10.

Task 1 là môi trường cục bộ, không tạo PR.

---

### Task 1: Khôi phục môi trường dev

`.venv` hiện tại còn shim trỏ tới đường dẫn worktree cũ trên OneDrive (`uv run pytest` lỗi exit 127). Xây lại venv tại chỗ. Không hardcode đường dẫn tuyệt đối; kiểm tra `uv` trước khi chạy.

**Files:** không đổi file nào trong repo (`.venv/` đã nằm trong `.gitignore`).

- [ ] **Step 1: Đảm bảo có `uv` trên PATH**

Run (từ repo root):

```bash
command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
command -v uv
```

Expected: in ra đường dẫn `uv` (ví dụ `~/.local/bin/uv`), exit 0.

- [ ] **Step 2: Xóa venv cũ và sync lại**

Run (từ repo root): `rm -rf .venv && uv sync --extra dev`
Expected: kết thúc với `Installed N packages` không lỗi. `uv sync` sinh lại toàn bộ launcher trong `.venv/bin` trỏ đúng interpreter mới.

- [ ] **Step 3: Xác nhận test runner hoạt động trực tiếp**

Run: `uv run pytest -q`
Expected: `110 passed`

- [ ] **Step 4: Xác nhận lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` và không file nào cần format.

Không có commit (không thay đổi file được track).

---

### Task 2: Broker định giá tài sản ngoài allowlist (batch, phân biệt lý do unpriced)

Hiện tại mọi asset ngoài `V1_SYMBOLS` bị gắn `unpriced=True, value_usdt="0"` và loại khỏi NAV. Task này định giá chúng bằng MỘT lời gọi batch `ticker_book_ticker()` không tham số (weight 4, rẻ hơn gọi lẻ từng symbol weight 2 khi có ≥2 asset): symbol có trong batch → tính vào NAV/gross với cờ `external`; không có trong batch → `unpriced_reason="no_usdt_pair"`; batch call thất bại → `unpriced_reason="pricing_unavailable"`. Batch chỉ được gọi khi thực sự có asset ngoài V1 (tài khoản chỉ có V1 + USDT không tốn thêm request nào).

**Files:**
- Modify: `src/crypto_desk/broker.py` (method `account_snapshot`, thêm method `_external_mids`)
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: `_unwrap(...)` và `self._client.rest_api.ticker_book_ticker()` (không tham số → toàn bộ symbols) sẵn có trong `broker.py`.
- Produces: position dict có thể chứa `"external": True` (asset ngoài V1 định giá được, đã tính vào NAV) hoặc `"unpriced": True` kèm `"unpriced_reason": "no_usdt_pair" | "pricing_unavailable"`. Task 3 tiêu thụ các key này.

- [ ] **Step 1: Viết bốn test fail**

Thêm vào cuối `tests/test_broker.py`:

```python
def test_non_allowlist_asset_with_usdt_pair_is_priced_into_nav(testnet_env, sdk):
    original = sdk.rest_api.ticker_book_ticker

    def with_batch(**kwargs):
        if "symbol" not in kwargs:
            return [{"symbol": "DOGEUSDT", "bidPrice": "0.10", "askPrice": "0.12"}]
        return original(**kwargs)

    sdk.rest_api.ticker_book_ticker = with_batch
    sdk.rest_api.account["balances"].append({"asset": "DOGE", "free": "100", "locked": "0"})

    snapshot = BinanceSpotBroker("testnet", client=sdk).account_snapshot()

    doge = next(position for position in snapshot.positions if position["asset"] == "DOGE")
    assert doge["external"] is True
    assert doge["mid_usdt"] == "0.11"
    assert doge["value_usdt"] == "11.00"
    assert snapshot.nav_usdt == Decimal("281.00000000")


def test_asset_without_usdt_pair_is_marked_no_usdt_pair(testnet_env, sdk):
    original = sdk.rest_api.ticker_book_ticker

    def with_batch(**kwargs):
        if "symbol" not in kwargs:
            return []
        return original(**kwargs)

    sdk.rest_api.ticker_book_ticker = with_batch
    sdk.rest_api.account["balances"].append({"asset": "ODD", "free": "1", "locked": "0"})

    snapshot = BinanceSpotBroker("testnet", client=sdk).account_snapshot()

    odd = next(position for position in snapshot.positions if position["asset"] == "ODD")
    assert odd["unpriced"] is True
    assert odd["unpriced_reason"] == "no_usdt_pair"
    assert snapshot.nav_usdt == Decimal("270.00000000")


def test_batch_pricing_failure_is_marked_pricing_unavailable(testnet_env, sdk):
    sdk.rest_api.account["balances"].append({"asset": "DUST", "free": "1", "locked": "0"})

    snapshot = BinanceSpotBroker("testnet", client=sdk).account_snapshot()

    dust = next(position for position in snapshot.positions if position["asset"] == "DUST")
    assert dust["unpriced"] is True
    assert dust["unpriced_reason"] == "pricing_unavailable"


def test_v1_only_account_skips_batch_pricing(testnet_env, sdk):
    BinanceSpotBroker("testnet", client=sdk).account_snapshot()

    batch_calls = [
        params
        for name, params in sdk.rest_api.calls
        if name == "ticker_book_ticker" and "symbol" not in params
    ]
    assert batch_calls == []
```

(Test 3 dựa vào việc `FakeRest.ticker_book_ticker` truy cập `kwargs["symbol"]` → `KeyError` khi gọi batch → `_external_mids` trả `None` → `pricing_unavailable`.)

- [ ] **Step 2: Chạy test để xác nhận fail**

Run: `uv run pytest tests/test_broker.py -q`
Expected: 4 test mới FAIL (`KeyError: 'external'`, `KeyError: 'unpriced_reason'`…); test cũ vẫn pass.

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/broker.py`, thay toàn bộ vòng `for balance in balances:` trong `account_snapshot` bằng:

```python
        entries: list[tuple[str, Decimal, Decimal, Decimal]] = []
        for balance in balances:
            asset = str(balance["asset"])
            free = Decimal(str(balance["free"]))
            locked = Decimal(str(balance["locked"]))
            total = free + locked
            if total > 0:
                entries.append((asset, free, locked, total))

        external_mids: dict[str, Decimal] | None = None
        if any(
            asset != "USDT" and f"{asset}USDT" not in V1_SYMBOLS
            for asset, _, _, _ in entries
        ):
            external_mids = self._external_mids()

        for asset, free, locked, total in entries:
            if asset == "USDT":
                nav += total
                free_usdt = free
                continue
            symbol = f"{asset}USDT"
            external = symbol not in V1_SYMBOLS
            if external:
                mid = (external_mids or {}).get(symbol)
            else:
                mid = self.latest_quote(symbol).mid
            if mid is None:
                reason = "pricing_unavailable" if external_mids is None else "no_usdt_pair"
                positions.append(
                    {
                        "asset": asset,
                        "symbol": symbol,
                        "free": str(free),
                        "locked": str(locked),
                        "total": str(total),
                        "mid_usdt": "0",
                        "value_usdt": "0",
                        "unpriced": True,
                        "unpriced_reason": reason,
                    }
                )
                continue
            value = total * mid
            nav += value
            position = {
                "asset": asset,
                "symbol": symbol,
                "free": str(free),
                "locked": str(locked),
                "total": str(total),
                "mid_usdt": str(mid),
                "value_usdt": str(value),
            }
            if external:
                position["external"] = True
            positions.append(position)
```

Thêm method mới ngay sau `latest_quote`:

```python
    def _external_mids(self) -> dict[str, Decimal] | None:
        try:
            payload = _unwrap(self._client.rest_api.ticker_book_ticker())
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        mids: dict[str, Decimal] = {}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            try:
                bid = Decimal(str(entry["bidPrice"]))
                ask = Decimal(str(entry["askPrice"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if bid > 0 and ask > bid:
                mids[str(entry["symbol"])] = (bid + ask) / Decimal("2")
        return mids
```

- [ ] **Step 4: Chạy test broker**

Run: `uv run pytest tests/test_broker.py -q`
Expected: PASS toàn bộ, gồm cả `test_unallowlisted_dust_is_recorded_unpriced_without_breaking_sync` (DUST giờ mang thêm `unpriced_reason="pricing_unavailable"`, các assertion cũ không đổi).

- [ ] **Step 5: Toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass.

- [ ] **Step 6: Commit**

```bash
git add src/crypto_desk/broker.py tests/test_broker.py
git commit -m "feat: batch-price non-allowlist spot assets into NAV"
```

---

### Task 3: Unpriced chỉ chặn BUY; SELL được phép; health cảnh báo `unpriced_asset`

**Sửa theo review [P1]:** KHÔNG bỏ hẳn chốt unpriced như bản plan đầu. Khi còn tài sản chưa định giá trị thật `U`, `max_gross` room và `usdt_reserve` room đều bị tính DƯ `0.2·U` (real max_gross room = `0.8·(nav+U) − (gross+U)` = computed − `0.2U`) — cho BUY đi qua là fail-open. Hành vi mới: BUY vẫn bị chặn khi còn bất kỳ position `unpriced` nào; SELL (REDUCE/EXIT — giảm rủi ro) được phép; health phát alert `unpriced_asset:{ASSET}`. Tính khả dụng vẫn đạt vì Task 2 đã định giá được đa số dust thật (có cặp USDT) → chúng không còn `unpriced`.

**Files:**
- Modify: `src/crypto_desk/execution.py` (giới hạn check unpriced trong `_pre_submit` vào side BUY)
- Modify: `src/crypto_desk/service.py` (giới hạn check unpriced trong `_create_ticket` vào ACCUMULATE, thêm alert trong `_health_alerts`)
- Test: `tests/test_execution.py` (GIỮ test chặn BUY hiện có, thêm test SELL được phép), `tests/test_service_cli.py` (thêm test health alert)

**Interfaces:**
- Consumes: key `"unpriced": True` trên position dict (Task 2).
- Produces: alert string mới `f"unpriced_asset:{asset}"` trong `health()["alerts"]`.

- [ ] **Step 1: Viết hai test fail (GIỮ NGUYÊN test `test_unpriced_spot_balance_blocks_submission` hiện có — BUY vẫn phải bị chặn)**

Thêm vào `tests/test_execution.py`:

```python
def test_sell_is_allowed_with_unpriced_dust_present(tmp_path):
    broker = FakeBroker("testnet")
    broker.positions = (
        {
            "asset": "BTC",
            "symbol": "BTCUSDT",
            "free": "0.00100000",
            "locked": "0",
            "total": "0.00100000",
            "mid_usdt": "100000",
            "value_usdt": "100",
        },
        {
            "asset": "AIRDROP",
            "symbol": "AIRDROPUSDT",
            "free": "1",
            "locked": "0",
            "total": "1",
            "mid_usdt": "0",
            "value_usdt": "0",
            "unpriced": True,
            "unpriced_reason": "no_usdt_pair",
        },
    )
    ticket = replace(
        make_ticket(),
        intent="REDUCE",
        side="SELL",
        risk_snapshot={"protection_list_client_order_id": "789"},
    )
    service, _, _ = make_service(
        tmp_path,
        testnet_enabled=True,
        broker=broker,
        ticket=ticket,
    )

    result = service.approve("ticket-1", actor="owner", channel="local")

    assert result.status == "FILLED"
```

Thêm vào `tests/test_service_cli.py` (sau các test health hiện có):

```python
def test_health_flags_unpriced_positions(tmp_path: Path):
    class UnpricedBroker(FakeBroker):
        def account_snapshot(self) -> PortfolioSnapshot:
            return PortfolioSnapshot(
                environment="testnet",
                nav_usdt=Decimal("10000"),
                free_usdt=Decimal("10000"),
                positions=(
                    {
                        "asset": "AIRDROP",
                        "symbol": "AIRDROPUSDT",
                        "free": "5",
                        "locked": "0",
                        "total": "5",
                        "mid_usdt": "0",
                        "value_usdt": "0",
                        "unpriced": True,
                        "unpriced_reason": "no_usdt_pair",
                    },
                ),
                open_orders=(),
                as_of=NOW.isoformat(),
            )

    settings = make_settings(tmp_path)
    store = Store(settings.database)
    service = CryptoDeskService(
        settings,
        store,
        broker=UnpricedBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.health()

    assert "unpriced_asset:AIRDROP" in result["alerts"]
```

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_execution.py::test_sell_is_allowed_with_unpriced_dust_present tests/test_service_cli.py::test_health_flags_unpriced_positions -q`
Expected: cả hai FAIL (SELL hiện bị chặn bởi `ValueError: Account contains unpriced Spot balances`; alert chưa tồn tại).

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/execution.py`, `_pre_submit`, thay:

```python
        if any(position.get("unpriced") for position in account.positions):
            raise ValueError("Account contains unpriced Spot balances")
```

bằng:

```python
        if ticket.side == "BUY" and any(
            position.get("unpriced") for position in account.positions
        ):
            raise ValueError("Account contains unpriced Spot balances")
```

Trong `src/crypto_desk/service.py`, `_create_ticket`, thay:

```python
        if any(position.get("unpriced") for position in portfolio.positions):
            return None
```

bằng:

```python
        if decision.action == "ACCUMULATE" and any(
            position.get("unpriced") for position in portfolio.positions
        ):
            return None
```

Trong `src/crypto_desk/service.py`, method `_health_alerts`, thêm vào ĐẦU vòng `for position in snapshot.positions:`:

```python
        for position in snapshot.positions:
            if position.get("unpriced"):
                alerts.append(f"unpriced_asset:{position.get('asset')}")
                continue
            symbol = str(position.get("symbol", ""))
```

(giữ nguyên phần thân còn lại của vòng lặp).

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass — gồm cả `test_unpriced_spot_balance_blocks_submission` (ticket mặc định là BUY nên vẫn bị chặn).

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/execution.py src/crypto_desk/service.py tests/test_execution.py tests/test_service_cli.py
git commit -m "fix: allow risk-reducing sells while unpriced dust blocks buys"
```

---

### Task 4: News — cô lập lỗi từng feed, lọc item future-dated, follow redirects

Một feed chết hiện raise xuyên qua `news()` làm hỏng evidence build của MỌI symbol. Ba sửa đổi (theo review [P2]):

1. Feed lỗi bị bỏ qua, feed sống vẫn đóng góp tin. Nếu TẤT CẢ feed chết → items rỗng → builder raise "No current news evidence" → NO_TRADE (fail-closed giữ nguyên).
2. Item có `published_at` trong tương lai bị loại tại chỗ — nếu giữ, `as_of = max(published)` vượt cutoff và `EvidenceBuilder` reject toàn bộ evidence ("evidence is from the future") vì một feed cấu hình sai giờ.
3. Client mặc định bật `follow_redirects=True` — httpx mặc định KHÔNG follow (đã xác minh), khiến feed trả 301/308 (ví dụ CoinDesk có `/` cuối) âm thầm cho ra body rỗng.

**Files:**
- Modify: `src/crypto_desk/data.py` (method `PublicDataClient.news`, `__init__`)
- Test: `tests/test_data.py`

**Interfaces:**
- Produces: `news()` giữ nguyên shape trả về (`Fetched` với `payload` là `list[dict]`, `source` là chuỗi các URL thành công nối bằng dấu phẩy; `as_of` luôn ≤ `fetched_at`). Không caller nào phải đổi.

- [ ] **Step 1: Viết helper + ba test fail**

Thêm vào `tests/test_data.py` (helper đặt gần đầu file, sau các hằng):

```python
def rss_xml(title: str, published: datetime) -> str:
    return (
        "<rss><channel><item>"
        f"<title>{title}</title>"
        f"<link>https://example.test/{title}</link>"
        f"<pubDate>{published.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        "</item></channel></rss>"
    )
```

Thêm test vào cuối file:

```python
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
```

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_data.py -q`
Expected: 3 test mới FAIL (`ConnectError` lan ra ngoài; item future không bị loại; `follow_redirects` đang `False`).

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/data.py`:

Trong `PublicDataClient.__init__`, thay `self.client = client or httpx.Client(timeout=10)` bằng:

```python
        self.client = client or httpx.Client(timeout=10, follow_redirects=True)
```

Thay toàn bộ method `news()`:

```python
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
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass (fixture `news.xml` trong `test_public_client_uses_only_fixed_public_endpoints_and_offline_fixtures` có pubDate quá khứ so với CUTOFF nên không bị lọc — nếu test này fail vì filter, kiểm tra pubDate của `tests/fixtures/news.xml` và điều chỉnh fixture về ngày quá khứ).

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/data.py tests/test_data.py
git commit -m "fix: isolate per-feed news failures and drop future-dated items"
```

---

### Task 5: News — cache 300 giây, KHÔNG cache thất bại toàn phần

`screen()`/`daily()` build evidence cho 4 symbol trên CÙNG một `PublicDataClient` → RSS bị fetch 4 lần giống hệt nhau. Cache kết quả `news()` với TTL 300s. **Theo review [P2]: chỉ cache khi có ít nhất một feed thành công** — cache cả kết quả rỗng khi mọi feed lỗi sẽ kéo dài NO_TRADE thêm 300s sau khi feed phục hồi.

**Files:**
- Modify: `src/crypto_desk/data.py` (hằng `NEWS_CACHE_SECONDS`, `PublicDataClient.__init__`, `news()`)
- Test: `tests/test_data.py`

**Interfaces:**
- Produces: `NEWS_CACHE_SECONDS = 300` (module-level, `data.py`). `news()` trả về CÙNG object `Fetched` trong cửa sổ cache; kết quả không có feed nào thành công không bao giờ được cache.

- [ ] **Step 1: Viết hai test fail**

Thêm vào cuối `tests/test_data.py`:

```python
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
```

(Test thứ hai so sánh tương đối `> first` thay vì đếm tuyệt đối để không vỡ khi Task 6 thêm retry.)

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_data.py::test_news_is_cached_within_a_run tests/test_data.py::test_total_feed_failure_is_not_cached -q`
Expected: test 1 FAIL tại `assert calls["count"] == 1` (thực tế là 2); test 2 PASS sẵn (giữ làm chốt hồi quy chống negative-cache).

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/data.py`:

Thêm hằng ngay dưới `COINGECKO_PUBLIC`:

```python
NEWS_CACHE_SECONDS = 300
```

Cuối `PublicDataClient.__init__` thêm:

```python
        self._news_cache: tuple[datetime, Fetched] | None = None
```

Đầu `news()` (ngay sau dòng `fetched_at = _aware(self.now())`) thêm:

```python
        if self._news_cache is not None:
            cached_at, cached = self._news_cache
            if (fetched_at - cached_at).total_seconds() < NEWS_CACHE_SECONDS:
                return cached
```

Cuối `news()`, thay `return Fetched(...)` bằng:

```python
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
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/data.py tests/test_data.py
git commit -m "perf: cache successful news fetches across a screen run"
```

---

### Task 6: Retry một lần cho public data GET

Lỗi transport thoáng qua hoặc 5xx trên MỘT trong ~10 endpoint làm cả symbol fail lượt chạy đó. Thêm retry đúng một lần (sleep 0.5s) cho GET public — an toàn vì toàn bộ là idempotent read.

**Files:**
- Modify: `src/crypto_desk/data.py` (thêm `import time`, method `PublicDataClient._get`, dùng trong `_json` và `news()`)
- Test: `tests/test_data.py`

**Interfaces:**
- Produces: `PublicDataClient._get(url, *, params=None, headers=None) -> httpx.Response` — retry 1 lần trên `httpx.TransportError` hoặc status ≥ 500, sau đó `raise_for_status()`.

- [ ] **Step 1: Viết hai test fail**

Thêm vào cuối `tests/test_data.py`:

```python
def test_transient_transport_error_is_retried_once(monkeypatch):
    monkeypatch.setattr("crypto_desk.data.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("transient")
        payload = json.loads(
            (FIXTURES / "binance_book_ticker.json").read_text(encoding="utf-8")
        )
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
```

Lưu ý: file test đã import `json`, `httpx`, `pytest` sẵn.

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_data.py::test_transient_transport_error_is_retried_once tests/test_data.py::test_server_error_is_retried_then_raised -q`
Expected: FAIL — test 1 vì `ConnectError` lan thẳng ra (chưa có retry và chưa có `crypto_desk.data.time`), test 2 vì `attempts == 1`.

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/data.py`:

Thêm `import time` vào khối import chuẩn (sau `import os`).

Thêm method vào `PublicDataClient` (trước `_json`):

```python
    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in (0, 1):
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                if attempt:
                    raise
                time.sleep(0.5)
                continue
            if response.status_code >= 500 and not attempt:
                time.sleep(0.5)
                continue
            response.raise_for_status()
            return response
        raise EvidenceError(f"unreachable retry state for {url}")
```

Trong `_json`, thay hai dòng gọi HTTP:

```python
        response = self._get(url, params=params, headers=headers)
```

(xóa dòng `response.raise_for_status()` cũ vì `_get` đã gọi).

Trong `news()` (đã sửa ở Task 4), thay:

```python
                response = self.client.get(url)
                response.raise_for_status()
```

bằng:

```python
                response = self._get(url)
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass (test `test_total_feed_failure_is_not_cached` vẫn xanh nhờ so sánh tương đối).

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/data.py tests/test_data.py
git commit -m "fix: retry transient public data failures once"
```

---

### Task 7: Committee — bỏ regex numeric-claim, bắt buộc đủ giá + entry lệch ≤2%

Regex bắt mọi con số trong văn LLM tạo NO_TRADE giả ("tăng 35%", "3 phiên"…) và cần whitelist hằng số thủ công. Thay bằng chốt tất định đúng chỗ tiền đi qua. **Sửa theo review [P1]:** validator phải yêu cầu đủ `entry/stop/target` cho MỌI actionable action (ACCUMULATE/REDUCE/EXIT), không chỉ ACCUMULATE — hiện REDUCE với position tồn tại và cả ba giá `None` lọt qua committee rồi nổ `AssertionError` tại `service._create_ticket` (assert nằm NGOÀI khối try chỉ bắt `ValueError`) làm crash `analyze()`. Kèm chốt phòng thủ ở service: thay assert bằng early-return.

**Files:**
- Modify: `src/crypto_desk/committee.py`
- Modify: `src/crypto_desk/service.py` (thay 2 assert bằng guard trong `_create_ticket`)
- Test: `tests/test_committee.py`

**Interfaces:**
- Produces: `MAX_ENTRY_DEVIATION = Decimal("0.02")` (module-level). `_call(..., snapshot_mid: Decimal, ...)` thay cho tham số `numeric_evidence`. `_validate_manager(decision, position_quantity, snapshot_mid)`.
- Xóa: `NUMBER_PATTERN`, `_validate_numeric_claims`, `_numeric_evidence`, `import re`.

- [ ] **Step 1: Thay test numeric-claim bằng ba test mới**

Trong `tests/test_committee.py`, XÓA toàn bộ hàm `test_numeric_claim_absent_from_evidence_is_rejected` (dòng ~200-216) và thêm vào vị trí đó:

```python
def test_hallucinated_entry_far_from_mid_is_rejected():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def far_entry(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["entry"] = "150000"
            response["stop"] = "140000"
            response["target"] = "160000"
        return response

    fake_llm.generate = far_entry

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "deviates" in result.decision.reason


def test_prose_percentages_no_longer_force_no_trade():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def prose(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["bull_case"] = "Khối lượng Spot tăng khoảng 35% so với tuần trước."
        return response

    fake_llm.generate = prose

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.action == "ACCUMULATE"


def test_reduce_with_position_but_missing_prices_is_rejected():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def reduce_without_prices(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["action"] = "REDUCE"
            response["entry"] = None
            response["stop"] = None
            response["target"] = None
        return response

    fake_llm.generate = reduce_without_prices

    result = CryptoCommittee(fake_llm).run(
        valid_snapshot(),
        position_quantity=Decimal("1"),
    )

    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
    assert "entry" in result.decision.reason
```

- [ ] **Step 2: Chạy để xác nhận trạng thái**

Run: `uv run pytest tests/test_committee.py -q`
Expected: cả ba test mới FAIL (entry 150000 hiện bị chặn với reason "numeric claim" chứ không phải "deviates"; "35%" gây NO_TRADE; REDUCE thiếu giá hiện đi qua committee với action REDUCE).

- [ ] **Step 3: Cài đặt committee**

Trong `src/crypto_desk/committee.py`:

1. Xóa `import re` và dòng `NUMBER_PATTERN = re.compile(...)`.
2. Thêm hằng dưới `SPECIALISTS`:

```python
MAX_ENTRY_DEVIATION = Decimal("0.02")
```

3. Trong `run()`: xóa dòng `numeric_evidence = self._numeric_evidence(snapshot)`; trong CẢ BA chỗ gọi `self._call(...)` (specialists, debate, manager) thay `numeric_evidence=numeric_evidence,` bằng `snapshot_mid=snapshot.binance_mid,`.
4. Sửa chữ ký và thân `_call`:

```python
    def _call(
        self,
        *,
        stage: str,
        model: str,
        response_model: type[AnalystReport] | type[ManagerDecision],
        system_prompt: str,
        payload: dict[str, Any],
        valid_evidence_ids: tuple[str, ...],
        snapshot_mid: Decimal,
        position_quantity: Decimal = Decimal("0"),
    ) -> AnalystReport | ManagerDecision:
        last_error = "invalid structured output"
        for _attempt in range(2):
            try:
                raw = self.llm.generate(
                    stage=stage,
                    model=model,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    payload=payload,
                )
                parsed = (
                    raw if isinstance(raw, response_model) else response_model.model_validate(raw)
                )
                self._validate_evidence_ids(
                    parsed.evidence_ids,
                    valid_evidence_ids,
                )
                if isinstance(parsed, ManagerDecision):
                    self._validate_manager(parsed, position_quantity, snapshot_mid)
                return parsed
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = str(exc)
        raise CommitteeOutputError(f"{stage} structured output rejected: {last_error}")
```

5. Sửa `_validate_manager` (check position TRƯỚC check giá để giữ nguyên message của test `test_reduce_without_position_is_forced_to_no_trade`):

```python
    @staticmethod
    def _validate_manager(
        decision: ManagerDecision,
        position_quantity: Decimal,
        snapshot_mid: Decimal,
    ) -> None:
        if decision.action in {"REDUCE", "EXIT"} and position_quantity <= 0:
            raise ValueError(f"{decision.action} requires an existing position")
        if decision.action not in {"ACCUMULATE", "REDUCE", "EXIT"}:
            return
        if None in (decision.entry, decision.stop, decision.target):
            raise ValueError(f"{decision.action} requires entry, stop and target")
        assert decision.entry is not None
        assert decision.stop is not None
        assert decision.target is not None
        if not decision.stop < decision.entry < decision.target:
            raise ValueError(f"{decision.action} requires stop < entry < target")
        if abs(decision.entry - snapshot_mid) / snapshot_mid > MAX_ENTRY_DEVIATION:
            raise ValueError("entry price deviates more than 2% from Binance mid")
```

6. Xóa toàn bộ hai staticmethod `_validate_numeric_claims` và `_numeric_evidence`.

- [ ] **Step 4: Chốt phòng thủ ở service**

Trong `src/crypto_desk/service.py`, `_create_ticket`, thay:

```python
        assert decision.entry is not None
        assert decision.stop is not None
```

bằng:

```python
        if decision.entry is None or decision.stop is None:
            return None
```

(Không cần test riêng — đường này không còn tới được qua committee sau Step 3; guard chỉ chống committee tùy biến sai.)

- [ ] **Step 5: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass (FakeLLM mặc định trả entry 100000 = mid → lệch 0%; test `test_reduce_without_position_is_forced_to_no_trade` vẫn nhận message "position"; test `test_invalid_accumulate_price_order_retries_then_no_trade` vẫn khớp "stop < entry < target").

- [ ] **Step 6: Commit**

```bash
git add src/crypto_desk/committee.py src/crypto_desk/service.py tests/test_committee.py
git commit -m "fix: require full prices for actionable decisions, drop prose regex"
```

---

### Task 8: Gom hằng số mainnet cap / graduation / TTL về config

`25 USDT`, `20 chains`, `30 phút` đang bị nhân bản độc lập ở risk.py, execution.py, cli.py. Gom về `config.py` làm nguồn duy nhất; đồng thời validate `ticket_ttl_minutes ≤ 30` để loại mâu thuẫn "config nói 45 nhưng execution âm thầm ép 30".

**Files:**
- Modify: `src/crypto_desk/config.py`, `src/crypto_desk/risk.py`, `src/crypto_desk/execution.py`, `src/crypto_desk/cli.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (module `crypto_desk.config`):
  - `MAINNET_GRADUATION_CHAINS: int = 20`
  - `HARD_MAINNET_CAP_USDT: Decimal = Decimal("25")`
  - `MAX_TICKET_TTL_MINUTES: int = 30`
- Xóa: `INITIAL_MAINNET_CAP_USDT` trong `risk.py` (không test nào import trực tiếp — đã xác minh bằng grep).

- [ ] **Step 1: Viết test config fail**

Thêm vào `tests/test_config.py`:

```python
def test_config_rejects_ticket_ttl_above_30_minutes(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
symbols: [BTCUSDT]
risk:
  ticket_ttl_minutes: 45
""",
    )

    with pytest.raises(ValueError, match="ticket_ttl_minutes"):
        load_settings(config)
```

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_config.py::test_config_rejects_ticket_ttl_above_30_minutes -q`
Expected: FAIL (45 hiện được chấp nhận vì chỉ check `<= 0`).

- [ ] **Step 3: Cài đặt**

`src/crypto_desk/config.py` — thêm ngay dưới `V1_SYMBOLS`:

```python
MAINNET_GRADUATION_CHAINS = 20
HARD_MAINNET_CAP_USDT = Decimal("25")
MAX_TICKET_TTL_MINUTES = 30
```

Trong `_validate`, thay:

```python
    if settings.risk.ticket_ttl_minutes <= 0:
        raise ValueError("ticket_ttl_minutes must be positive")
```

bằng:

```python
    if not 0 < settings.risk.ticket_ttl_minutes <= MAX_TICKET_TTL_MINUTES:
        raise ValueError("ticket_ttl_minutes must be between 1 and 30")
```

`src/crypto_desk/risk.py` — đổi import và xóa hằng cục bộ:

```python
from .config import HARD_MAINNET_CAP_USDT, MAINNET_GRADUATION_CHAINS, RiskSettings
```

(xóa dòng `INITIAL_MAINNET_CAP_USDT = Decimal("25")`).

Trong `size_buy`, thay khối rooms mainnet:

```python
    if environment == "mainnet" and completed_mainnet_chains < MAINNET_GRADUATION_CHAINS:
        rooms["mainnet_cap"] = min(mainnet_order_cap_usdt, HARD_MAINNET_CAP_USDT)
```

và khối kiểm tra cuối:

```python
    if (
        environment == "mainnet"
        and completed_mainnet_chains < MAINNET_GRADUATION_CHAINS
        and notional > min(mainnet_order_cap_usdt, HARD_MAINNET_CAP_USDT)
    ):
        raise ValueError("Order exceeds active Mainnet cap")
```

`src/crypto_desk/execution.py` — mở rộng import config:

```python
from .config import (
    HARD_MAINNET_CAP_USDT,
    MAINNET_GRADUATION_CHAINS,
    MAX_TICKET_TTL_MINUTES,
    Settings,
)
```

Trong `_pending_ticket`, thay `timedelta(minutes=30)` bằng `timedelta(minutes=MAX_TICKET_TTL_MINUTES)`.

Trong `_pre_submit`, thay khối cap mainnet:

```python
        if (
            ticket.environment == "mainnet"
            and self.store.completed_mainnet_chains() < MAINNET_GRADUATION_CHAINS
            and current_notional > HARD_MAINNET_CAP_USDT
        ):
            raise ValueError("Mainnet ticket exceeds the active 25 USDT cap")
```

`src/crypto_desk/cli.py` — đổi import `from .config import Settings, load_settings` thành:

```python
from .config import MAINNET_GRADUATION_CHAINS, Settings, load_settings
```

và trong `doctor_report` thay `"initial_cap_active": store.completed_mainnet_chains() < 20,` bằng:

```python
            "initial_cap_active": store.completed_mainnet_chains() < MAINNET_GRADUATION_CHAINS,
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass — thuần refactor trừ validation TTL mới.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/config.py src/crypto_desk/risk.py src/crypto_desk/execution.py src/crypto_desk/cli.py tests/test_config.py
git commit -m "refactor: single-source mainnet cap and ticket TTL constants"
```

---

### Task 9: Confirmation code chấp nhận bucket liền trước

Code hiện chỉ hợp lệ trong bucket 300s hiện tại → code sinh lúc 09:04:59 chết lúc 09:05:00. Chấp nhận thêm bucket liền trước: hiệu lực thực tế 5–10 phút.

**Files:**
- Modify: `src/crypto_desk/execution.py` (hàm `confirmation_code`, `verify_confirmation_code`, thêm `_code_for_bucket`)
- Modify: `README.md` (dòng mô tả "còn hạn năm phút")
- Test: `tests/test_execution.py`

**Interfaces:**
- Chữ ký public không đổi: `confirmation_code(secret, ticket_id, now)`, `verify_confirmation_code(secret, ticket_id, supplied, now)`.
- Produces: `CODE_BUCKET_SECONDS = 300` (module-level, `execution.py`).

- [ ] **Step 1: Viết test mới + cập nhật test cũ**

Thêm vào `tests/test_execution.py` (sau `test_confirmation_code_is_ticket_bound_and_five_minute_scoped`):

```python
def test_confirmation_code_from_previous_bucket_is_accepted():
    issued = NOW - timedelta(seconds=1)
    code = confirmation_code(SECRET, "ticket-1", issued)

    assert verify_confirmation_code(SECRET, "ticket-1", code, NOW + timedelta(seconds=1))
    assert not verify_confirmation_code(SECRET, "ticket-1", code, NOW + timedelta(seconds=301))
```

Trong `test_confirmation_code_is_ticket_bound_and_five_minute_scoped`, assertion cuối hiện dùng `NOW + timedelta(minutes=5)` cho `verify_confirmation_code` — đổi thành `NOW + timedelta(minutes=10)` (với chấp nhận bucket liền trước, +5 phút vẫn hợp lệ theo thiết kế mới; +10 phút thì không):

```python
    assert not verify_confirmation_code(
        SECRET,
        "ticket-1",
        first,
        NOW + timedelta(minutes=10),
    )
```

(giữ nguyên assertion `first != confirmation_code(SECRET, "ticket-1", NOW + timedelta(minutes=5))` — hàm sinh code vẫn theo bucket hiện tại.)

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_execution.py::test_confirmation_code_from_previous_bucket_is_accepted -q`
Expected: FAIL tại assertion đầu (code bucket trước hiện bị từ chối).

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/execution.py`, thay hai hàm `confirmation_code` và `verify_confirmation_code` bằng:

```python
CODE_BUCKET_SECONDS = 300


def _code_for_bucket(secret: str, ticket_id: str, bucket: int) -> str:
    digest = hmac.new(
        secret.encode(),
        f"{ticket_id}:{bucket}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def confirmation_code(
    secret: str,
    ticket_id: str,
    now: datetime,
) -> str:
    if not secret:
        raise ValueError("LIVE_CONFIRMATION_SECRET is required")
    bucket = int(now.astimezone(UTC).timestamp()) // CODE_BUCKET_SECONDS
    return _code_for_bucket(secret, ticket_id, bucket)


def verify_confirmation_code(
    secret: str,
    ticket_id: str,
    supplied: str,
    now: datetime,
) -> bool:
    bucket = int(now.astimezone(UTC).timestamp()) // CODE_BUCKET_SECONDS
    current = hmac.compare_digest(_code_for_bucket(secret, ticket_id, bucket), supplied)
    previous = hmac.compare_digest(_code_for_bucket(secret, ticket_id, bucket - 1), supplied)
    return current | previous
```

(dùng `|` thay vì `or` để cả hai phép so sánh luôn được thực thi — tránh timing side-channel.)

Trong `README.md`, thay đoạn:

```
3. approval từ Telegram user trong allowlist kèm confirmation code còn hạn năm
   phút và HMAC proof do trusted Telegram ingress tạo.
```

bằng:

```
3. approval từ Telegram user trong allowlist kèm confirmation code còn hạn
   (hợp lệ trong bucket 5 phút hiện tại hoặc liền trước, tối đa 10 phút) và
   HMAC proof do trusted Telegram ingress tạo.
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/execution.py tests/test_execution.py README.md
git commit -m "fix: accept previous confirmation-code bucket"
```

---

### Task 10: Resume ticket APPROVED bị kẹt — mọi gate chạy lại, có bằng chứng test

Crash giữa `record_approval` và `save_submission` để lại ticket `APPROVED` không có submission: không re-approve được (đòi PENDING), không reconcile được (đòi submission) — kẹt vĩnh viễn dù chưa gửi lệnh nào. Cho phép `approve` chạy tiếp ("resume") đúng trường hợp này: mọi gate và `_pre_submit` chạy lại đầy đủ, chỉ bỏ qua `record_approval` (đã có bản ghi). **Theo review [P2]:** resume phải xác minh approval record tồn tại với decision `APPROVE`, và phải có test chứng minh từng gate vẫn chặn khi resume (hết hạn, lệch giá, Mainnet thiếu code, thiếu approval record).

**Files:**
- Modify: `src/crypto_desk/execution.py` (thêm `_approvable_ticket`, sửa `approve` và `_pending_ticket`)
- Test: `tests/test_execution.py`

**Interfaces:**
- Produces: `ExecutionService._approvable_ticket(ticket_id) -> tuple[TradeTicket, bool]` — phần tử thứ hai là `resuming`. `reject` vẫn dùng `_pending_ticket` (không cho reject ticket đã APPROVED — bản ghi approval đã tồn tại).

- [ ] **Step 1: Viết sáu test**

Thêm vào `tests/test_execution.py`:

```python
def test_ticket_stuck_in_approved_without_submission_can_resume(tmp_path):
    service, store, broker = make_service(tmp_path, testnet_enabled=True)
    store.record_approval("ticket-1", actor="owner", channel="local", status="APPROVED")

    result = service.approve("ticket-1", actor="owner", channel="local")

    assert result.status == "SUBMITTED"
    assert broker.place_calls == 1
    assert store.submission("ticket-1") is not None
    assert store.approval("ticket-1")["decision"] == "APPROVE"


def test_resume_is_blocked_once_submission_exists(tmp_path):
    service, store, _ = make_service(tmp_path, testnet_enabled=True)
    service.approve("ticket-1", actor="owner", channel="local")

    with pytest.raises(ValueError, match="not PENDING|already submitted"):
        service.approve("ticket-1", actor="owner", channel="local")


def test_resume_is_blocked_when_ticket_expired(tmp_path):
    service, store, _ = make_service(
        tmp_path,
        testnet_enabled=True,
        ticket=make_ticket(created_at=NOW - timedelta(minutes=31)),
    )
    store.record_approval("ticket-1", actor="owner", channel="local", status="APPROVED")

    with pytest.raises(ValueError, match="expired"):
        service.approve("ticket-1", actor="owner", channel="local")


def test_resume_reruns_price_deviation_gate(tmp_path):
    broker = FakeBroker("testnet")
    broker.mid = Decimal("101000")
    service, store, _ = make_service(tmp_path, testnet_enabled=True, broker=broker)
    store.record_approval("ticket-1", actor="owner", channel="local", status="APPROVED")

    with pytest.raises(ValueError, match="deviation"):
        service.approve("ticket-1", actor="owner", channel="local")


def test_mainnet_resume_still_requires_fresh_code_and_proof(tmp_path):
    service, store, _ = make_service(
        tmp_path,
        environment="mainnet",
        ticket_environment="mainnet",
        live_enabled=True,
    )
    store.record_approval("ticket-1", actor="owner", channel="telegram", status="APPROVED")

    with pytest.raises(ValueError, match="confirmation"):
        service.approve("ticket-1", actor="owner", channel="telegram", code="000000")


def test_resume_requires_matching_approval_record(tmp_path):
    service, store, _ = make_service(tmp_path, testnet_enabled=True)
    store.set_ticket_status("ticket-1", "APPROVED")

    with pytest.raises(ValueError, match="approval"):
        service.approve("ticket-1", actor="owner", channel="local")
```

- [ ] **Step 2: Chạy để xác nhận trạng thái**

Run: `uv run pytest tests/test_execution.py -k "resume or stuck" -q`
Expected: `test_ticket_stuck_in_approved_without_submission_can_resume` FAIL (`Ticket ticket-1 is not PENDING`); các test chặn-resume cũng FAIL với message "not PENDING" (chưa phân biệt lý do) — sau cài đặt phải FAIL đúng message chuyên biệt; `test_resume_is_blocked_once_submission_exists` PASS sẵn (chốt hồi quy).

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/execution.py`:

1. Thay method `_pending_ticket` bằng hai method:

```python
    def _approvable_ticket(self, ticket_id: str) -> tuple[TradeTicket, bool]:
        ticket = self.store.ticket(ticket_id)
        resuming = ticket.status == "APPROVED" and self.store.submission(ticket_id) is None
        if ticket.status != "PENDING" and not resuming:
            raise ValueError(f"Ticket {ticket_id} is not PENDING")
        if resuming and self.store.approval(ticket_id)["decision"] != "APPROVE":
            raise ValueError(f"Ticket {ticket_id} approval is not APPROVE")
        now = self._now()
        created = datetime.fromisoformat(ticket.created_at).astimezone(UTC)
        expires = datetime.fromisoformat(ticket.expires_at).astimezone(UTC)
        if created > now:
            raise ValueError("Ticket creation time is in the future")
        if now >= expires or now >= created + timedelta(minutes=MAX_TICKET_TTL_MINUTES):
            raise ValueError(f"Ticket {ticket_id} is expired")
        return ticket, resuming

    def _pending_ticket(self, ticket_id: str) -> TradeTicket:
        ticket, resuming = self._approvable_ticket(ticket_id)
        if resuming:
            raise ValueError(f"Ticket {ticket_id} is not PENDING")
        return ticket
```

(`store.approval()` tự raise `ValueError("Ticket ... has no approval")` khi thiếu record — test `test_resume_requires_matching_approval_record` dựa vào đó. `MAX_TICKET_TTL_MINUTES` được import ở Task 8; nếu task này chạy trước Task 8, tạm dùng `timedelta(minutes=30)` và Task 8 sẽ thay.)

2. Trong `approve`, thay dòng đầu `ticket = self._pending_ticket(ticket_id)` bằng:

```python
        ticket, resuming = self._approvable_ticket(ticket_id)
```

3. Vẫn trong `approve`, bọc lời gọi `record_approval` (nhánh submit thật, ngay trước `save_submission`):

```python
        if not resuming:
            self.store.record_approval(
                ticket.id,
                actor=actor,
                channel=channel,
                status="APPROVED",
            )
```

(nhánh dry-run `APPROVED_DRY_RUN` giữ nguyên — resume với execution tắt sẽ báo lỗi "already has an approval decision", chấp nhận được.)

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass — 6 test mới xanh với đúng message chuyên biệt (expired / deviation / confirmation / approval), reject và các test expiry dùng `_pending_ticket` không đổi hành vi.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/execution.py tests/test_execution.py
git commit -m "fix: resume approved tickets stranded without submission"
```

---

### Task 11: Khả kiến news_feeds — doctor, config mẫu, README

Config mẫu để `news_feeds: []` khiến mọi `analyze` NO_TRADE vĩnh viễn mà không có cảnh báo nào. Doctor phải nói thẳng điều đó; config mẫu và README phải hướng dẫn. **Theo review [P2]: URL CoinDesk KHÔNG có dấu `/` cuối** — bản có `/` trả HTTP 308 (đã xác minh bằng curl: `/` cuối → 308, không `/` → 200).

**Files:**
- Modify: `src/crypto_desk/cli.py` (hàm `doctor_report`)
- Modify: `config.example.yaml`, `README.md`
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Produces: key mới trong doctor report: `report["news"] = {"feeds_configured": int, "analyze_possible": bool}`.

- [ ] **Step 1: Viết test fail**

Trong `tests/test_service_cli.py`, thêm `doctor_report` vào import hiện có từ `crypto_desk.cli`:

```python
from crypto_desk.cli import _hermes_installed, app, doctor_report
```

Thêm test:

```python
def test_doctor_reports_news_feed_visibility(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    report = doctor_report(settings, store, online=False)

    assert report["news"] == {"feeds_configured": 0, "analyze_possible": False}
```

- [ ] **Step 2: Chạy để xác nhận fail**

Run: `uv run pytest tests/test_service_cli.py::test_doctor_reports_news_feed_visibility -q`
Expected: FAIL với `KeyError: 'news'`.

- [ ] **Step 3: Cài đặt**

Trong `src/crypto_desk/cli.py`, hàm `doctor_report`, thêm vào dict `report` (sau key `"telegram"`):

```python
        "news": {
            "feeds_configured": len(settings.news_feeds),
            "analyze_possible": bool(settings.news_feeds),
        },
```

Trong `config.example.yaml`, thay dòng `news_feeds: []` bằng:

```yaml
# analyze/daily yêu cầu ít nhất một RSS feed có tin trong 48 giờ gần nhất;
# để trống news_feeds thì mọi phiên phân tích sẽ trả về NO_TRADE.
# Ví dụ (chú ý: không có dấu "/" cuối URL CoinDesk — bản có "/" trả HTTP 308):
# news_feeds:
#   - https://www.coindesk.com/arc/outboundfeeds/rss
#   - https://cointelegraph.com/rss
news_feeds: []
```

Trong `README.md`, ngay sau đoạn "Đặt `OPENAI_API_KEY`, tùy chọn `COINGECKO_DEMO_API_KEY`, danh sách RSS trong `config.yaml`, ..." thêm câu:

```
Bắt buộc cấu hình ít nhất một RSS feed hoạt động: `news_feeds` rỗng hoặc toàn
feed chết khiến mọi lệnh `analyze` trả về `NO_TRADE` theo thiết kế fail-closed.
```

- [ ] **Step 4: Chạy toàn suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: tất cả pass (test doctor hiện có `test_json_doctor_reports_secret_presence_without_values` chỉ assert các key sẵn có nên không vỡ).

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/cli.py config.example.yaml README.md tests/test_service_cli.py
git commit -m "docs: surface news feed requirement in doctor and config"
```

---

## Thứ tự và phụ thuộc

1. Task 1 (env) trước tất cả.
2. Task 2 → Task 3 (execution/service tiêu thụ position `external`/`unpriced_reason` mới).
3. Task 4 → Task 5 → Task 6 (cùng sửa `news()`/`_get` trong `data.py`, theo đúng thứ tự này).
4. Task 7, 8, 9, 10, 11 độc lập với nhau (Task 10 dùng `MAX_TICKET_TTL_MINUTES` của Task 8 — nếu đảo thứ tự, dùng giá trị `30` tạm như ghi chú trong task).

## Định nghĩa hoàn thành

- `uv run pytest -q`: toàn bộ test pass (110 test gốc trừ các test được viết lại có chủ đích + các test mới).
- `uv run ruff check .` và `uv run ruff format --check .` sạch.
- `uv lock --check` sạch (không đổi dependency).
- Hành vi Mainnet gates không đổi: các test `test_mainnet_requires_all_three_gates`, `test_mainnet_is_blocked_when_binance_env_gate_is_absent`, `test_claimed_telegram_channel_without_trusted_proof_is_blocked` pass nguyên trạng.
- BUY với tài sản chưa định giá vẫn bị chặn: `test_unpriced_spot_balance_blocks_submission` pass nguyên trạng.
