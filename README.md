# AI Investment Desk

CLI cá nhân cho research đa-agent, health check danh mục và lệnh **IBKR Paper có phê duyệt**.
Live account bị chặn trong mã; công cụ không phải tư vấn đầu tư.

## Cài đặt trên WSL2

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --extra all --extra dev
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

Cài đúng Hermes v0.18.2, tạo Telegram gateway, rồi liên kết skill:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- \
  --branch v2026.7.7.2 --skip-browser
hermes gateway setup
hermes gateway install
hermes gateway start
mkdir -p ~/.hermes/skills
ln -s "$PWD/hermes/investment-desk" ~/.hermes/skills/investment-desk
mkdir -p ~/.hermes/scripts
ln -s "$PWD/scripts/health.sh" ~/.hermes/scripts/investment-desk-health.sh
hermes skills
hermes cron create "every 30m" --no-agent \
  --script ~/.hermes/scripts/investment-desk-health.sh \
  --deliver telegram --name investment-desk-health
```

Nếu user service không ổn định trên WSL, dùng foreground trong tmux theo khuyến nghị Hermes:
`tmux new -s hermes 'hermes gateway run'`. Scheduler chỉ chạy khi gateway đang hoạt động.

Cài service OpenBB MCP:

```bash
mkdir -p ~/.config/systemd/user
ln -s "$PWD/services/openbb-mcp.service" ~/.config/systemd/user/openbb-mcp.service
systemctl --user daemon-reload
systemctl --user enable --now openbb-mcp
hermes mcp add openbb --url http://127.0.0.1:8001/mcp/
hermes mcp test openbb
```

Trên Windows, chạy TWS Offline bằng tài khoản Paper, bật socket API port `7497`, tắt
Read-Only API và cho phép địa chỉ WSL. `desk doctor` phải hiển thị account bắt đầu bằng `DU`.

## Quy trình

```bash
uv run desk doctor
uv run desk sync
uv run desk screen
uv run desk analyze NVDA
uv run desk tickets
uv run desk approve TICKET_ID       # chỉ duyệt dry-run
uv run desk orders --reconcile      # đồng bộ trạng thái/fill từ TWS
uv run desk reflections --evaluate # đánh giá quyết định đủ 20 phiên
```

Chỉ sau paper smoke test mới đặt `PAPER_EXECUTION_ENABLED=1`. Lúc duyệt, hệ thống kiểm tra
lại TTL, user allowlist, độ lệch giá, account `DU`, risk và idempotency. Lệnh mở BUY dùng
bracket; lệnh REDUCE/EXIT dùng một limit SELL vì bracket SELL có thể vô tình mở lại vị thế.

## Giới hạn v1

- Cổ phiếu/ETF, long-only, không margin/short/options/futures/crypto.
- Dữ liệu free-first có thể trễ hoặc thiếu; khi OpenBB và yfinance không khớp, kết quả là
  `NO_TRADE`.
- WSL/Hermes phải đang chạy; `desk health --catch-up` xử lý phiên gần nhất sau khi máy thức.
- Health check hiện phát hiện exposure và thesis cũ; không tự tạo lệnh thoát.
- Reflection dùng adjusted close sau 20 phiên để đánh giá quyết định, không giả làm realized P&L.
  TradingAgents giữ decision log riêng; Hermes không được tự sửa prompt/skill từ reflection.

## Kiểm thử

```bash
uv run pytest
uv run ruff check .
```
