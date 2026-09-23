# Audit Remediation — Design

Ngày: 2026-09-23. Baseline: `ae17034`, 332 passed, ruff sạch.

Spec này mô tả năm khiếm khuyết đo được trên dữ liệu thật của repo, root cause đã truy tới dòng,
và quyết định thiết kế cho từng cái. Plan hiện thực: `docs/superpowers/plans/2026-09-23-audit-remediation.md`.

## Bằng chứng nền

Đo trên `data/crypto_desk.sqlite3` (339 research run) và `artifacts/crypto/` (328 evidence.json,
590 lệnh gọi LLM trong tháng 9):

| Chỉ số | Giá trị |
|---|---|
| Phân bố action | NO_TRADE 322 (95,0%) · HOLD 10 · ACCUMULATE 7 · REDUCE/EXIT 0 |
| Run chết ở tầng evidence | 235/328 = 71,6%, trong đó **221 vì `"binance evidence is from the future"`** |
| Run tới committee rồi chết vì provider | 16/93 ≈ 17% (14 `rate_limit`, 2 `model_unavailable`) |
| Ticket / approval / order_event trong DB | **0 / 0 / 0** |
| Entry so với mid, 31 quyết định tháng 9 | median **−2,86%**, chỉ 3/31 nằm trong 0,5%, max +0,01% |
| Reflection đã chấm | 9 hàng, 4 hàng là chính benchmark → 5 mẫu hữu dụng |

## Khiếm khuyết 1 — `analyze` không bao giờ tạo nổi ticket

`_create_ticket` ([service.py:520](../../../src/crypto_desk/service.py)) đòi một `PortfolioSnapshot`
mới dưới 5 phút. Nhưng `desk analyze` dựng service với `broker=False`
([cli.py:123](../../../src/crypto_desk/cli.py)), nên service **không có cách nào tự `sync()`** —
nó chỉ đọc snapshot mà một lệnh khác tình cờ để lại.

Bằng chứng: ACCUMULATE BTCUSDT lúc `2026-09-18T04:47`, snapshot gần nhất trong DB là
`2026-09-21T05:49`. Cả 7 quyết định ACCUMULATE đều rơi vào `return None` và **không để lại
một dòng nào** giải thích vì sao. `except ValueError: return None`
([service.py:593](../../../src/crypto_desk/service.py)) nuốt nốt mọi lỗi sizing còn lại.

**Quyết định thiết kế:**
- `_create_ticket` trả `tuple[str | None, str | None]` — id và lý do bị chặn. Lý do được ghi ra
  `ticket_blocked.json` trong thư mục artifact của run.
- Service tự `sync()` khi snapshot cũ, và chỉ khi có broker. Không có broker thì trả lý do
  `no_portfolio_snapshot_within_5min` chứ không im lặng.
- `analyze` và `daily` trong `cli.py` dựng service với `broker=True`.
- Sai sót của broker khi sync **không** được phép làm hỏng run: `analyze` vẫn phải ghi đủ artifact
  và trả `AnalysisRun`. Ticket là hệ quả của nghiên cứu, không phải điều kiện của nó.

## Khiếm khuyết 2 — một lỗi 429 huỷ trắng cả run

`_call` ([committee.py:783-797](../../../src/crypto_desk/committee.py)) ghi nhận `ProviderError`
rồi `raise` ngay, không dùng nốt lượt `attempt` thứ hai mà chính vòng lặp `range(1, 3)` đã cấp.
`run()` bắt lại ở ngoài và trả NO_TRADE cho toàn bộ run.

Chi phí đo được: 14 run mất trắng, mỗi run trung bình 8 call đã trả tiền và ~148 giây.

**Quyết định thiết kế:**
- Chỉ retry hai loại nhất thời theo định nghĩa: `rate_limit`, `network`. `authentication`,
  `model_unavailable`, `provider_error` phải hỏng ngay — retry một khoá sai là đốt thời gian.
- Không thêm `sleep` ở tầng committee. `GeminiStructuredClient` đã giữ nhịp `min_interval_seconds`
  và tự ngủ 65s sau mỗi 429; lượt thứ hai của committee tự nhiên cách lượt đầu ~130s.
  Tổng request tối đa cho một stage: 6 (2 attempt × 3 lần thử trong client).
- Lý do NO_TRADE cuối cùng phải nói rõ là đã retry: `"... rejected: provider:rate_limit"`.

## Khiếm khuyết 3 — `daily()` không thể dựng nổi evidence

`daily()` đặt `cutoff = <ngày> 00:15:00` rồi mới đi fetch, và gọi `screen(cutoff)` →
`builder.build(..., live=False)`. Với `live=False`, `future_cutoff = cutoff` và `tolerance = 0`
([data.py:641](../../../src/crypto_desk/data.py)), nên mọi `fetched_at` — vốn luôn muộn hơn cutoff
vài giây — đều bị coi là "từ tương lai". 221/328 run chết vì đúng dòng này.

Suite không bắt được vì `test_fetch_timestamp_after_live_cutoff_is_not_future_evidence`
([tests/test_data.py:209](../../../tests/test_data.py)) chỉ phủ nhánh `live=True`, và `FakeBuilder`
trong `test_service_cli.py` không bao giờ nhìn đồng hồ.

**Quyết định thiết kế:**
- `daily()` chạy với cutoff **live** — thời điểm nó thực sự chạy, không phải 00:15 danh nghĩa.
  Bucket idempotency vẫn là chuỗi ngày, nên không đổi ngữ nghĩa lịch.
- Catch-up cho một ngày đã qua **không được phân tích**. Sổ lệnh, funding và RSS 48 giờ của một
  ngày đã qua không dựng lại được; mọi cố gắng chỉ sinh ra evidence rác hoặc lỗi. Bucket quá khứ
  chỉ chạy `refresh_reflections` (klines lịch sử dựng lại được thật) rồi đánh dấu xong với
  status `REFLECTIONS_ONLY`.
- VN không có khiếm khuyết này: `VNEvidenceBuilder` so ngày phiên với ngày cutoff
  ([vn_data.py:61-73](../../../src/crypto_desk/vn_data.py)), không so mốc thời gian fetch.
  Không đụng `vn_service.py` trong hạng mục này.

## Khiếm khuyết 4 — ba ngưỡng giá mâu thuẫn

| Tầng | Ngưỡng | Nguồn |
|---|---|---|
| Committee | entry lệch ≤ **2%** so với mid | `MAX_ENTRY_DEVIATION` [committee.py:21](../../../src/crypto_desk/committee.py) |
| Execution | entry lệch ≤ **0,5%** so với mid lúc duyệt | `risk.max_quote_deviation` [execution.py:317](../../../src/crypto_desk/execution.py) |
| Sàn | LIMIT **FOK** — khớp toàn bộ ngay hoặc huỷ | [broker.py:264](../../../src/crypto_desk/broker.py) |

Vùng 0,5%–2% là vùng chết: ticket được đúc ra để chắc chắn bị từ chối lúc duyệt.

**Quyết định thiết kế:**
- Ngưỡng của committee lấy từ `risk.max_quote_deviation`, truyền vào constructor. Hai con số phải
  là **một** con số; để hai chỗ tự đặt là cách chúng lệch nhau ở lần sửa sau.
- Thông báo lỗi phải in ra ngưỡng thật, không ghi cứng chuỗi `"2%"`.
- Đây **không** phải cách làm desk giao dịch nhiều hơn. Nó làm desk thôi đúc ticket chết. Việc cho
  chiến lược mua hồi có đường chảy ra lệnh là `entry_type: RESTING` ở giai đoạn sau, ngoài phạm vi.

## Khiếm khuyết 5 — news không phân biệt được tin của symbol

`news_payload = {"symbol": symbol, "items": recent_news}`
([data.py:545](../../../src/crypto_desk/data.py)): `recent_news` chỉ lọc theo thời gian, nên
SUIUSDT và BTCUSDT nhận **cùng một rổ headline**. Bên VN nặng hơn: mọi mã nhận cùng RSS thị trường
CafeF/Vietstock ([vn_data.py:190](../../../src/crypto_desk/vn_data.py)). Prompt vẫn bảo chuyên gia
news "đánh giá mức độ liên quan với symbol" mà không cho nó cơ sở nào để làm việc đó.

**Quyết định thiết kế:**
- **Gắn nhãn, không lọc bỏ.** Mỗi item nhận `relevance: "symbol" | "market"`. Lọc cứng sẽ biến một
  ngày không có tin riêng thành `EvidenceError("No current news evidence")` → NO_TRADE cho cả desk;
  đó là đổi một khiếm khuyết lấy một khiếm khuyết tệ hơn. Rổ tin chung vẫn là bối cảnh hợp lệ.
- Alias lấy từ dữ liệu đã có: `rules.base_asset` (`"BTC"`) và `coingecko_ids[symbol]`
  (`"bitcoin"`). VN dùng chính mã (`"FPT"`). Không thêm bảng ánh xạ mới.
- So khớp theo biên từ, không phân biệt hoa thường, để `"BTC"` không trúng trong `"BTCUSDT"` và
  `"sui"` không trúng trong `"suit"`.
- Item `relevance: "symbol"` xếp trước trong payload, và payload mang `symbol_news_count`.
- Prompt của `news` phải được sửa để nói rõ nhãn đó nghĩa là gì — thêm field mà không nói cho model
  biết thì field vô dụng.

## Ngoài phạm vi

- `analysis_summary` vẫn in Entry/Stop/Target lên Telegram khi action là NO_TRADE
  ([notifications.py:24-56](../../../src/crypto_desk/notifications.py)). Rủi ro vận hành thật
  (người đọc thấy một kế hoạch giao dịch đầy đủ dưới nhãn "không giao dịch") nhưng không nằm trong
  năm hạng mục được yêu cầu.
- `entry_type: RESTING`, liquidation/CVD/volume profile, `rebuttals` cho vòng tranh biện 2,
  `evidence_ids` mức datapoint: giai đoạn 2 và 3 của roadmap.
- `AGENTS.md` ở gốc repo là template onboarding Vnstock, không mô tả dự án này.
