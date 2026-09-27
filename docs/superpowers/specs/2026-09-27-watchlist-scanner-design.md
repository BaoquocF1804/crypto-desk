# Máy quét watchlist bằng model rẻ — Design

**Ngày:** 2026-09-27 · **Trạng thái:** chờ duyệt

## Mục tiêu

Tìm tối đa 10 đồng ngoài allowlist có evidence mạnh cho một thesis Spot long 20 ngày và đưa vào watchlist. Committee đầy đủ tự chạy cho 3 đồng điểm cao nhất; 7 đồng còn lại người dùng tự chọn phân tích bằng `desk analyze SYMBOL --research`. Mọi run nghiên cứu được chấm điểm riêng để biết máy quét chọn có tốt không. Toàn bộ **chỉ phục vụ nghiên cứu**: watchlist không bao giờ sinh ticket hay lệnh.

## Quyết định đã chốt với người dùng

| Câu hỏi | Chốt |
|---|---|
| Đồng tìm được dùng để làm gì | Chỉ nghiên cứu. Allowlist 5 đồng giữ nguyên. Muốn giao dịch đồng nào thì phải sửa code (`V1_SYMBOLS`, `coingecko_ids`, `KNOWN_SYMBOLS` của web) rồi mới thêm vào `symbols` |
| Phạm vi quét | Tự động top 30 cặp USDT theo volume 24h, đã lọc |
| Ngân sách | Flash chọn tối đa 10 đồng vào watchlist; committee tự chạy cho 3 đồng điểm cao nhất |
| 7 đồng còn lại | Người dùng tự phân tích từng đồng bằng `desk analyze SYMBOL --research` |
| Lịch chạy | Chỉ lệnh tay (`desk scan`) |
| Hiển thị | CLI, report.md, và mục watchlist trên dashboard |
| Cách quét | Hướng A: code tính chỉ số, Gemini Flash xếp hạng cả bảng trong 1 lần gọi |

## Bất biến

1. Run nghiên cứu **không bao giờ** tạo ticket. Có ba lớp chặn độc lập:
   - `_create_ticket` từ chối mọi decision có `research_only` (do `analyze(research=True)` đánh dấu) hoặc symbol ngoài allowlist;
   - `_pre_submit` của execution vẫn từ chối symbol ngoài allowlist (đã có);
   - `_validate_symbol` của broker từ chối symbol ngoài `V1_SYMBOLS` (đã có).
2. `V1_SYMBOLS`, `settings.symbols` và đường thực thi không đổi.
3. Mọi con số trong bảng quét do code tính. Model chỉ xếp hạng và giải thích, không được đưa ra số mới.
4. Lượt quét lỗi (khám phá phạm vi hoặc xếp hạng) giữ nguyên watchlist.

## Kiến trúc

| Đơn vị | Trách nhiệm | Phụ thuộc |
|---|---|---|
| `scanner.py` (mới) | Khám phá phạm vi → tính chỉ số → xếp hạng → trả `ScanResult` | `PublicDataClient`, `StructuredClient`, `indicators`, `data.price_structure` |
| `data.py` | Thêm 4 hàm đọc public cho toàn thị trường; `EvidenceBuilder.build` nhận thêm `coingecko_id` tuỳ chọn | — |
| `store.py` | Schema v4: bảng `watchlist` | SQLite |
| `service.py` | `scan()` điều phối; `analyze(..., research=True)`; reflection có thêm `cohort` | scanner, store, committee |
| `scorecard.py` | Gộp nhóm theo (benchmark, cohort) | — |
| `cli.py` | `desk scan`, `desk watchlist`, `desk analyze SYMBOL --research` | service |
| `dashboard.py` + `web/` | Mục `watchlist` trong snapshot và trên giao diện | store |

## Phần 1 — Luồng quét (đã duyệt)

### 1.1 Khám phá phạm vi (4 request, không gọi LLM)

Các hàm mới trong `PublicDataClient`:
- `ticker_24h_all()`: `GET /api/v3/ticker/24hr`, không truyền symbol.
- `spot_exchange_info_all()`: `GET /api/v3/exchangeInfo?permissions=SPOT`.
- `futures_exchange_info()`: `GET /fapi/v1/exchangeInfo`.
- `coingecko_markets()`: `GET /coins/markets?vs_currency=usd&per_page=250&page=1`.

Một đồng được giữ khi thoả **tất cả**:
- Quote là `USDT`, `status == "TRADING"`, `isSpotTradingAllowed`, `ocoAllowed`, `otoAllowed`.
- Có perpetual USDⓈ-M cùng symbol, đang `TRADING`.
- Base asset không thuộc `NON_CRYPTO_BASES`: `USDC, FDUSD, TUSD, USDP, DAI, BUSD, USDE, USD1, BFUSD, XUSD, RLUSD, EUR, EURI, AEUR, PAXG, XAUT`.
- Có CoinGecko id. Mỗi ticker lấy id đầu tiên trong danh sách `coins/markets`, vốn đã xếp theo vốn hoá giảm dần. Map sai ticker trùng tên sẽ bị bộ kiểm tra lệch giá Binance/CoinGecko 0.5% sẵn có loại ở bước committee.
- `quoteVolume ≥ 20 000 000` USDT.
- Không thuộc `settings.symbols`.

Sắp xếp theo `quoteVolume` giảm dần, lấy `UNIVERSE_SIZE = 30`.

Đo ngày 27/09: bộ lọc trên cho 22 đồng, chưa chạm mức 30. Hạ `quoteVolume` xuống 10 000 000 thì đủ 30.

### 1.2 Tính chỉ số (khoảng 4 request mỗi đồng, cộng 1 request dùng chung)

- **Dùng chung cho tất cả:** `GET /fapi/v1/premiumIndex` không truyền symbol, trả funding của mọi đồng. Funding được đọc riêng cho từng đồng, nên một dòng hỏng chỉ loại đồng của nó.
- **Mỗi đồng:**
  - nến ngày `limit=121`, chỉ giữ nến đã đóng;
  - `openInterestHist` 1h × 25;
  - `globalLongShortAccountRatio` 1h × 500;
  - `takerlongshortRatio` 1h × 24.

`ScanFeatures` (dataclass frozen), mọi giá trị là `Decimal` đã làm tròn, hoặc `None` khi không đủ lịch sử:

| Trường | Cách tính |
|---|---|
| `symbol`, `quote_volume_musd` | Ticker 24h, đơn vị triệu USDT |
| `change_20d_pct` | close / close 20 phiên trước − 1 |
| `ema20_gap_pct` | close / EMA20 − 1 |
| `ema20_above_ema50` | bool |
| `atr1d_pct` | ATR14 ngày / close |
| `range_position_20d`, `range_position_55d` | (close − low_N) / (high_N − low_N) × 100 |
| `support_distance_atr` | (close − swing support gần nhất dưới giá, hoặc low_20d) / ATR |
| `resistance_distance_atr` | (swing resistance gần nhất trên giá − close) / ATR; `None` khi không còn kháng cự |
| `long_short_pctile_20d` | Như evidence hiện tại |
| `taker_buy_sell_24h`, `oi_change_24h_pct`, `funding_rate` | Như evidence hiện tại |

Đồng nào lỗi dữ liệu (HTTP, thiếu nến, số không hợp lệ) thì bị loại và ghi `dropped: {symbol: lý do}`, lượt quét vẫn chạy tiếp.

### 1.3 Xếp hạng bằng Gemini Flash (1 lần gọi)

- **Model:** `settings.models.quick`, `thinking = settings.models.quick_thinking`, qua cùng `StructuredClient` của committee.
- **System prompt:** `BASE_OUTPUT_CONTRACT` (bảng quét là dữ liệu không tin cậy, không bịa số) cộng vai trò:
  - xếp hạng sức mạnh evidence cho thesis Spot long 20 ngày;
  - xu hướng đồng thuận;
  - khoảng trống tới kháng cự so với khoảng cách xuống hỗ trợ tính bằng ATR, tức có khả năng đạt R:R ≥ 1.5 với stop ≥ 1 ATR ngày;
  - vị thế không crowded theo percentile;
  - dòng tiền 24h ủng hộ;
  - chọn **tối đa 10, mạnh nhất trước, được phép ít hơn hoặc 0**.
- **Payload:** `{"horizon_days": 20, "fields": {tên: mô tả}, "candidates": [ScanFeatures...]}`.
- **Schema:**
  - `ScanPick`: `symbol`, `evidence_score` (0–10), `thesis` (≤ 400 ký tự), `supporting_fields` (1–6 tên trường).
  - `ScanRanking`: `picks` (≤ 10), `summary` (≤ 600 ký tự).
- **Code kiểm tra:** symbol phải có trong `candidates`, không trùng, `supporting_fields` là tên trường có thật. Sai thì thử lại 1 lần kèm lý do; vẫn sai hoặc provider lỗi thì lượt quét dừng với lỗi. Artifact bảng chỉ số vẫn được ghi để chẩn đoán.
- Picks được sắp theo `evidence_score` giảm dần; điểm bằng nhau giữ thứ tự model trả về.

### 1.4 Artifact

`artifacts/crypto/scans/<YYYY-MM-DD>/<scan_id>.json` chứa: `scan_id`, `started_at`, `universe` (symbol, coingecko_id, volume), `dropped`, `features`, `ranking` (hoặc `error`), `run_ids`.

## Phần 2 — Watchlist, phân tích nghiên cứu, chấm điểm

### 2.1 Bảng `watchlist` (Store schema v4)

```sql
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY,
  coingecko_id TEXT NOT NULL,
  first_added_at TEXT NOT NULL,
  last_picked_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  scan_id TEXT NOT NULL,
  payload TEXT NOT NULL   -- {"evidence_score", "thesis", "supporting_fields"}
);
```

Migration theo đúng mẫu v2/v3 hiện có (`version < 4`).

Methods:
- `upsert_watchlist(symbol, coingecko_id, scan_id, picked_at, expires_at, payload)`: giữ `first_added_at` cũ.
- `active_watchlist(now, limit)`: `expires_at > now`, sắp theo `last_picked_at` giảm dần rồi `evidence_score` giảm dần (các pick của một lượt quét có cùng `last_picked_at`), lấy `limit` hàng đầu.
- `watchlist_entry(symbol)`.
- `watchlist_symbols()`: mọi symbol từng có trong bảng. Hàng hết hạn **không bị xoá**, vì reflection vẫn cần chấm chúng.

Run gần nhất của một đồng đọc từ `research_runs` (`latest_run`, `latest_valid_run`), không lưu lại trong watchlist. Lưu lại thì sẽ lệch ngay khi người dùng phân tích thủ công.

Hằng số:
- `WATCHLIST_TTL_DAYS = 7`.
- `WATCHLIST_MAX_ACTIVE = 10`: số entry tối đa CLI và dashboard hiển thị. `MAX_PICKS` của máy quét lấy đúng giá trị này, nên pick của lượt mới nhất luôn hiện đủ.
- `AUTO_ANALYZE_PICKS = 3`.

Một đồng được chọn lại thì làm mới hạn, điểm và thesis.

### 2.2 `CryptoDeskService.scan()`

1. Gọi scanner. Lỗi thì ghi artifact có `error` và trả `{"status": "FAILED", ...}`.
2. Upsert **mọi** pick vào watchlist.
3. Chỉ `AUTO_ANALYZE_PICKS = 3` pick đầu (điểm cao nhất) được `analyze(pick.symbol, research=True)`. Các pick còn lại chờ người dùng chạy `desk analyze SYMBOL --research`.
4. Ghi artifact, trả `{"status": "COMPLETED", scan_id, universe_size, dropped, picks: [{symbol, evidence_score, thesis, run_id, action}]}`. `run_id` và `action` là `null` với pick chờ phân tích thủ công.

### 2.3 Phân tích nghiên cứu: `analyze(symbol, cutoff=None, *, research=False)`

- `research=False`: như hiện tại (allowlist bắt buộc).
- `research=True`:
  - symbol phải là watchlist entry **đang còn hạn** và **không** thuộc allowlist;
  - evidence dựng bằng `builder.build(symbol, cutoff, live=cutoff is None, coingecko_id=entry.coingecko_id)`;
  - `position_quantity = 0`;
  - decision được đánh dấu `research_only=True` (trường mới của `ResearchDecision`, mặc định `False`);
  - không tạo ticket: ghi `ticket_blocked.json` `{"reason": "research_only"}` khi action là ACCUMULATE/REDUCE/EXIT;
  - `report.md` có dòng đầu "NGHIÊN CỨU — không giao dịch".
- Mọi phần còn lại (prior thesis, reflections trong prompt, vòng xác nhận ACCUMULATE, artifact) dùng chung đường hiện có.

### 2.4 Chấm điểm

- `refresh_reflections` quét symbol thuộc `settings.symbols ∪ store.watchlist_symbols()`.
- Payload reflection thêm `cohort`: `"watchlist"` nếu decision có `research_only`, ngược lại `"allowlist"`.
- Scorecard gộp theo `(benchmark, cohort)`; hàng cũ không có `cohort` được coi là `allowlist`. Tiêu đề nhóm ghi rõ `Benchmark: BTCUSDT · watchlist (nghiên cứu)`.

### 2.5 CLI

- `desk scan`: in JSON (`--json`) hoặc bảng gọn.
- `desk watchlist`: liệt kê entry còn hạn cùng quyết định committee gần nhất và thời điểm của nó (`null` khi chưa phân tích).
- `desk analyze SYMBOL --research`: phân tích thủ công một entry còn hạn (một trong 7 đồng không được tự chạy, hoặc chạy lại). Entry không có hoặc đã hết hạn thì báo lỗi gọn, exit code 2, không in traceback.

## Phần 3 — Dashboard, xử lý lỗi, kiểm thử

### 3.1 Dashboard

- **Python:** `DashboardSnapshot.watchlist: list[WatchlistSection]`, mặc định `[]`.
  - `WatchlistSection` (`extra="forbid"`) gồm: `symbol`, `evidence_score`, `thesis`, `last_picked_at`, `expires_at`, `mark_usdt`, `change_24h_pct`, `sparkline_closes`, `latest_valid_decision` (dùng lại `LatestValidDecision`).
  - Dựng từ `active_watchlist(now)` cộng `latest_valid_run(symbol)`.
  - `desk scan` publish dashboard khi đã cấu hình.
- **Web:**
  - Contract thêm `DashboardWatchlistEntry` và parser `watchlist`:
    - thiếu thì trả `[]`;
    - symbol theo regex `^[A-Z0-9]{2,20}USDT$`;
    - tối đa 10 entry;
    - trường văn bản bị giới hạn độ dài như các trường hiện có.
  - `watchlist-view` thêm nhóm "Watchlist nghiên cứu — không giao dịch" hiển thị điểm, thesis, action, conviction, thời điểm quyết định, entry/stop/target và R:R.
  - Đồng chưa có run hiện "Chờ phân tích thủ công". Đồng thủ công có thể mang quyết định cũ từ lần chọn trước, nên luôn hiện thời điểm quyết định.
  - Không có nút analyze. Icon dùng avatar chung cho symbol lạ.
  - `KNOWN_SYMBOLS` không đổi.
- **Thứ tự deploy:** parser web bỏ qua key top-level không biết, nên Python ra trước không làm vỡ web cũ. Web do người dùng deploy (Cloudflare).

### 3.2 Xử lý lỗi

| Tình huống | Hành vi |
|---|---|
| 1 trong 4 request khám phá phạm vi lỗi | Dừng lượt quét, `FAILED`, watchlist không đổi |
| Một đồng lỗi dữ liệu chỉ số | Loại đồng đó, ghi vào `dropped` |
| Ít hơn 1 candidate sau lọc | `COMPLETED` với 0 pick |
| Xếp hạng không hợp lệ 2 lần, hoặc provider lỗi | `FAILED`, artifact có `features` và `error` |
| Model chọn 0 đồng | `COMPLETED`, không phân tích gì |
| Pick thứ 4 trở đi | Vào watchlist, không có run, không tốn lượt gọi model |
| Pick top 3 lỗi evidence (lệch giá, thiếu tin…) | Run NO_TRADE undecided. Lỗi dừng ở `builder.build`, trước mọi lời gọi model. Không tự lấy pick thứ 4 bù |
| Committee của một pick lỗi | Run ghi NO_TRADE undecided như hiện tại; entry vẫn nằm trong watchlist |
| Phân tích thủ công một entry hết hạn | Báo lỗi gọn, exit code 2 |

### 3.3 Kiểm thử

- **Khám phá phạm vi:** fixture 4 response; kiểm tra từng bộ lọc (stable, không futures, không OCO/OTO, không CoinGecko id, volume thấp, allowlist), map CoinGecko theo vốn hoá, cắt top 30.
- **Chỉ số:** nến tổng hợp có giá trị biết trước. Kiểm tra `range_position`, khoảng cách ATR, `resistance_distance_atr = None` khi không còn kháng cự, và đồng lỗi dữ liệu thì bị loại.
- **Xếp hạng:** chấp nhận đầu ra hợp lệ; thử lại khi symbol lạ, trùng, quá 10 pick hoặc trường không tồn tại; `FAILED` sau 2 lần sai; 0 pick hợp lệ.
- **Quét:** một dòng funding hỏng không làm hỏng lượt quét.
- **Store:** migration v3→v4 trên DB có dữ liệu; upsert giữ `first_added_at`; hết hạn; thứ tự theo lượt chọn rồi theo điểm (so sánh số, không so chuỗi); cắt theo `limit`.
- **Nghiên cứu:**
  - symbol ngoài allowlist chỉ phân tích được khi có entry còn hạn;
  - không bao giờ có ticket kể cả khi ACCUMULATE;
  - `position_quantity = 0`;
  - `_create_ticket` từ chối decision `research_only`;
  - evidence lỗi ra NO_TRADE và không gọi committee.
- **`scan()`:** mọi pick vào watchlist; chỉ 3 pick đầu được phân tích; pick còn lại có `run_id = null`.
- **Reflection/scorecard:** run nghiên cứu được chấm với `cohort = watchlist`; scorecard tách hai nhóm; hàng cũ vào nhóm `allowlist`.
- **CLI:**
  - `scan`, `watchlist`, `analyze --research` (service được thay bằng fake);
  - `analyze --research` không có entry thì exit code 2;
  - `desk scan` dùng một model client và một public client chung cho committee và máy quét.
- **Dashboard:** snapshot có `watchlist`. Web unit test cho parser: hợp lệ; thiếu trường `watchlist`; symbol sai; vượt 10.
- **Live smoke** (thủ công, chỉ đọc): `desk scan` một lần.

## Ngoài phạm vi (và khi nào làm)

- **Tự chạy theo lịch:** khi người dùng muốn; hiện chỉ lệnh tay.
- **Nút quét trên dashboard:** khi luồng CLI đã ổn định.
- **Nút phân tích watchlist trên dashboard:** khi chạy CLI cho 7 đồng thủ công thấy bất tiện.
- **Tự lấy pick thứ 4 bù khi một đồng top 3 lỗi evidence:** khi chuyện này hay xảy ra.
- **Hạ ngưỡng volume xuống 10M để đủ 30 ứng viên:** khi muốn Flash chọn 10 đồng từ một nhóm rộng hơn 22.
- **Tự đưa đồng vào allowlist:** luôn để người dùng quyết.
- **Percentile top trader trong bảng quét:** thêm 1 request/đồng, khi scorecard watchlist cho thấy cần.
- **VN:** không áp dụng.
