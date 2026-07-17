# Binance Spot Mainnet Canary

Mainnet canary không thuộc quyền thực thi mặc định của kế hoạch này. Nó chỉ được
chạy sau một ủy quyền riêng, rõ ràng của chủ tài khoản sau khi bằng chứng Testnet
đã được duyệt. Hiện tại: **NOT AUTHORIZED / NOT RUN**.

## Bằng chứng offline bắt buộc

Fake-broker matrix phải chứng minh không có lời gọi submit thật khi:

| Environment | Live flag | Approval | Kết quả yêu cầu |
|---|---:|---|---|
| testnet | 1 | code hợp lệ | không Mainnet submit |
| mainnet | 0 | code hợp lệ | blocked |
| mainnet | 1 | code sai | blocked |
| mainnet | 1 | local actor | blocked |
| mainnet | 1 | Telegram hợp lệ, 25,01 USDT | blocked |

Kết quả 2026-07-17: **PASS, 5 tests**. Lệnh đã chạy:

```bash
pytest tests/test_execution.py::test_mainnet_requires_all_three_gates \
  tests/test_execution.py::test_wrong_actor_expired_ticket_and_mainnet_local_channel_are_blocked \
  tests/test_execution.py::test_mainnet_initial_cap_blocks_ticket_over_twenty_five_usdt
```

Các nhánh bị chặn ghi nhận zero fake-broker submit. Không có authenticated Mainnet
client hoặc endpoint submit thật nào được gọi.

Giới hạn 25 USDT chỉ được gỡ tự động sau ít nhất 20 Mainnet order chain terminal
đã reconcile; không sửa database để vượt gate.

## Checklist xin ủy quyền

- Testnet read-only, OTOCO lifecycle, cancel, reconcile và restart idempotency đều
  có bằng chứng `PASS`.
- Mainnet key chỉ có Spot trading, withdrawal bị tắt, không có Margin/derivatives
  permission và có IP restriction.
- `TELEGRAM_HOME_CHANNEL` và user allowlist đã được kiểm tra.
- Hermes gateway process được inject riêng `HERMES_TELEGRAM_INGRESS_SECRET`;
  direct CLI shell không có secret/proof bị chặn.
- Chủ tài khoản đã xem ticket, entry/stop/target và chấp nhận rủi ro.
- Có lệnh rollback sẵn trước khi submit.

## Canary sau khi được ủy quyền riêng

1. Đặt `BINANCE_ENV=mainnet`, `LIVE_EXECUTION_ENABLED=1`.
2. Chạy `doctor --online`; dừng nếu bất kỳ check nào đỏ.
3. Tạo local five-minute code bằng `desk live-code TICKET_ID`.
4. Telegram user allowlisted duyệt một ticket không quá 25 USDT.
5. Ghi redacted ticket ID, `listClientOrderId`, UTC timestamps và trạng thái.
6. Reconcile tới terminal; không submit ticket thứ hai.
7. Rollback ngay:

```dotenv
BINANCE_ENV=testnet
TESTNET_EXECUTION_ENABLED=0
LIVE_EXECUTION_ENABLED=0
```

8. Khởi động lại gateway/scheduler, chạy `doctor` và xác nhận Mainnet bị khóa.

Nếu trạng thái không rõ, không tạo code mới và không approve lại. Chuyển sang
runbook Testnet mục `RECONCILE_REQUIRED`, nhưng tra cứu đúng Mainnet environment
và client ID đã ghi.
