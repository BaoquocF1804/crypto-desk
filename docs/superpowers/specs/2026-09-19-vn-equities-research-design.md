# Cổ phiếu Việt Nam vào desk — nghiên cứu, không thực thi

**Ngày:** 2026-09-19
**Phạm vi V1:** FPT, MBB trên HOSE. Chỉ nghiên cứu: committee phân tích, ra action + báo cáo, ghi reflection để chấm điểm sau. Không sinh lệnh, không gọi broker.

## Vì sao không phải là nới allowlist

Binance nằm trong 13/17 module. `USDT` hardcode 39 chỗ. Ba lớp chặn độc lập:

- `SymbolRules.__post_init__` raise nếu `quote_asset != "USDT"`
- `config.validate` chặn `symbol not in V1_SYMBOLS`
- `config.validate` chặn `not symbol.endswith("USDT")`, và bắt mọi symbol phải có `coingecko_id`

`EvidenceSnapshot` mang `rules: SymbolRules`, `binance_mid`, `reference_usdt`, `funding_rate`, `open_interest`. Không tồn tại giá trị hợp lệ nào của các trường này cho một cổ phiếu HOSE.

Nên đây là asset class thứ hai, không phải symbol thứ sáu.

## Phương án đã chọn: đường song song, dùng chung committee

Ba module mới cho đường VN; một chỗ chạm nhỏ, có kiểm soát, vào `committee.py`. Không đụng `risk.py`, `execution.py`, `broker.py`, `dispatcher.py`, `SymbolRules`.

Đã cân nhắc và loại:

- **Tổng quát hoá lõi bằng chiều `asset_class`** — sạch hơn về lâu dài, nhưng thiết kế trừu tượng khi mới có instance thứ hai là đoán. Để hai implementation chạy thật rồi seam sẽ tự lộ ra. Nếu có asset class thứ ba, lúc đó mới rút ra.
- **Công cụ riêng không dùng lại gì** — mất committee, mất reflection, mất scorecard, tức mất toàn bộ thứ đáng giá.

## Nguồn dữ liệu: SSI iboard

Đã thăm dò trực tiếp ngày 2026-09-19, không cần API key, không bị Cloudflare chặn. TCBS bị Cloudflare, CafeF trả rỗng, VCI sai path — SSI là lựa chọn duy nhất chạy được mà không thêm dependency. Câu này chỉ đúng cho **dữ liệu thị trường**; báo cáo tài chính là chuyện khác và buộc phải thêm dependency, xem mục vnstock bên dưới.

| Endpoint | Cho gì |
|---|---|
| `iboard-api.ssi.com.vn/statistics/charts/history?resolution=1D&symbol=X&from=&to=` | chuỗi OHLCV theo ngày; cũng phục vụ `VN30` làm benchmark |
| `iboard-api.ssi.com.vn/statistics/company/ssmi/stock-info?symbol=X&fromDate=DD/MM/YYYY&toDate=DD/MM/YYYY` | OHLCV từng phiên + khối ngoại + số lệnh + trần/sàn/tham chiếu |
| `iboard-query.ssi.com.vn/stock/exchange/hose` | ảnh chụp bảng giá toàn sàn: `matchedPrice`, `ceiling`, `floor`, `refPrice` |

Trường dùng đến, từ `stock-info`:

```
close, open, high, low            giá ĐIỀU CHỈNH (dùng cho lợi nhuận)
closeRaw, openRaw, highRaw, lowRaw  giá THÔ (dùng cho biên độ, đối chiếu)
ceilingPrice, floorPrice, refPrice  biên độ phiên (không điều chỉnh)
totalMatchVol, totalMatchVal, avgPrice
totalBuyTrade, totalSellTrade, totalBuyTradeVol, totalSellTradeVol
foreignBuyVolTotal, foreignSellVolTotal, foreignBuyValTotal, foreignSellValTotal
foreignCurrentRoom
netBuySellVol, netBuySellVal
```

## Năm chuyên gia, ánh xạ sang thị trường VN

| Vai crypto | Vai VN | Evidence kind | Dữ liệu |
|---|---|---|---|
| technical | technical | `spot` | `daily_closes` (điều chỉnh), giá hiện tại, biến động |
| liquidity | liquidity | `spot` | `totalMatchVol/Val`, `avgPrice`, số lệnh mua/bán, khoảng cách tới trần/sàn |
| news | news | `news` | RSS CafeF + Vietstock |
| derivatives | **dòng tiền** | `flow` | khối ngoại mua/bán ròng, `foreignCurrentRoom`, `netBuySellVol`, mất cân đối lệnh mua/bán |
| *(không có)* | **cơ bản** | `fundamentals` | báo cáo kết quả kinh doanh, cân đối kế toán, chỉ tiêu tài chính — **tùy chọn**, xem bất biến 9 |

Vai "derivatives" ở crypto đọc vị thế của một nhóm tham gia khác thông qua thị trường phái sinh. VN không có phái sinh cho từng mã, nhưng có khối ngoại và room ngoại — cùng chức năng, khác dữ liệu. Đây là ánh xạ theo vai trò, không phải cố nhét dữ liệu vào chỗ trống.

Chuyên gia **cơ bản** không có đối ứng bên crypto, vì crypto không có báo cáo tài chính. Với cổ phiếu thì ngược lại: phân tích mà không đọc báo cáo tài chính là bỏ qua phần cốt lõi. Đây là chuyên gia duy nhất mà đường VN có thêm so với crypto, và là chuyên gia duy nhất có evidence **tùy chọn**.

Ngoài bốn chuyên gia, snapshot VN còn mang một evidence item kind `reference`: giá `matchedPrice` lấy từ `iboard-query`, đúng vai CoinGecko ở đường crypto. Không chuyên gia nào đọc riêng nó; nó tồn tại để kiểm chéo (bất biến 2) và để `required_kinds` đủ bộ.

Debate bull/bear và manager giữ nguyên như crypto. Manager VN không sinh `futures_setups` — không có thị trường phái sinh cho từng mã để lập kịch bản. Không cần đổi schema: `ManagerDecision.futures_bias` đã mặc định `"NEUTRAL"` và `futures_setups` mặc định rỗng, nên manager VN chỉ cần không nhắc tới chúng trong prompt.

## Nguồn thứ hai: vnstock, cho báo cáo tài chính

SSI iboard **không** có báo cáo tài chính — chỉ `company-profile` (vốn điều lệ, số cổ phiếu, free float, ngành). Đã khảo sát ngày 2026-09-20: TCBS bị Cloudflare chặn kể cả với header trình duyệt đầy đủ, CafeF trả 301 không có JSON API dùng được, Vietstock render bằng JS phải scrape. `vnstock` là lựa chọn duy nhất chạy được.

Dùng `vnstock.api.financial.Finance(symbol, source="VCI")`, **không** dùng lớp `Vnstock()` cũ — nó đã in cảnh báo ngừng hỗ trợ.

| Phương thức | Cho gì |
|---|---|
| `income_statement(period, lang="vi")` | báo cáo kết quả kinh doanh theo năm/quý |
| `balance_sheet(period, lang="vi")` | cân đối kế toán |
| `ratio(period, lang="vi")` | 54 chỉ tiêu: P/E, P/B, ROE, ROA, biên lợi nhuận, đòn bẩy, và bộ chỉ tiêu ngân hàng |

Cái giá phải trả, ghi ra để không ai bất ngờ:

- **Phá tính chất "không thêm dependency"** mà phần SSI ở trên đạt được.
- **Phụ thuộc vào bên thứ ba duy trì workaround Cloudflare.** TCBS chặn chúng ta trực tiếp; vnstock qua được vì họ bảo trì. Ngày họ ngừng, tầng cơ bản chết.
- Có "Insiders Program", hàm ý free tier bị giới hạn rate.
- In banner quảng cáo — đã kiểm: ra **stderr**, stdout sạch, nên `desk --json` không bị hỏng JSON.

Vì ba điểm đầu, evidence `fundamentals` là **tùy chọn** (bất biến 10). Một thư viện bên thứ ba không được phép làm chết cả desk.

## Bất biến bắt buộc

### 1. Giá điều chỉnh và giá thô không được trộn

Thăm dò FPT phiên 18/09/2026:

```
close      65182.47   (điều chỉnh)
closeRaw   71700      (giá khớp thật)
floorPrice 69100      (sàn, không điều chỉnh)
```

Giá điều chỉnh **nằm dưới giá sàn**. Code trộn hai loại sẽ kết luận FPT khớp dưới sàn — điều bất khả.

Quy tắc: **lợi nhuận và alpha tính từ giá điều chỉnh; mọi kiểm tra biên độ, đối chiếu và hiển thị giá khớp dùng giá thô.**

MBB cùng phiên không lệch (`close == closeRaw == 19900`). **Test cho bất biến này bắt buộc dùng FPT.** Test chỉ dựng trên MBB sẽ pass mà không kiểm được gì.

### 2. Kiểm chéo hai endpoint SSI

So `matchedPrice` (`iboard-query`) với `closeRaw` (`iboard-api`). Lệch quá 0,5% → `EvidenceError` → `NO_TRADE`, cùng ngưỡng desk crypto dùng cho Binance↔CoinGecko.

**Giới hạn phải ghi rõ trong báo cáo VN:** đây là hai service của cùng một nhà cung cấp, yếu hơn cross-vendor. SSI sai đồng bộ ở cả hai thì desk không phát hiện được. Không giấu giới hạn này.

Lợi ích phụ: nếu code lỡ so `matchedPrice` với `close` thay vì `closeRaw`, FPT lệch ~9% và guard kêu ngay — bất biến 1 được bảo vệ miễn phí.

### 3. Tươi/cũ theo lịch giao dịch, không theo đồng hồ

`EvidenceBuilder._fresh` hiện tại so `cutoff - as_of > maximum_age`, đúng cho thị trường chạy 24/7. Áp lên VN sẽ báo stale mọi thứ Bảy, Chủ nhật và ngày lễ.

Quy tắc VN: dữ liệu tươi khi nó là **phiên giao dịch gần nhất đã đóng cửa** tính đến cutoff. Suy ra lịch giao dịch từ chính chuỗi ngày SSI trả về, không hardcode danh sách ngày lễ.

### 4. Horizon đếm bằng phiên giao dịch, không bằng ngày lịch

Crypto dùng `REFLECTION_HORIZON_DAYS = 20` ngày lịch, đúng cho thị trường chạy liên tục. 20 ngày lịch ở VN chỉ khoảng 14 phiên, và số phiên rơi vào đó còn đổi theo lễ tết.

Đường VN đếm **20 phiên giao dịch đã đóng cửa**, lấy thẳng từ chuỗi SSI trả về — cùng con số 20 nhưng khác đơn vị. Payload reflection ghi `horizon_days: 20` như crypto; con số đó đã được render từ hàng chứ không từ hằng số (sửa ở plan trước), nên hai đường không giẫm lên nhau. Báo cáo VN ghi rõ đơn vị là phiên.

### 5. Benchmark là VN30

FPT và MBB đều là thành phần VN30. Đo chúng với VNINDEX là tặng không phần chênh lệch vốn hoá lớn rồi gọi đó là alpha — cùng sai lầm với việc để BTCUSDT tự chấm alpha của chính nó.

### 6. Reflection phải mang benchmark của nó

`build_scorecard` hiện gộp mọi hàng vào một trung bình theo action. Khi reflection VN và crypto nằm chung bảng, nó sẽ trộn alpha-so-với-BTC với alpha-so-với-VN30.

Bắt buộc: ghi `benchmark_symbol` vào payload reflection, và scorecard **tách nhóm theo benchmark** trước khi gộp theo action. Món này bị defer ở plan trước với lý do "hàng cũ vẫn thiếu trường nên renderer phải xử hai dạng dù sao" — lý do đó đúng khi chỉ có một benchmark và sai ngay khi có hai.

Hàng cũ không có `benchmark_symbol`: suy ra từ symbol (kết thúc bằng `USDT` → `BTCUSDT`), có ghi chú trong render rằng đây là suy luận.

### 7. Reflection chỉ nhận quyết định thật của hội đồng

Phát hiện ngày 2026-09-19 khi soi dữ liệu thật: **7 trong 9 reflection đang được scorecard chấm điểm là run hỏng**, không phải quyết định. Chúng chết vì `provider:rate_limit` và `provider:model_unavailable`, chưa từng tới được hội đồng.

Nguyên nhân: `service._no_trade()` sinh ra một `ResearchDecision` trông y hệt quyết định thật, chỉ khác ở trường `reason`, mà `reason` không được đưa vào payload reflection. Bảng `reflections` vì thế không phân biệt được "hội đồng từ chối" với "chạy hỏng", và hàng `NO_TRADE` trên scorecard đang đo giá đi đâu sau khi Gemini chặn rate limit.

Bắt buộc: **chỉ ghi reflection cho run mà hội đồng thật sự ra quyết định.** Ghi `decided: true` vào payload tại chỗ ra quyết định, và `refresh_reflections` bỏ qua mọi run không có nó. Hàng cũ không có trường này: suy ra từ `reason` (`"committee decision"` hoặc `"Quyết định của hội đồng."` là thật, còn lại là hỏng).

Bất biến này quan trọng với VN hơn với crypto, vì bất biến 8 dưới đây khiến đường VN sinh ra nhiều `_no_trade` giả hơn.

### 8. V1 của VN không có REDUCE và EXIT

`_position_quantity()` đọc `store.latest_snapshot(binance.environment)` — ảnh chụp danh mục Binance. VN không có broker nên giá trị luôn là `0`. `_validate_manager` thì raise `REDUCE/EXIT requires an existing position` khi vị thế `<= 0`, structured output bị từ chối hai lần rồi rơi xuống `_no_trade`.

Nếu để nguyên, mọi lần manager VN cho rằng nên giảm hoặc thoát hàng sẽ bị ghi lại thành `NO_TRADE` kèm lý do `manager structured output rejected` — một quyết định thật bị biến dạng thành lỗi kỹ thuật, đúng thứ bất biến 7 vừa cấm.

Bắt buộc: **prompt manager VN chỉ đưa ra ba lựa chọn — ACCUMULATE, HOLD, NO_TRADE** — để model không bao giờ đề xuất thứ chắc chắn bị từ chối. Không đổi enum `Action` (dùng chung), chỉ thu hẹp trong prompt.

Đây là giới hạn của V1, không phải thiết kế cuối. Gỡ nó cần một nguồn vị thế cho VN (người dùng tự khai danh mục), nằm ngoài phạm vi V1.

### 9. Không đưa chỉ tiêu tài chính không áp dụng cho loại hình doanh nghiệp

Bảng `ratio()` của vnstock là **hợp phẳng** của chỉ tiêu ngân hàng và phi ngân hàng: cả FPT lẫn MBB đều trả về đúng 54 chỉ tiêu như nhau. Ô không áp dụng **không phải null, mà bằng 0.0**.

Đã kiểm ngày 2026-09-20: FPT — một công ty công nghệ không cho vay — có `Nợ xấu (%) = 0.0`. Đưa nguyên bảng cho model thì nó sẽ kết luận "FPT nợ xấu 0%, chất lượng tín dụng xuất sắc" và dựng luận điểm trên đó. Chiều ngược lại, MBB có `Số ngày tồn kho = 0` cho một ngân hàng không có hàng tồn kho.

Cùng loại bẫy với bất biến 1: con số đúng về kỹ thuật, vô nghĩa về ý nghĩa, và model sẽ tin nó.

Bắt buộc: phân loại doanh nghiệp bằng `industryName` từ `company-profile` của SSI (đã có sẵn, miễn phí — FPT trả `"Công nghệ Thông tin"`, MBB trả `"Ngân hàng"`), rồi **loại bỏ hẳn** chỉ tiêu không áp dụng khỏi payload thay vì để số 0 đi qua. Không dùng `bankNumberOfBranch`: nó bằng 0 cho cả hai, vô dụng làm bộ phân loại.

Phạm vi của bất biến này **chỉ là `ratio()`**. `income_statement` và `balance_sheet` đã đúng theo loại hình sẵn: FPT trả 25 chỉ tiêu (`Doanh thu thuần`, `Giá vốn hàng bán`, `Lợi nhuận gộp`), MBB trả 26 (`Thu nhập lãi và các khoản thu nhập tương tự`, `Lãi/(lỗ) thuần từ mua bán chứng khoán kinh doanh`), chỉ 7 chỉ tiêu chung. Hai bản báo cáo khác nhau thật sự, không cần lọc.

### 10. `fundamentals` là evidence tùy chọn, không được làm chết run

`required_kinds` hiện tại chặn cả run khi thiếu bất kỳ kind nào. `fundamentals` **không** nằm trong tập đó.

Khi vnstock hỏng, rate limit, hay đổi API: chuyên gia cơ bản bị bỏ qua, run vẫn chạy với bốn chuyên gia còn lại, và **báo cáo ghi rõ ở đầu rằng phân tích này không đọc được báo cáo tài chính**. Không im lặng bỏ qua — người đọc phải biết mình đang cầm một phân tích thiếu phần cơ bản.

Cần một thay đổi nhỏ trong `committee.run()`: `_specialist_payload` hiện làm `next(item for item in snapshot.items if item.kind == kind)`, ném `StopIteration` khi thiếu. Phải bỏ qua chuyên gia có evidence vắng mặt thay vì vỡ, và ghi tên chuyên gia bị bỏ vào `CommitteeResult` để service dựng dòng cảnh báo.

Đây là lý do đã chọn ở mục vnstock: một thư viện bên thứ ba không được phép quyết định desk có chạy hay không.

## Interface

### Snapshot

VN không dùng lại `EvidenceSnapshot` — nó mang `rules: SymbolRules` sẽ raise, cộng `binance_mid` / `funding_rate` / `open_interest` không có nghĩa cho cổ phiếu.

`VNEvidenceSnapshot` là dataclass riêng, thoả đúng những gì committee chạm tới:

```
symbol: str
cutoff: str
items: tuple[EvidenceItem, ...]      # dùng lại EvidenceItem nguyên vẹn
mid: Decimal                          # giá khớp gần nhất, GIÁ THÔ
evidence_ids: tuple[str, ...]         # property
```

`EvidenceItem` dùng chung, thêm `"flow"` vào `EvidenceKind`.

`EvidenceSnapshot` (crypto) nhận thêm `@property mid` trả `binance_mid`. Thuần bổ sung, không đổi hành vi.

### Committee

`CryptoCommittee.__init__` nhận thêm ba tham số, **mặc định đúng bằng giá trị module-level hiện tại**, nên crypto không đổi một chút nào:

```
specialists: tuple[str, ...] = SPECIALISTS
role_prompts: dict[str, str] = ROLE_PROMPTS
specialist_evidence: dict[str, tuple[str, tuple[str, ...]]] = SPECIALIST_EVIDENCE
mid_label: str = "Binance mid"
```

Trong `run()`:
- `for role in SPECIALISTS` → `self.specialists`
- `required_kinds = {"spot","news","derivatives","reference"}` hardcode → suy ra: `{kind for kind, _ in self.specialist_evidence.values()} | {"reference"}`. Với crypto kết quả y hệt tập hardcode hiện tại (đã chạy thử xác nhận).
- `snapshot.binance_mid` (3 chỗ) → `snapshot.mid`
- `_system_prompt(role)` → đọc `self.role_prompts`

`MAX_ENTRY_DEVIATION` giữ nguyên 2% cho cả hai đường — với cổ phiếu biên độ ±7% thì 2% quanh giá khớp gần nhất vẫn là ràng buộc hợp lý cho một mức entry. Chỉ thông báo lỗi phải sửa: nó đang ghi cứng `"entry price deviates more than 2% from Binance mid"`, sai chữ khi chạy trên HOSE. Dùng `self.mid_label` để dựng thông báo; VN truyền `"giá khớp SSI"`.

Thêm một thay đổi nữa cho bất biến 10: `_specialist_payload` đang làm `next(item for item in snapshot.items if item.kind == kind)` và ném `StopIteration` khi evidence vắng mặt. Đổi thành trả `None`, `run()` bỏ qua chuyên gia đó, và `CommitteeResult` mang thêm `skipped_specialists: tuple[str, ...]` để service dựng dòng cảnh báo trong báo cáo. Với crypto tập này luôn rỗng vì `required_kinds` đã chặn trước, nên hành vi không đổi.

`OUTPUT_CONTRACT` dùng chung, không đổi.

### Module mới

| File | Nội dung |
|---|---|
| `src/crypto_desk/vn_data.py` | `SSIClient` (3 endpoint, có timeout và retry như `PublicDataClient`), `VNEvidenceBuilder.build(symbol, cutoff)`, `reflection_closes(symbol, start)`, `VNEvidenceSnapshot` |
| `src/crypto_desk/vn_fundamentals.py` | `VNFundamentals.fetch(symbol, industry) -> EvidenceItem \| None` — gọi vnstock, lọc `ratio()` theo ngành (bất biến 9), trả `None` thay vì ném khi vnstock hỏng (bất biến 10) |
| `src/crypto_desk/vn_prompts.py` | `VN_SPECIALISTS` (5 chuyên gia), `VN_ROLE_PROMPTS` (5 chuyên gia + bull/bear/manager bản VN), `VN_SPECIALIST_EVIDENCE`, `RATIO_WHITELIST` theo ngành |
| `src/crypto_desk/vn_service.py` | `VNDeskService.analyze(symbol, cutoff)` — dựng evidence, gọi committee, ghi `research_runs` + artifacts, `refresh_reflections` với benchmark VN30 |

### Store — cần một bộ lọc, không dùng lại nguyên vẹn

Bản trước của spec viết "dùng lại `Store` nguyên vẹn". Sai. `unreflected_runs` không có bộ lọc asset:

```sql
SELECT r.* FROM research_runs AS r
LEFT JOIN reflections f ON f.run_id = r.id
WHERE f.run_id IS NULL AND r.cutoff <= ?  LIMIT 100
```

Nên `refresh_reflections` của crypto sẽ vớ phải run FPT/MBB, đọc `evidence["binance_mid"]` — khoá không tồn tại trong evidence VN — ném `KeyError`, rơi thẳng vào `except (EvidenceError, OSError, ValueError, KeyError, TypeError): continue`. Hậu quả: run VN **không bao giờ được chấm**, lỗi bị nuốt không báo, và vì vẫn "chưa reflect" nên mỗi lần `desk daily` lại thử lại vĩnh viễn, dần chiếm hết suất `LIMIT 100` và đẩy run crypto ra khỏi lô.

Sửa: `unreflected_runs(completed_before, symbols)` nhận thêm danh sách symbol và lọc `WHERE r.symbol IN (...)`. Crypto truyền `settings.symbols`, VN truyền `settings.vn_symbols`. Không đổi schema, không migration.

### CLI

`desk vn-analyze SYMBOL` — phân tích một mã, cutoff live.

`desk vn-daily` — **V1 không có screener.** `screen()` của crypto đọc `snapshot.quote_volume` và `snapshot.spread` (hai trường `VNEvidenceSnapshot` không có) với ngưỡng hiệu chỉnh cho crypto. Xây screener VN là việc riêng, không thuộc V1. Nên `vn-daily` chạy `refresh_reflections` rồi phân tích **toàn bộ** `vn_symbols` không lọc, và đánh dấu bucket lịch `"vn_daily"` — tên khác `"daily"` để hai lịch không giẫm lên nhau.

Cùng khuôn `_emit` / `_load` / `Store` sẵn có.

### Config

Khoá mới, tách khỏi validate của crypto:

```yaml
vn_symbols: [FPT, MBB]
vn_news_feeds:
  - https://cafef.vn/thi-truong-chung-khoan.rss
  - https://vietstock.vn/144/chung-khoan/co-phieu.rss
```

Dependency mới trong `pyproject.toml`, ghim phiên bản chính xác như mọi dependency khác của repo:

```
"vnstock==<phiên bản đang cài lúc thực thi>",
```

Hằng số: `VN_BENCHMARK_SYMBOL = "VN30"`, `VN_V1_SYMBOLS = frozenset({"FPT","MBB"})`.

`vn_symbols` **không** đi qua `endswith("USDT")` hay `V1_SYMBOLS`; validate riêng theo `VN_V1_SYMBOLS`.

## Dữ liệu chảy thế nào

```
desk vn-analyze FPT
  └ VNEvidenceBuilder.build("FPT", cutoff)
      ├ charts/history  → daily_closes (điều chỉnh)
      ├ stock-info      → phiên gần nhất: OHLCV, khối ngoại, số lệnh, trần/sàn
      ├ iboard-query    → matchedPrice  ─┐
      │                                  ├→ lệch >0,5% → EvidenceError → NO_TRADE
      │                   closeRaw      ─┘
      ├ RSS CafeF/Vietstock → news items
      ├ company-profile → industryName (phân loại ngành cho bất biến 9)
      ├ vnstock → income_statement + balance_sheet + ratio(đã lọc theo ngành)
      │            hỏng → bỏ qua, KHÔNG ném (bất biến 10)
      └ kiểm phiên gần nhất đã đóng → không thì EvidenceError
  └ CryptoCommittee.run(snapshot, specialists=VN_SPECIALISTS, ...)
      technical / liquidity / news / flow / cơ bản → bull ⇄ bear ×2 → manager
      chuyên gia thiếu evidence bị bỏ qua, tên vào skipped_specialists
  └ Store.save_run + artifacts (report.md, evidence.json, decision.json)

desk vn-daily
  ├ refresh_reflections(symbols=vn_symbols)   ← chỉ quét run VN
  │    bỏ qua run không phải quyết định hội đồng (bất biến 7)
  │    entry     = closeRaw lúc quyết định
  │    closes    = 20 phiên điều chỉnh sau đó
  │    benchmark = VN30
  │    payload  += decided, benchmark_symbol, decision_cutoff, horizon_days
  ├ phân tích toàn bộ vn_symbols, không lọc (V1 không có screener)
  └ mark_scheduled("vn_daily", bucket)        ← khác "daily" của crypto
```

## Xử lý lỗi

Theo đúng nguyên tắc sẵn có của desk: **thà không ra gì còn hơn ra sai.**

| Tình huống | Hành vi |
|---|---|
| Hai endpoint SSI lệch >0,5% | `EvidenceError` → `NO_TRADE` |
| Phiên gần nhất chưa đóng cửa | `EvidenceError` → `NO_TRADE` |
| Thiếu bất kỳ evidence kind nào | committee tự trả `NO_TRADE` (cơ chế sẵn có) |
| SSI lỗi mạng / 5xx | retry rồi `EvidenceError` → `NO_TRADE` |
| Tất cả RSS feed hỏng | `EvidenceError` — cùng cách desk crypto xử `news_feeds` rỗng |
| Chuỗi giá < số phiên cần | `EvidenceError`, không suy đoán bù |
| **vnstock lỗi / rate limit / đổi API** | **bỏ qua chuyên gia cơ bản, run vẫn chạy**, báo cáo ghi rõ ở đầu là thiếu phần cơ bản |
| `industryName` không đọc được | bỏ qua chuyên gia cơ bản — không đoán ngành để lọc chỉ tiêu |

Mọi dòng trên đều kết thúc bằng `NO_TRADE` được ghi vào `research_runs` — nhưng **không dòng nào trong số đó được sinh reflection** (bất biến 7). Chỉ run mang `decided: true` mới vào bảng chấm điểm. Đọc bảng này mà quên điều đó là tái tạo đúng lỗi đang có trong dữ liệu hôm nay.

## Test

Bắt buộc, ngoài test đơn vị thông thường:

1. **FPT giá điều chỉnh vs thô** — fixture lấy đúng số phiên 18/09/2026 (`close` 65182.47, `closeRaw` 71700, `floorPrice` 69100). Khẳng định lợi nhuận tính từ giá điều chỉnh, đối chiếu tính từ giá thô, và không đường nào kết luận giá dưới sàn.
2. **Kiểm chéo bắt được nhầm trường** — cho `matchedPrice` so với `close` thay vì `closeRaw` và khẳng định nó raise.
3. **Lịch giao dịch** — cutoff rơi vào thứ Bảy, dữ liệu phiên thứ Sáu vẫn là tươi.
4. **Scorecard tách benchmark** — trộn hàng BTCUSDT-benchmark và VN30-benchmark, khẳng định chúng không gộp vào cùng một trung bình.
5. **Hàng reflection cũ không có `benchmark_symbol`** — suy ra đúng và render có ghi chú là suy luận.
6. **Crypto không đổi hành vi** — committee dựng không truyền tham số mới phải cho kết quả y hệt trước.
7. **Run hỏng không sinh reflection** — dựng một run có `reason="provider:rate_limit"` và khẳng định `refresh_reflections` bỏ qua nó; một run có `reason="Quyết định của hội đồng."` thì nhận. Bảo vệ bất biến 7.
8. **Hàng reflection cũ không có `decided`** — suy ra từ `reason` đúng theo cả hai định dạng: `"committee decision"` (bản tiếng Anh cũ) và `"Quyết định của hội đồng."`.
9. **`unreflected_runs` lọc theo symbol** — trộn run `BTCUSDT` và `FPT` trong cùng bảng, khẳng định truy vấn với `settings.symbols` không trả về `FPT` và ngược lại. Bảo vệ khỏi lỗi nuốt im lặng.
10. **Prompt manager VN không mời REDUCE/EXIT** — khẳng định chuỗi prompt chỉ liệt kê ACCUMULATE, HOLD, NO_TRADE. Bảo vệ bất biến 8.
11. **Chỉ tiêu ngân hàng bị loại khỏi payload của FPT** — fixture `ratio()` có `Nợ xấu (%) = 0.0` cho FPT, khẳng định khoá đó **không tồn tại** trong payload gửi model, chứ không phải bằng 0. Chiều ngược lại: `Số ngày tồn kho` bị loại khỏi payload của MBB. Bảo vệ bất biến 9 — đây là test quan trọng nhất của phần cơ bản.
12. **vnstock hỏng không làm chết run** — cho `VNFundamentals.fetch` ném, khẳng định `build()` vẫn trả snapshot, `fundamentals` không có trong `items`, và committee vẫn chạy với bốn chuyên gia còn lại.
13. **Báo cáo nói rõ khi thiếu phần cơ bản** — khẳng định `report.md` chứa dòng cảnh báo khi `skipped_specialists` không rỗng. Thiếu im lặng là thứ bất biến 10 cấm.

## Không làm ở V1

- **REDUCE và EXIT trên đường VN** (bất biến 8). Không có nguồn vị thế cho cổ phiếu nên hai action này luôn bị `_validate_manager` từ chối; prompt VN vì thế không mời chúng. Gỡ được khi có chỗ để người dùng khai danh mục VN.
- **Screener cho VN.** `screen()` hiện tại đọc `quote_volume` và `spread` với ngưỡng crypto. `vn-daily` chạy thẳng cả `vn_symbols` không lọc.

- Đặt lệnh, sinh phiếu lệnh, sizing theo VND. Không đụng `risk.py` / `execution.py` / `broker.py`.
- Sổ lệnh mức 1-10 (SSI iboard không cho công khai). Chuyên gia liquidity làm việc với khối lượng khớp và số lệnh.
- Phiên ATO/ATC, T+2.5, biên độ riêng của HNX/UPCoM. V1 chỉ HOSE, chỉ giá đóng cửa.
- Room ngoại như một ràng buộc thực thi. V1 chỉ đọc `foreignCurrentRoom` làm tín hiệu, không làm điều kiện chặn.
- Mở rộng universe. FPT và MBB trước; chọn thêm là quyết định riêng, theo thanh khoản và độ phủ dữ liệu, không theo kỳ vọng giá.
