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

Đã thăm dò trực tiếp ngày 2026-09-19, không cần API key, không bị Cloudflare chặn. TCBS bị Cloudflare, CafeF trả rỗng, VCI sai path — SSI là lựa chọn duy nhất chạy được mà không thêm dependency.

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

## Bốn chuyên gia, ánh xạ sang thị trường VN

| Vai crypto | Vai VN | Evidence kind | Dữ liệu |
|---|---|---|---|
| technical | technical | `spot` | `daily_closes` (điều chỉnh), giá hiện tại, biến động |
| liquidity | liquidity | `spot` | `totalMatchVol/Val`, `avgPrice`, số lệnh mua/bán, khoảng cách tới trần/sàn |
| news | news | `news` | RSS CafeF + Vietstock |
| derivatives | **dòng tiền** | `flow` | khối ngoại mua/bán ròng, `foreignCurrentRoom`, `netBuySellVol`, mất cân đối lệnh mua/bán |

Vai "derivatives" ở crypto đọc vị thế của một nhóm tham gia khác thông qua thị trường phái sinh. VN không có phái sinh cho từng mã, nhưng có khối ngoại và room ngoại — cùng chức năng, khác dữ liệu. Đây là ánh xạ theo vai trò, không phải cố nhét dữ liệu vào chỗ trống.

Ngoài bốn chuyên gia, snapshot VN còn mang một evidence item kind `reference`: giá `matchedPrice` lấy từ `iboard-query`, đúng vai CoinGecko ở đường crypto. Không chuyên gia nào đọc riêng nó; nó tồn tại để kiểm chéo (bất biến 2) và để `required_kinds` đủ bộ.

Debate bull/bear và manager giữ nguyên như crypto. Manager VN không sinh `futures_setups` — không có thị trường phái sinh cho từng mã để lập kịch bản. Không cần đổi schema: `ManagerDecision.futures_bias` đã mặc định `"NEUTRAL"` và `futures_setups` mặc định rỗng, nên manager VN chỉ cần không nhắc tới chúng trong prompt.

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
```

Trong `run()`:
- `for role in SPECIALISTS` → `self.specialists`
- `required_kinds = {"spot","news","derivatives","reference"}` hardcode → suy ra: `{kind for kind, _ in self.specialist_evidence.values()} | {"reference"}`. Với crypto kết quả y hệt tập hardcode hiện tại.
- `snapshot.binance_mid` (3 chỗ) → `snapshot.mid`
- `_system_prompt(role)` → đọc `self.role_prompts`

`OUTPUT_CONTRACT` dùng chung, không đổi.

### Module mới

| File | Nội dung |
|---|---|
| `src/crypto_desk/vn_data.py` | `SSIClient` (3 endpoint, có timeout và retry như `PublicDataClient`), `VNEvidenceBuilder.build(symbol, cutoff)`, `reflection_closes(symbol, start)`, `VNEvidenceSnapshot` |
| `src/crypto_desk/vn_prompts.py` | `VN_SPECIALISTS`, `VN_ROLE_PROMPTS` (4 chuyên gia + bull/bear/manager bản VN), `VN_SPECIALIST_EVIDENCE` |
| `src/crypto_desk/vn_service.py` | `VNDeskService.analyze(symbol, cutoff)` — dựng evidence, gọi committee, ghi `research_runs` + artifacts, `refresh_reflections` với benchmark VN30 |

CLI: `desk vn-analyze SYMBOL`, `desk vn-daily`. Cùng khuôn `_emit` / `_load` / `Store` sẵn có.

### Config

Khoá mới, tách khỏi validate của crypto:

```yaml
vn_symbols: [FPT, MBB]
vn_news_feeds:
  - https://cafef.vn/thi-truong-chung-khoan.rss
  - https://vietstock.vn/144/chung-khoan/co-phieu.rss
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
      └ kiểm phiên gần nhất đã đóng → không thì EvidenceError
  └ CryptoCommittee.run(snapshot, specialists=VN_SPECIALISTS, ...)
      technical / liquidity / news / flow → bull ⇄ bear ×2 → manager
  └ Store.save_run + artifacts (report.md, evidence.json, decision.json)

desk vn-daily  (sau ≥20 phiên)
  └ refresh_reflections: entry = closeRaw lúc quyết định
                         closes  = 20 phiên điều chỉnh sau đó
                         benchmark = VN30
                         payload += benchmark_symbol, decision_cutoff, horizon_days
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

## Test

Bắt buộc, ngoài test đơn vị thông thường:

1. **FPT giá điều chỉnh vs thô** — fixture lấy đúng số phiên 18/09/2026 (`close` 65182.47, `closeRaw` 71700, `floorPrice` 69100). Khẳng định lợi nhuận tính từ giá điều chỉnh, đối chiếu tính từ giá thô, và không đường nào kết luận giá dưới sàn.
2. **Kiểm chéo bắt được nhầm trường** — cho `matchedPrice` so với `close` thay vì `closeRaw` và khẳng định nó raise.
3. **Lịch giao dịch** — cutoff rơi vào thứ Bảy, dữ liệu phiên thứ Sáu vẫn là tươi.
4. **Scorecard tách benchmark** — trộn hàng BTCUSDT-benchmark và VN30-benchmark, khẳng định chúng không gộp vào cùng một trung bình.
5. **Hàng reflection cũ không có `benchmark_symbol`** — suy ra đúng và render có ghi chú là suy luận.
6. **Crypto không đổi hành vi** — committee dựng không truyền tham số mới phải cho kết quả y hệt trước.

## Không làm ở V1

- Đặt lệnh, sinh phiếu lệnh, sizing theo VND. Không đụng `risk.py` / `execution.py` / `broker.py`.
- Sổ lệnh mức 1-10 (SSI iboard không cho công khai). Chuyên gia liquidity làm việc với khối lượng khớp và số lệnh.
- Phiên ATO/ATC, T+2.5, biên độ riêng của HNX/UPCoM. V1 chỉ HOSE, chỉ giá đóng cửa.
- Room ngoại như một ràng buộc thực thi. V1 chỉ đọc `foreignCurrentRoom` làm tín hiệu, không làm điều kiện chặn.
- Mở rộng universe. FPT và MBB trước; chọn thêm là quyết định riêng, theo thanh khoản và độ phủ dữ liệu, không theo kỳ vọng giá.
