# Binance Spot Testnet E2E

Runbook này ghi lại bằng chứng Testnet mà không chứa API key, secret, raw account
payload hoặc số dư nhạy cảm. Chỉ đánh dấu một bước `PASS` sau khi đã lưu timestamp,
redacted ID và trạng thái terminal quan sát được.

## Trạng thái hiện tại

| Hạng mục | Trạng thái | Bằng chứng |
|---|---|---|
| Automated offline suite | PASS | 2026-07-18: 243 tests, Ruff, format, web tests/build/lint |
| Public endpoint doctor | PASS | 2026-07-18: Spot clock drift 173 ms; Spot, Futures, CoinGecko, RSS, Gemini OK |
| Read-only online smoke | PASS | 2026-07-18: `sync`, `screen`, `analyze BTCUSDT`; run `f90cac25…`; local snapshot published |
| Một OTOCO lifecycle | BLOCKED | Risk fail-closed trước submit: 25 unpriced assets, gross > 80%, reserve < 20% |
| Failure-path online checks | NOT RUN | Cần Testnet execution |
| Restart idempotency | NOT RUN | Cần một order chain Testnet |

`BLOCKED` hoặc `NOT RUN` không được diễn giải thành thành công.

Lần chạy 2026-07-18 không gửi authenticated order nào. Testnet credentials và
execution flag đều hợp lệ, nhưng account bootstrap của Spot Testnet không đáp ứng
portfolio guard của ứng dụng. Không được bỏ qua guard hoặc submit trực tiếp qua
broker chỉ để đổi trạng thái runbook thành `PASS`.

## Điều kiện trước khi chạy

1. Tạo Binance Spot Testnet key riêng.
2. Xác nhận `.env` có `BINANCE_ENV=testnet`,
   `LIVE_EXECUTION_ENABLED=0` và ban đầu
   `TESTNET_EXECUTION_ENABLED=0`.
3. Xác nhận `config.yaml` chỉ chứa `BTCUSDT`, `ETHUSDT`, `BNBUSDT`,
   `SOLUSDT`.
4. Không ghi key, secret, balance đầy đủ hoặc raw response vào runbook.

## Read-only smoke

```bash
uv run desk --config config.yaml --json doctor --online
uv run desk --config config.yaml --json sync
uv run desk --config config.yaml --json screen
uv run desk --config config.yaml --json analyze BTCUSDT
```

Ghi nhận:

- endpoint được báo là Testnet;
- `sync` trả snapshot có timestamp;
- `screen` không xử lý ngoài allowlist;
- `analyze` tạo đủ `evidence.json`, `analysts.json`, `decision.json`,
  `report.md`;
- chưa có authenticated submit.

## Một OTOCO lifecycle

Chỉ sau read-only smoke, đặt `TESTNET_EXECUTION_ENABLED=1`, khởi động lại process
và chạy lại `doctor`. Tạo một ticket BTCUSDT đáp ứng filter hiện hành, duyệt bằng
Telegram user trong allowlist, rồi ghi:

| Field | Redacted evidence |
|---|---|
| ticket ID | pending |
| `listClientOrderId` | pending |
| created/submitted UTC | pending |
| working order | phải là `LIMIT FOK` |
| protection children | take-profit + stop-loss |
| terminal status | pending |
| cancel/reconcile UTC | pending |

Chạy reconciliation hai lần để chứng minh idempotency:

```bash
uv run desk --config config.yaml --json orders --reconcile
uv run desk --config config.yaml --json orders --reconcile
```

Khởi động lại WSL/Hermes và xác nhận không tạo `listClientOrderId` thứ hai cho
cùng ticket.

## Failure paths

Kiểm tra tuần tự, mỗi trường hợp dùng ticket mới khi cần:

- Telegram user ngoài allowlist;
- ticket quá 30 phút;
- confirmation code sai;
- quote lệch hơn 0,5%;
- symbol filters thay đổi;
- timeout nhưng tìm thấy chain theo client ID;
- timeout và không tìm thấy chain;
- HTTP 429 không tạo retry storm;
- approval lặp lại không submit thêm.

Nếu kết quả là `RECONCILE_REQUIRED`, Không tự gửi lại hoặc approve lại. Chạy:

```bash
uv run desk --config config.yaml --json orders --reconcile
uv run desk --config config.yaml --json health
```

Sau đó đối chiếu trực tiếp Binance bằng `listClientOrderId`. Chỉ đóng sự cố khi
đã có trạng thái rõ và audit record tương ứng.

## Kết thúc an toàn

Đặt lại:

```dotenv
BINANCE_ENV=testnet
TESTNET_EXECUTION_ENABLED=0
LIVE_EXECUTION_ENABLED=0
```

Chạy `doctor`, giữ database/artifacts để audit và cập nhật bảng trạng thái ở đầu
runbook.
