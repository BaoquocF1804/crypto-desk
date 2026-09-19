## Kết luận

**Request changes — chưa nên triển khai nguyên trạng.** Ý tưởng tổng thể tốt, nhưng Task 2–3 đang tạo một đường fail-open ảnh hưởng trực tiếp tới risk sizing.

### Các điểm cần sửa trước

1. **[P1] `unpriced` không bảo thủ như kế hoạch khẳng định.**

   `_external_mid()` trả `None` cho cả “không tồn tại cặp” lẫn lỗi mạng/API, sau đó Task 3 cho phép BUY dù không biết giá trị tài sản.

   Nếu tài sản chưa định giá có giá trị thật là `U`:

   - `max_gross` room bị tính dư `0.2 × U`;
   - `min_usdt_reserve` room cũng bị tính dư `0.2 × U`.

   Khuyến nghị:

   - Phân biệt `no_usdt_pair` và `pricing_unavailable`.
   - Khi còn tài sản chưa định giá: vẫn chặn `ACCUMULATE/BUY`, chỉ cho phép `REDUCE/EXIT`.
   - Lấy book ticker theo batch thay vì một request cho mỗi asset. Binance hỗ trợ lấy nhiều hoặc toàn bộ symbols với weight 4, trong khi mỗi symbol riêng lẻ có weight 2. [Binance bookTicker documentation](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#ticker-book-ticker).

2. **[P1] Task 7 vẫn để lọt quyết định `REDUCE/EXIT` thiếu giá.**

   `_validate_manager()` mới chỉ bắt `entry/stop/target` cho `ACCUMULATE`. Với vị thế đang tồn tại, `REDUCE` có cả ba giá bằng `None` vẫn qua committee, rồi đụng `assert decision.entry is not None` ngoài `try` tại [service.py](/home/bao_quoc/crypto-desk-rewrite/src/crypto_desk/service.py:414), làm `analyze()` crash thay vì fail-closed.

   Cần yêu cầu đủ `entry`, `stop`, `target` cho **mọi actionable action** và thêm test `REDUCE` có position nhưng thiếu giá → `NO_TRADE`.

3. **[P2] News reliability còn ba lỗ hổng.**

   - URL CoinDesk trong plan có dấu `/` cuối và hiện trả `308`; `httpx.Client` đang không follow redirect. URL không có dấu `/` trả `200`: [CoinDesk RSS](https://www.coindesk.com/arc/outboundfeeds/rss).
   - Task 5 cache cả kết quả rỗng khi tất cả feeds lỗi, khiến feed phục hồi vẫn bị NO_TRADE thêm 300 giây. Chỉ nên cache khi có ít nhất một feed thành công, hoặc dùng negative TTL ngắn.
   - Một feed có bài future-dated vẫn làm `news.as_of` nằm trong tương lai và đầu độc toàn bộ feeds. Cần test “feed tốt + feed future-dated” và bỏ item/feed sai thay vì làm hỏng tập hợp.

4. **[P2] Task 10 có hướng đúng nhưng test chưa chứng minh lời hứa “mọi gate chạy lại”.**

   Code hiện ghi `SUBMITTING` trước khi gọi Binance nên resume đúng crash window là hợp lý. Nên bổ sung test resume bị chặn bởi:

   - ticket hết hạn;
   - quote lệch trên 0,5%;
   - Mainnet thiếu code/proof mới;
   - approval row không tồn tại hoặc không phải `APPROVE`.

5. **[P3] Task 1 chưa chạy được đúng như viết.**

   Trong môi trường hiện tại, `uv` nằm ở `~/.local/bin/uv` nhưng không có trong `PATH`; kể cả gọi đầy đủ thì `uv run pytest` vẫn gặp launcher cũ. Nên dùng đường dẫn tương đối và thêm bước kiểm tra `command -v uv`, tránh hard-code `/home/bao_quoc/...`.

### Phần đáng giữ

Task 4, 6, 8, 9 và ý tưởng cốt lõi của Task 10 đều hợp lý. Cấu trúc TDD, commit nhỏ và không thêm dependency cũng tốt. Tuy nhiên, plan gồm nhiều subsystem độc lập; nên tách thành 3–4 nhóm PR: portfolio/risk, data reliability, committee validation, execution/operations.

Kiểm chứng hiện trạng:

- `110 passed` khi chạy qua `.venv/bin/python -m pytest -q`;
- Ruff sạch, 22 files đúng format;
- `uv lock --check` sạch;
- `uv run pytest -q` hiện fail exit 127 do launcher cũ;
- không sửa file nào; plan vẫn đang untracked.

::code-comment{title="[P1] Unpriced exposure is not conservative" body="Removing both unpriced guards permits BUY decisions with unknown portfolio value. Missing value can overstate max_gross and USDT-reserve room; keep BUY fail-closed and allow only risk-reducing REDUCE/EXIT until every material position is valued." file="/home/bao_quoc/crypto-desk-rewrite/docs/superpowers/plans/2026-07-17-availability-improvements.md" start=177 priority=1}
::code-comment{title="[P1] Require prices for every actionable action" body="The proposed validator requires entry/stop/target only for ACCUMULATE. REDUCE or EXIT with an existing position and null prices can pass committee validation and later hit an assertion outside the service's guarded sizing block." file="/home/bao_quoc/crypto-desk-rewrite/docs/superpowers/plans/2026-07-17-availability-improvements.md" start=733 priority=1}
::code-comment{title="[P2] Do not negative-cache total feed failure" body="This stores and reuses an empty result when all feeds fail, delaying recovery for the full 300-second TTL. Cache only a result backed by at least one successful feed, or use a much shorter negative TTL." file="/home/bao_quoc/crypto-desk-rewrite/docs/superpowers/plans/2026-07-17-availability-improvements.md" start=452 priority=2}
::code-comment{title="[P2] Example URL redirects under current client" body="The trailing-slash CoinDesk URL currently returns HTTP 308, while PublicDataClient does not enable redirect following. Use https://www.coindesk.com/arc/outboundfeeds/rss or explicitly add and test redirect handling." file="/home/bao_quoc/crypto-desk-rewrite/docs/superpowers/plans/2026-07-17-availability-improvements.md" start=1181 priority=2}