# Crypto Desk

Crypto Desk là trợ lý nghiên cứu và thực thi Binance Spot long-only có phê duyệt
thủ công. Hệ thống thu thập Spot OHLCV/order book/khối lượng, RSS news, funding
rate, open interest và giá đối chiếu CoinGecko; các tín hiệu phái sinh chỉ phục vụ
nghiên cứu, không dùng để thực thi lệnh.

Đây là công cụ hỗ trợ quyết định cá nhân, không phải tư vấn tài chính. Mainnet có
thể làm mất tiền thật; mặc định cả Testnet execution và Mainnet execution đều tắt.

## Phạm vi và giới hạn

- Chỉ Binance Spot, long-only; không Margin, không vay/short và không thực thi
  derivatives.
- Allowlist V1: `BTCUSDT`, `ETHUSDT`, `BNBUSDT`, `SOLUSDT`.
- Rủi ro tối đa 0,5% NAV/lệnh, 20% NAV/coin, tổng crypto 80% NAV và giữ ít nhất
  20% NAV dưới dạng USDT khả dụng.
- Mainnet bị khóa ở tối đa 25 USDT/order chain cho đến khi có ít nhất 20 chain
  Mainnet được reconcile về trạng thái terminal.
- Dữ liệu thiếu, stale, time leakage hoặc Binance/CoinGecko lệch hơn 0,5% luôn
  dẫn tới `NO_TRADE`.

## Cài đặt trên WSL2

Yêu cầu Python 3.12 và `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --extra dev
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

Tạo Binance Spot Testnet account và API key tại cổng Testnet chính thức. Điền key
vào `BINANCE_TESTNET_API_KEY` và `BINANCE_TESTNET_API_SECRET`; giữ
`BINANCE_ENV=testnet` và `TESTNET_EXECUTION_ENABLED=0` khi bắt đầu.

Nếu sau này dùng Mainnet, tạo một API key riêng chỉ có quyền Spot trading:

- tắt withdrawal;
- không cấp quyền Margin hay derivatives;
- bật IP restriction tới IP thực thi;
- không dùng lại key Testnet;
- lưu trong `BINANCE_MAINNET_API_KEY` và `BINANCE_MAINNET_API_SECRET`.

Đặt `OPENAI_API_KEY`, tùy chọn `COINGECKO_DEMO_API_KEY`, danh sách RSS trong
`config.yaml`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL` và Telegram user ID
trong `telegram_allowlist`. Không commit `.env` hoặc `config.yaml`.

Kiểm tra cấu hình offline trước:

```bash
uv run desk --config config.yaml --json doctor
```

Chỉ thêm `--online` khi chủ động muốn gọi các endpoint ngoài:

```bash
uv run desk --config config.yaml --json doctor --online
```

## Luồng sử dụng

```bash
uv run desk --config config.yaml --json sync
uv run desk --config config.yaml --json screen
uv run desk --config config.yaml --json analyze BTCUSDT
uv run desk --config config.yaml --json tickets
uv run desk --config config.yaml --json approve TICKET_ID
uv run desk --config config.yaml --json orders
```

`sync` lấy balance/vị thế Spot. `screen` chỉ xếp hạng bốn cặp allowlist.
`analyze` ghi snapshot, committee result, decision và báo cáo tiếng Việt vào
`artifacts/crypto/`. Risk engine dùng `Decimal` và tạo ticket độc lập với LLM.

Testnet execution chỉ được bật sau smoke test read-only:

```dotenv
BINANCE_ENV=testnet
TESTNET_EXECUTION_ENABLED=1
LIVE_EXECUTION_ENABLED=0
```

Mainnet cần đồng thời đủ ba cổng:

1. `BINANCE_ENV=mainnet`;
2. `LIVE_EXECUTION_ENABLED=1`;
3. approval từ Telegram user trong allowlist kèm confirmation code còn hạn năm
   phút.

Confirmation code chỉ được tạo cục bộ:

```bash
uv run desk --config config.yaml live-code TICKET_ID
```

Sau đó Telegram user đã xác thực gửi:

```text
/crypto-desk approve TICKET_ID CODE
```

Hermes lấy user ID từ metadata Telegram; tham số sau ticket luôn là code, không
phải user ID. Không gửi code vào log hoặc chat khác.

Nếu submit timeout hoặc trạng thái không rõ, ticket chuyển sang
`RECONCILE_REQUIRED`. Không approve lại và không gửi lại lệnh. Dùng
`desk --json orders`, chạy `desk --json health` và đối chiếu Binance theo
`listClientOrderId`; chỉ tiếp tục sau khi trạng thái đã được reconcile.

## Lịch Hermes

Liên kết skill và wrapper:

```bash
mkdir -p ~/.hermes/skills ~/.hermes/scripts
ln -s "$PWD/hermes/crypto-desk" ~/.hermes/skills/crypto-desk
ln -s "$PWD/scripts/health.sh" ~/.hermes/scripts/crypto-desk-health.sh
ln -s "$PWD/scripts/daily.sh" ~/.hermes/scripts/crypto-desk-daily.sh
```

Tạo đúng hai cron job UTC:

```bash
hermes cron create "*/15 * * * *" --no-agent \
  --script crypto-desk-health.sh \
  --deliver telegram --name crypto-desk-health
hermes cron create "15 0 * * *" --no-agent \
  --script crypto-desk-daily.sh \
  --deliver telegram --name crypto-desk-daily
hermes cron list
```

Health chạy mỗi 15 phút. Full analysis chạy mỗi ngày lúc 00:15 UTC. Wrapper không
phát thông báo khi idempotency bucket đã hoàn tất.

OpenBB MCP là tùy chọn cho tra cứu tương tác của Hermes và không nằm trong đường
quyết định hay execution:

```bash
mkdir -p ~/.config/systemd/user
ln -s "$PWD/services/openbb-mcp.service" ~/.config/systemd/user/openbb-mcp.service
systemctl --user daemon-reload
systemctl --user enable --now openbb-mcp
hermes mcp add openbb --url http://127.0.0.1:8001/mcp/
hermes mcp test openbb
```

Crypto Desk vẫn phải chạy được khi service tùy chọn này dừng.

## Testnet smoke và rollback

Trình tự smoke tối thiểu:

1. Chạy `doctor --online`, `sync`, `screen`, `analyze BTCUSDT` với execution tắt.
2. Bật riêng `TESTNET_EXECUTION_ENABLED=1`.
3. Tạo và duyệt một ticket Testnet hợp lệ.
4. Xác nhận duy nhất một `listClientOrderId`, entry `LIMIT FOK`, hai child bảo vệ,
   cancel và terminal reconcile.
5. Khởi động lại WSL và xác nhận không phát sinh order chain trùng.

Rollback an toàn:

```dotenv
BINANCE_ENV=testnet
TESTNET_EXECUTION_ENABLED=0
LIVE_EXECUTION_ENABLED=0
```

Khởi động lại scheduler/gateway sau khi đổi `.env`, rồi chạy `doctor`. Thao tác
rollback không xóa database hay artifacts cũ.

## Kiểm thử

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
```
