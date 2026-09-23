# Audit Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đường từ quyết định của hội đồng đến ticket ngừng đứt im lặng, và ba lớp lỗi đã giết 71,6% run ngừng tái diễn.

**Architecture:** Năm sửa chữa độc lập trên các đường đang chạy, không thêm module mới, không đổi schema SQLite. `_create_ticket` trả kèm lý do bị chặn và service tự `sync()` khi snapshot cũ. `_call` dùng nốt lượt attempt thứ hai cho lỗi nhất thời. `daily()` chạy trên cutoff live, và bucket quá khứ chỉ replay reflection. Ngưỡng lệch entry của committee lấy từ `risk.max_quote_deviation` thay vì hằng số riêng. Mỗi news item mang nhãn `relevance`.

**Tech Stack:** Python 3.12, `Decimal`, dataclass `frozen=True, slots=True`, Pydantic v2, SQLite qua `Store`, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-audit-remediation-design.md` — năm khiếm khuyết, bằng chứng đo được và quyết định thiết kế cho từng cái. Đọc spec trước; plan này không lặp lại lập luận trong đó.

## Global Constraints

- Python `>=3.12,<3.13`. Ruff `line-length = 100`, `target-version = "py312"`.
- Mọi số tiền và tỉ lệ dùng `Decimal`, không `float`.
- Dataclass mới: `@dataclass(frozen=True, slots=True)`.
- Mọi file `.py` mở đầu bằng `from __future__ import annotations`.
- Văn bản hướng tới người dùng và hướng tới model: **tiếng Việt có dấu**. Giữ nguyên JSON key, enum, symbol, evidence ID.
- Không đụng `risk.py`, `broker.py`, `store.py`, `dispatcher.py`, `runner.py`.
- Không đổi schema SQLite.
- Chạy test: `uv run pytest`. Lint: `uv run ruff check src tests`.
- Baseline tại `ae17034`: **332 passed**, ruff sạch. Sau Task 5 phải là **345 passed** (332 + 13 test mới; test bị sửa trong Task 3 được đổi tên và đổi khẳng định, không bị xoá).

## Review Focus

Năm lớp đầu vào spec ngụ ý nhưng không task nào tự nhiên chạm tới. Mỗi dòng đã được gắn test vào task sở hữu đoạn mã đó.

1. `sync()` ném lỗi giữa `analyze` (mạng chập, key hết hạn) — run vẫn phải ghi đủ artifact và trả `AnalysisRun`; ticket là hệ quả của nghiên cứu, không phải điều kiện của nó. → Task 1, Step 11.
2. Broker trả snapshot của môi trường khác (`mainnet` trong lúc config là `testnet`) — không được dùng để đúc ticket. Lớp chặn này đang có ở `_create_ticket` và không được rơi mất khi tách hàm. → Task 1, Step 13.
3. Lỗi 429 ở stage **specialist** chứ không phải manager — run phải đi tiếp và không mất report của các stage đã xong. → Task 2, Step 7.
4. Entry lệch **đúng bằng** ngưỡng — phải được chấp nhận. Phép so sánh là `>`, không phải `>=`; sửa nhầm ở đây làm hỏng đúng biên mà hội đồng hay chạm. → Task 4, Step 7.
5. News item thiếu `title` (feed hỏng, phần tử rỗng) — hàm gắn nhãn không được ném. Một RSS lỗi không được quyền quyết định desk có chạy hay không. → Task 5, Step 7.

---

## File Structure

| File | Trách nhiệm sau plan | Task |
|---|---|---|
| `src/crypto_desk/service.py` | `_fresh_portfolio` mới; `_create_ticket` trả tuple; `analyze` ghi `ticket_blocked.json`; `daily` chạy live và tách nhánh backfill | 1, 3 |
| `src/crypto_desk/cli.py` | `analyze` và `daily` dựng service có broker; hai factory truyền `max_entry_deviation` | 1, 4 |
| `src/crypto_desk/committee.py` | `_call` retry lỗi nhất thời; ngưỡng lệch entry thành tham số; prompt `news` mô tả nhãn `relevance`; `SPECIALIST_EVIDENCE` nhận thêm `symbol_news_count` | 2, 4, 5 |
| `src/crypto_desk/data.py` | `tag_news_relevance` mới; `news_payload` mang nhãn và đếm | 5 |
| `src/crypto_desk/vn_data.py` | `news_item` dùng chung `tag_news_relevance` | 5 |
| `src/crypto_desk/vn_prompts.py` | prompt `news` và `VN_SPECIALIST_EVIDENCE` khớp với nhãn mới | 5 |
| `tests/test_service_cli.py` | test ticket bị chặn, sync tự động, daily live, daily backfill | 1, 3 |
| `tests/test_committee.py` | test retry, test ngưỡng lệch entry | 2, 4 |
| `tests/test_data.py` | test gắn nhãn news | 5 |

---

### Task 1: Ticket không còn chết im lặng

**Files:**
- Modify: `src/crypto_desk/service.py:202` (call site trong `analyze`)
- Modify: `src/crypto_desk/service.py:509-596` (`_create_ticket`)
- Modify: `src/crypto_desk/service.py:496` (chèn `_fresh_portfolio` ngay trước `_position_quantity`)
- Modify: `src/crypto_desk/cli.py:123`, `src/crypto_desk/cli.py:136`
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Consumes: `CryptoDeskService.sync() -> PortfolioSnapshot` (đã có, [service.py:60](../../../src/crypto_desk/service.py)); `Store.latest_snapshot(environment: str) -> PortfolioSnapshot | None`.
- Produces:
  - `CryptoDeskService._fresh_portfolio(self, now: datetime) -> PortfolioSnapshot | None`
  - `CryptoDeskService._create_ticket(self, decision: ResearchDecision, evidence: EvidenceSnapshot | None, cutoff: datetime) -> tuple[str | None, str | None]` — `(ticket_id, blocked_reason)`. Đúng một trong hai phần tử khác `None`, trừ trường hợp action không actionable thì cả hai đều `None`.
  - Artifact mới `ticket_blocked.json` với hình dạng `{"reason": "<mã lý do>"}`. Các mã: `cutoff_outside_5min_window`, `no_portfolio_snapshot_within_5min`, `unpriced_positions_block_accumulate`, `decision_missing_entry_or_stop`, `protection_order_list_ambiguous`, `sizing:<thông điệp ValueError>`.

- [ ] **Step 1: Viết test cho việc tự sync khi snapshot cũ**

Thêm vào cuối `tests/test_service_cli.py`:

```python
def test_accumulate_syncs_the_portfolio_when_the_stored_snapshot_is_stale(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.save_snapshot(
        PortfolioSnapshot(
            environment="testnet",
            nav_usdt=Decimal("10000"),
            free_usdt=Decimal("10000"),
            positions=(),
            open_orders=(),
            as_of=(NOW - timedelta(days=3)).isoformat(),
        )
    )
    service = CryptoDeskService(
        settings,
        store,
        broker=FakeBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.ticket_id is not None
    assert store.ticket(result.ticket_id).status == "PENDING"
    assert not (result.report_dir / "ticket_blocked.json").exists()
```

- [ ] **Step 2: Chạy test, xác nhận nó hỏng**

Run: `uv run pytest tests/test_service_cli.py::test_accumulate_syncs_the_portfolio_when_the_stored_snapshot_is_stale -v`
Expected: FAIL — `assert None is not None`, vì snapshot cũ 3 ngày bị `_create_ticket` từ chối và không có gì thay thế.

- [ ] **Step 3: Viết `_fresh_portfolio`**

Chèn vào `src/crypto_desk/service.py` ngay trước `def _position_quantity`:

```python
    def _fresh_portfolio(self, now: datetime) -> PortfolioSnapshot | None:
        """Snapshot còn hạn 5 phút, tự sync khi cũ.

        ``analyze`` không tự gọi ``sync``, nên nếu không làm ở đây thì ticket chỉ
        ra đời khi một lệnh khác tình cờ sync trong 5 phút trước đó — xác suất
        thực đo được trên repo này là 0/7.

        Lỗi broker ở đây trả ``None`` chứ không lan ra ngoài: ticket là hệ quả
        của nghiên cứu, không phải điều kiện của nó.
        """
        snapshot = self.store.latest_snapshot(self.settings.binance.environment)
        if snapshot is not None and snapshot.environment == self.settings.binance.environment:
            as_of = datetime.fromisoformat(snapshot.as_of).astimezone(UTC)
            if as_of <= now and now - as_of <= timedelta(minutes=5):
                return snapshot
        if self.broker is None:
            return None
        try:
            refreshed = self.sync()
        except Exception:
            return None
        if refreshed.environment != self.settings.binance.environment:
            return None
        return refreshed
```

- [ ] **Step 4: Nối `_fresh_portfolio` vào `_create_ticket`**

Trong `src/crypto_desk/service.py`, thay khối tại dòng 520-531:

```python
        portfolio = self.store.latest_snapshot(self.settings.binance.environment)
        if portfolio is None or portfolio.environment != self.settings.binance.environment:
            return None
        if decision.action == "ACCUMULATE" and any(
            position.get("unpriced") for position in portfolio.positions
        ):
            return None
        portfolio_as_of = datetime.fromisoformat(portfolio.as_of).astimezone(UTC)
        if portfolio_as_of > now or now - portfolio_as_of > timedelta(minutes=5):
            return None
```

bằng:

```python
        portfolio = self._fresh_portfolio(now)
        if portfolio is None:
            return None, "no_portfolio_snapshot_within_5min"
        if decision.action == "ACCUMULATE" and any(
            position.get("unpriced") for position in portfolio.positions
        ):
            return None, "unpriced_positions_block_accumulate"
```

- [ ] **Step 5: Đổi chữ ký và các lối thoát còn lại của `_create_ticket`**

Trong cùng hàm, đổi dòng khai báo trả về và bốn lối thoát còn lại:

```python
    def _create_ticket(
        self,
        decision: ResearchDecision,
        evidence: EvidenceSnapshot | None,
        cutoff: datetime,
    ) -> tuple[str | None, str | None]:
        if decision.action not in {"ACCUMULATE", "REDUCE", "EXIT"} or evidence is None:
            return None, None
        now = self._now()
        if cutoff > now or now - cutoff > timedelta(minutes=5):
            return None, "cutoff_outside_5min_window"
```

Ba lối thoát phía dưới:

```python
        if decision.entry is None or decision.stop is None:
            return None, "decision_missing_entry_or_stop"
```

```python
                if len(protection_ids) != 1:
                    return None, "protection_order_list_ambiguous"
```

```python
        except ValueError as exc:
            return None, f"sizing:{exc}"
        self.store.save_ticket(ticket)
        return ticket.id, None
```

- [ ] **Step 6: Cập nhật call site trong `analyze`**

Thay `src/crypto_desk/service.py:202`:

```python
        ticket_id = self._create_ticket(decision, snapshot, effective_cutoff)
```

bằng:

```python
        ticket_id, blocked_by = self._create_ticket(decision, snapshot, effective_cutoff)
        if blocked_by:
            # Một quyết định actionable mà không ra ticket là sự kiện phải điều
            # tra được, không phải một None lặng lẽ.
            self._write_json(report_dir / "ticket_blocked.json", {"reason": blocked_by})
```

- [ ] **Step 7: Cấp broker cho `analyze` và `daily` trong CLI**

`src/crypto_desk/cli.py:123`:

```python
    service = _service(settings, broker=True)
```

`src/crypto_desk/cli.py:136`:

```python
    result = _service(settings, broker=True).daily(due=due, catch_up=catch_up)
```

- [ ] **Step 8: Chạy test, xác nhận nó đạt**

Run: `uv run pytest tests/test_service_cli.py::test_accumulate_syncs_the_portfolio_when_the_stored_snapshot_is_stale -v`
Expected: PASS

- [ ] **Step 9: Viết test cho lý do bị chặn được ghi ra artifact**

```python
def test_blocked_ticket_records_the_reason_in_the_run_artifacts(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.ticket_id is None
    blocked = json.loads((result.report_dir / "ticket_blocked.json").read_text(encoding="utf-8"))
    assert blocked["reason"] == "no_portfolio_snapshot_within_5min"
```

- [ ] **Step 10: Chạy test**

Run: `uv run pytest tests/test_service_cli.py::test_blocked_ticket_records_the_reason_in_the_run_artifacts -v`
Expected: PASS

- [ ] **Step 11: Viết test Review Focus 1 — sync hỏng không được làm hỏng run**

```python
def test_broker_failure_during_sync_does_not_abort_the_analysis(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    class ExplodingBroker(FakeBroker):
        def account_snapshot(self):
            raise ConnectionError("binance unreachable")

    service = CryptoDeskService(
        settings,
        store,
        broker=ExplodingBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.decision.action == "ACCUMULATE"
    for name in ("evidence.json", "analysts.json", "decision.json", "report.md"):
        assert (result.report_dir / name).is_file()
    assert result.ticket_id is None
    blocked = json.loads((result.report_dir / "ticket_blocked.json").read_text(encoding="utf-8"))
    assert blocked["reason"] == "no_portfolio_snapshot_within_5min"
```

- [ ] **Step 12: Chạy test**

Run: `uv run pytest tests/test_service_cli.py::test_broker_failure_during_sync_does_not_abort_the_analysis -v`
Expected: PASS

- [ ] **Step 13: Viết test Review Focus 2 — snapshot sai môi trường bị từ chối**

```python
def test_snapshot_from_another_environment_never_mints_a_ticket(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    class MainnetBroker(FakeBroker):
        def account_snapshot(self):
            return PortfolioSnapshot(
                environment="mainnet",
                nav_usdt=Decimal("10000"),
                free_usdt=Decimal("10000"),
                positions=(),
                open_orders=(),
                as_of=NOW.isoformat(),
            )

    service = CryptoDeskService(
        settings,
        store,
        broker=MainnetBroker(),
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.analyze("BTCUSDT")

    assert result.ticket_id is None
    blocked = json.loads((result.report_dir / "ticket_blocked.json").read_text(encoding="utf-8"))
    assert blocked["reason"] == "no_portfolio_snapshot_within_5min"
```

- [ ] **Step 14: Chạy test**

Run: `uv run pytest tests/test_service_cli.py::test_snapshot_from_another_environment_never_mints_a_ticket -v`
Expected: PASS

- [ ] **Step 15: Chạy toàn bộ suite và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **336 passed**, ruff sạch.

- [ ] **Step 16: Commit**

```bash
git add src/crypto_desk/service.py src/crypto_desk/cli.py tests/test_service_cli.py
git commit -m "fix: tự sync danh mục và nói ra lý do khi ticket không được tạo

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Một lỗi 429 không huỷ trắng cả run

**Files:**
- Modify: `src/crypto_desk/committee.py:21` (thêm hằng số cạnh `MAX_ENTRY_DEVIATION`)
- Modify: `src/crypto_desk/committee.py:783-797` (khối `except ProviderError` trong `_call`)
- Test: `tests/test_committee.py`

**Interfaces:**
- Consumes: `ProviderError.category: str` với các giá trị do `_provider_error_category` sinh ra — `authentication`, `model_unavailable`, `rate_limit`, `network`, `provider_error`.
- Produces: `RETRYABLE_PROVIDER_ERRORS: frozenset[str]` export từ `crypto_desk.committee`.

- [ ] **Step 1: Viết test cho việc retry lỗi nhất thời**

Thêm vào `tests/test_committee.py`:

```python
def test_rate_limited_stage_is_retried_before_the_run_is_abandoned():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate
    remaining = {"manager": 1}

    def flaky(**kwargs):
        stage = kwargs["stage"]
        if remaining.get(stage):
            remaining[stage] -= 1
            raise ProviderError("rate_limit")
        return original_generate(**kwargs)

    fake_llm.generate = flaky

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is True
    assert result.decision.action == "ACCUMULATE"
    manager_calls = [call for call in result.calls if call.stage == "manager"]
    assert [call.status for call in manager_calls] == ["failure", "success"]
    assert manager_calls[0].error_category == "rate_limit"
```

- [ ] **Step 2: Chạy test, xác nhận nó hỏng**

Run: `uv run pytest tests/test_committee.py::test_rate_limited_stage_is_retried_before_the_run_is_abandoned -v`
Expected: FAIL — `result.decision.decided` là `False`, `_call` ném ngay ở lần đầu.

- [ ] **Step 3: Thêm hằng số**

Trong `src/crypto_desk/committee.py`, ngay dưới dòng 21:

```python
MAX_ENTRY_DEVIATION = Decimal("0.02")
# Hai loại lỗi này là nhất thời theo định nghĩa: 429 hết hạn, mạng chập rồi
# thông. Ném ngay ở stage cuối tức là vứt 8 call đã trả tiền — đo được 14 lần
# trên repo này. Khoá sai hay model không tồn tại thì retry chỉ đốt thời gian.
RETRYABLE_PROVIDER_ERRORS = frozenset({"rate_limit", "network"})
```

- [ ] **Step 4: Sửa khối `except ProviderError` trong `_call`**

Thay dòng `raise` cuối khối (`src/crypto_desk/committee.py:797`) bằng:

```python
                if attempt >= 2 or exc.category not in RETRYABLE_PROVIDER_ERRORS:
                    raise
                last_error = f"provider:{exc.category}"
```

Không thêm `sleep`: `GeminiStructuredClient` đã giữ nhịp `min_interval_seconds` và tự ngủ 65s sau mỗi 429, nên lượt thứ hai tự nhiên cách lượt đầu ~130s. Tổng request tối đa cho một stage là 6 (2 attempt × 3 lần thử trong client).

- [ ] **Step 5: Chạy test**

Run: `uv run pytest tests/test_committee.py::test_rate_limited_stage_is_retried_before_the_run_is_abandoned -v`
Expected: PASS

- [ ] **Step 6: Viết test cho lỗi không nhất thời phải hỏng ngay**

```python
def test_authentication_error_aborts_the_run_without_retrying():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def unauthorized(**kwargs):
        if kwargs["stage"] == "manager":
            raise ProviderError("authentication")
        return original_generate(**kwargs)

    fake_llm.generate = unauthorized

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is False
    manager_calls = [call for call in result.calls if call.stage == "manager"]
    assert len(manager_calls) == 1
    assert manager_calls[0].error_category == "authentication"
```

- [ ] **Step 7: Viết test Review Focus 3 — 429 ở specialist không xoá report đã có**

```python
def test_rate_limited_specialist_is_retried_and_later_reports_survive():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate
    remaining = {"liquidity": 1}

    def flaky(**kwargs):
        stage = kwargs["stage"]
        if remaining.get(stage):
            remaining[stage] -= 1
            raise ProviderError("rate_limit")
        return original_generate(**kwargs)

    fake_llm.generate = flaky

    result = CryptoCommittee(fake_llm).run(valid_snapshot())

    assert result.decision.decided is True
    assert set(result.reports) == {
        "technical",
        "liquidity",
        "news",
        "derivatives",
        "bull_round_1",
        "bear_round_1",
        "bull_round_2",
        "bear_round_2",
    }
    assert result.skipped_specialists == ()
```

- [ ] **Step 8: Chạy cả hai test mới**

Run: `uv run pytest tests/test_committee.py -k "authentication_error_aborts or rate_limited_specialist" -v`
Expected: 2 passed

- [ ] **Step 9: Chạy toàn bộ suite và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **339 passed**, ruff sạch.

- [ ] **Step 10: Commit**

```bash
git add src/crypto_desk/committee.py tests/test_committee.py
git commit -m "fix: thử lại stage bị rate limit thay vì vứt cả run

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `daily()` dựng được evidence

**Files:**
- Modify: `src/crypto_desk/service.py:212-251` (`daily`)
- Test: `tests/test_service_cli.py` — sửa `test_daily_catch_up_uses_most_recent_missing_utc_day` (dòng 468), thêm hai test mới

**Interfaces:**
- Consumes: `CryptoDeskService.screen(cutoff: datetime | None = None)` và `CryptoDeskService.analyze(symbol: str, cutoff: datetime | None = None)` — cả hai suy ra `live=cutoff is None`. Không đổi chữ ký nào.
- Produces: `daily()` thêm một giá trị `status` mới là `"REFLECTIONS_ONLY"`, bên cạnh `"NOT_DUE"`, `"ALREADY_DONE"`, `"COMPLETED"`. Payload của nhánh này có `screen: []` và `run_ids: []`.

**Lưu ý về một test bị thay thế:** `test_daily_catch_up_uses_most_recent_missing_utc_day` hiện khẳng định catch-up một ngày quá khứ trả `"COMPLETED"`. Khẳng định đó đang bảo vệ đúng hành vi hỏng đã giết 221 run. Nó được đổi, không được xoá — bucket vẫn phải là `2026-07-16`, chỉ status đổi.

- [ ] **Step 1: Viết test cho việc daily chạy trên cutoff live**

Thêm vào `tests/test_service_cli.py`:

```python
def test_daily_builds_live_evidence_instead_of_a_backdated_cutoff(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)

    class ClockAwareBuilder(FakeBuilder):
        def __init__(self):
            super().__init__()
            self.live_flags: list[bool] = []

        def build(self, symbol, cutoff, *, live=False):
            self.live_flags.append(live)
            return super().build(symbol, cutoff, live=live)

    builder = ClockAwareBuilder()
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=builder,
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    result = service.daily()

    assert result["status"] == "COMPLETED"
    assert result["run_ids"]
    assert builder.live_flags and all(builder.live_flags)
```

- [ ] **Step 2: Chạy test, xác nhận nó hỏng**

Run: `uv run pytest tests/test_service_cli.py::test_daily_builds_live_evidence_instead_of_a_backdated_cutoff -v`
Expected: FAIL — `builder.live_flags` toàn `False`, vì `daily` truyền một cutoff cố định.

- [ ] **Step 3: Viết lại thân `daily`**

Thay `src/crypto_desk/service.py:212-251` bằng:

```python
    def daily(
        self,
        *,
        due: bool = False,
        catch_up: bool = False,
    ) -> dict[str, Any]:
        now = self._now()
        target_date = now.date()
        if now.time() < time(0, 15):
            target_date -= timedelta(days=1)
            if due and not catch_up:
                return {"status": "NOT_DUE", "bucket": str(target_date)}
        backfill = False
        if catch_up:
            for days_ago in range(31):
                candidate = target_date - timedelta(days=days_ago)
                if not self.store.scheduled_done(
                    "daily",
                    candidate.isoformat(),
                ):
                    target_date = candidate
                    backfill = days_ago > 0
                    break
        bucket = target_date.isoformat()
        if self.store.scheduled_done("daily", bucket):
            return {"status": "ALREADY_DONE", "bucket": bucket}

        if backfill:
            # Sổ lệnh, funding và RSS 48 giờ của một ngày đã bị bỏ qua không dựng
            # lại được. Chỉ reflection là dựng lại được thật, vì nó đọc klines
            # lịch sử theo khoảng thời gian.
            reflection_run_ids = self.refresh_reflections(
                datetime.combine(target_date, time(0, 15), tzinfo=UTC)
            )
            self.store.mark_scheduled("daily", bucket)
            return {
                "status": "REFLECTIONS_ONLY",
                "bucket": bucket,
                "screen": [],
                "run_ids": [],
                "reflection_run_ids": reflection_run_ids,
            }

        # Không truyền cutoff: screen và analyze tự lấy mốc live của chính chúng.
        # Một cutoff dựng sẵn cộng với thời gian fetch luôn khiến mọi fetched_at
        # rơi vào "tương lai" — đúng dòng đã giết 221 run.
        reflection_run_ids = self.refresh_reflections(now)
        screen_results = self.screen()
        runs = [
            self.analyze(result.symbol).run_id
            for result in screen_results
            if result.passes
        ]
        self.store.mark_scheduled("daily", bucket)
        return {
            "status": "COMPLETED",
            "bucket": bucket,
            "screen": [to_jsonable(result) for result in screen_results],
            "run_ids": runs,
            "reflection_run_ids": reflection_run_ids,
        }
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_service_cli.py::test_daily_builds_live_evidence_instead_of_a_backdated_cutoff -v`
Expected: PASS

- [ ] **Step 5: Sửa test catch-up đang khẳng định hành vi hỏng**

Trong `tests/test_service_cli.py:468`, đổi ba dòng cuối của `test_daily_catch_up_uses_most_recent_missing_utc_day`:

```python
    result = service.daily(catch_up=True)

    assert result["status"] == "REFLECTIONS_ONLY"
    assert result["bucket"] == "2026-07-16"
    assert result["run_ids"] == []
```

Đổi luôn tên hàm cho khớp điều nó thật sự khẳng định:

```python
def test_daily_catch_up_replays_reflections_for_a_missed_day_without_analysing(tmp_path):
```

- [ ] **Step 6: Viết test cho việc bucket quá khứ vẫn được đánh dấu xong**

```python
def test_reflections_only_bucket_is_marked_done_and_never_repeats(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    store.mark_scheduled("daily", "2026-07-17")
    service = CryptoDeskService(
        settings,
        store,
        evidence_builder=FakeBuilder(),
        committee=FakeCommittee(),
        now=lambda: NOW,
    )

    first = service.daily(catch_up=True)
    second = service.daily(catch_up=True)

    assert first["status"] == "REFLECTIONS_ONLY"
    assert first["bucket"] == "2026-07-16"
    assert second["bucket"] == "2026-07-15"
```

- [ ] **Step 7: Chạy hai test catch-up**

Run: `uv run pytest tests/test_service_cli.py -k "catch_up or reflections_only_bucket" -v`
Expected: 2 passed

- [ ] **Step 8: Chạy toàn bộ suite và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **341 passed**, ruff sạch.

- [ ] **Step 9: Commit**

```bash
git add src/crypto_desk/service.py tests/test_service_cli.py
git commit -m "fix: daily dựng evidence live, catch-up chỉ replay reflection

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Một ngưỡng lệch entry duy nhất

**Files:**
- Modify: `src/crypto_desk/committee.py:556-594` (`CryptoCommittee.__init__`)
- Modify: `src/crypto_desk/committee.py:824-843` (`_validate_manager`)
- Modify: `src/crypto_desk/cli.py:572-578` (`_service`) và `src/crypto_desk/cli.py:604-617` (`_vn_service`)
- Test: `tests/test_committee.py`

**Interfaces:**
- Consumes: `RiskSettings.max_quote_deviation: Decimal` (mặc định `Decimal("0.005")`, đã được `_validate` ràng buộc trong khoảng `(0, 1]` tại [config.py:155](../../../src/crypto_desk/config.py)).
- Produces: `CryptoCommittee.__init__` nhận thêm keyword `max_entry_deviation: Decimal = MAX_ENTRY_DEVIATION`, lưu ở `self.max_entry_deviation`. `MAX_ENTRY_DEVIATION` vẫn là mặc định cho mọi caller không truyền gì, nên các test hiện có không đổi hành vi.

- [ ] **Step 1: Viết test cho ngưỡng lấy từ cấu hình**

Thêm vào `tests/test_committee.py`:

```python
def test_entry_deviation_threshold_comes_from_the_configured_risk_limit():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def one_percent_above_mid(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["entry"] = "101000"
            response["stop"] = "95000"
            response["target"] = "110000"
        return response

    fake_llm.generate = one_percent_above_mid

    result = CryptoCommittee(
        fake_llm,
        max_entry_deviation=Decimal("0.005"),
    ).run(valid_snapshot())

    assert result.decision.action == "NO_TRADE"
    assert "0.50%" in result.decision.reason
```

- [ ] **Step 2: Chạy test, xác nhận nó hỏng**

Run: `uv run pytest tests/test_committee.py::test_entry_deviation_threshold_comes_from_the_configured_risk_limit -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'max_entry_deviation'`.

- [ ] **Step 3: Nhận tham số trong constructor**

Trong `src/crypto_desk/committee.py`, thêm vào danh sách keyword của `__init__` ngay sau `mid_label`:

```python
        mid_label: str = "Binance mid",
        max_entry_deviation: Decimal = MAX_ENTRY_DEVIATION,
```

và thêm vào thân hàm ngay sau `self.mid_label = mid_label`:

```python
        self.max_entry_deviation = max_entry_deviation
```

- [ ] **Step 4: Dùng tham số trong `_validate_manager`**

Thay hai dòng cuối `_validate_manager` (`src/crypto_desk/committee.py:841-842`):

```python
        if abs(decision.entry - snapshot_mid) / snapshot_mid > MAX_ENTRY_DEVIATION:
            raise ValueError(f"entry price deviates more than 2% from {self.mid_label}")
```

bằng:

```python
        if abs(decision.entry - snapshot_mid) / snapshot_mid > self.max_entry_deviation:
            raise ValueError(
                f"entry price deviates more than {self.max_entry_deviation:.2%} "
                f"from {self.mid_label}"
            )
```

- [ ] **Step 5: Chạy test**

Run: `uv run pytest tests/test_committee.py::test_entry_deviation_threshold_comes_from_the_configured_risk_limit -v`
Expected: PASS

- [ ] **Step 6: Truyền ngưỡng từ hai factory**

Trong `src/crypto_desk/cli.py`, thêm một dòng vào lời gọi `CryptoCommittee(...)` trong `_service`, ngay sau `debate_rounds=settings.models.debate_rounds,`:

```python
            max_entry_deviation=settings.risk.max_quote_deviation,
```

Thêm dòng y hệt vào lời gọi `CryptoCommittee(...)` trong `_vn_service`, ngay sau `debate_rounds=settings.models.debate_rounds,`.

- [ ] **Step 7: Viết test Review Focus 4 — lệch đúng bằng ngưỡng thì chấp nhận**

```python
def test_entry_exactly_at_the_deviation_threshold_is_accepted():
    fake_llm = FakeLLM()
    original_generate = fake_llm.generate

    def exactly_at_threshold(**kwargs):
        response = original_generate(**kwargs)
        if kwargs["stage"] == "manager" and "action" in response:
            response["entry"] = "100500"   # đúng +0,50% so với mid 100000
            response["stop"] = "95000"
            response["target"] = "110000"
        return response

    fake_llm.generate = exactly_at_threshold

    result = CryptoCommittee(
        fake_llm,
        max_entry_deviation=Decimal("0.005"),
    ).run(valid_snapshot())

    assert result.decision.action == "ACCUMULATE"
    assert result.decision.entry == Decimal("100500")
```

- [ ] **Step 8: Chạy test**

Run: `uv run pytest tests/test_committee.py::test_entry_exactly_at_the_deviation_threshold_is_accepted -v`
Expected: PASS

- [ ] **Step 9: Chạy toàn bộ suite và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **343 passed**, ruff sạch.

- [ ] **Step 10: Commit**

```bash
git add src/crypto_desk/committee.py src/crypto_desk/cli.py tests/test_committee.py
git commit -m "fix: committee và execution dùng chung một ngưỡng lệch giá

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: News mang nhãn liên quan tới symbol

**Files:**
- Modify: `src/crypto_desk/data.py` (hàm module-level mới, đặt ngay trước `class EvidenceBuilder` ở dòng 342; và `news_payload` ở dòng 544)
- Modify: `src/crypto_desk/vn_data.py` (khối `news_item` trong `VNEvidenceBuilder.build`)
- Modify: `src/crypto_desk/committee.py:115` và prompt `news` ở dòng 51-56
- Modify: `src/crypto_desk/vn_prompts.py:23` và prompt `news`
- Test: `tests/test_data.py`

**Interfaces:**
- Produces: `crypto_desk.data.tag_news_relevance(items: list[dict[str, str]], aliases: tuple[str, ...]) -> list[dict[str, str]]` — trả danh sách mới, mỗi phần tử là bản sao của item cộng khoá `relevance` nhận giá trị `"symbol"` hoặc `"market"`, và item `"symbol"` được xếp trước. Không bỏ phần tử nào.
- Produces: payload evidence `news` có thêm khoá `symbol_news_count: int`; cả hai `SPECIALIST_EVIDENCE` phải liệt kê khoá này, nếu không phép chiếu field ở `_specialist_payload` sẽ cắt mất nó.

- [ ] **Step 1: Viết test cho hàm gắn nhãn**

Thêm vào `tests/test_data.py`:

```python
def test_news_relevance_marks_symbol_matches_and_keeps_market_items():
    items = [
        {"title": "Institutional crypto flows rise"},
        {"title": "Bitcoin ETF inflows hit a record"},
        {"title": "BTC's dominance keeps climbing"},
        {"title": "He wore a dark suit to the hearing"},
    ]

    tagged = tag_news_relevance(items, ("BTC", "bitcoin"))

    assert [item["relevance"] for item in tagged] == [
        "symbol",
        "symbol",
        "market",
        "market",
    ]
    assert [item["title"] for item in tagged][:2] == [
        "Bitcoin ETF inflows hit a record",
        "BTC's dominance keeps climbing",
    ]
    assert len(tagged) == len(items)
```

- [ ] **Step 2: Chạy test, xác nhận nó hỏng**

Thêm `tag_news_relevance` vào khối import từ `crypto_desk.data` ở đầu `tests/test_data.py`, rồi:

Run: `uv run pytest tests/test_data.py::test_news_relevance_marks_symbol_matches_and_keeps_market_items -v`
Expected: FAIL — `ImportError: cannot import name 'tag_news_relevance'`.

- [ ] **Step 3: Viết `tag_news_relevance`**

Trong `src/crypto_desk/data.py`, chèn `import re` giữa `import os` (dòng 4) và `import time` (dòng 5), rồi chèn hàm sau ngay trước `class EvidenceBuilder`:

```python
def tag_news_relevance(
    items: list[dict[str, str]],
    aliases: tuple[str, ...],
) -> list[dict[str, str]]:
    """Gắn nhãn mỗi tin là ``symbol`` hay ``market``, không bỏ tin nào.

    Lọc cứng sẽ biến một ngày không có tin riêng thành ``EvidenceError`` và kéo
    cả desk về NO_TRADE — đổi một khiếm khuyết lấy một khiếm khuyết tệ hơn. Rổ
    tin chung vẫn là bối cảnh thị trường hợp lệ, chỉ là model cần biết đâu là
    tin của chính mã nó đang xét.

    Biên từ được viết tay thay vì ``\\b`` để ``BTC`` không trúng trong
    ``BTCUSDT`` và ``sui`` không trúng trong ``suit``, nhưng vẫn trúng ``BTC's``.
    """
    patterns = [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
        for alias in aliases
        if alias
    ]
    tagged: list[dict[str, str]] = []
    for item in items:
        title = str(item.get("title") or "")
        relevance = "symbol" if any(pattern.search(title) for pattern in patterns) else "market"
        tagged.append({**item, "relevance": relevance})
    tagged.sort(key=lambda item: item["relevance"] != "symbol")
    return tagged
```

`list.sort` ổn định, nên thứ tự theo thời gian trong mỗi nhóm được giữ nguyên.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_data.py::test_news_relevance_marks_symbol_matches_and_keeps_market_items -v`
Expected: PASS

- [ ] **Step 5: Dùng hàm trong `EvidenceBuilder.build`**

Thay `src/crypto_desk/data.py:544`:

```python
        news_payload = {"symbol": symbol, "items": recent_news}
```

bằng:

```python
        tagged_news = tag_news_relevance(
            recent_news,
            (rules.base_asset, self.coingecko_ids[symbol]),
        )
        news_payload = {
            "symbol": symbol,
            "items": tagged_news,
            "symbol_news_count": sum(
                1 for item in tagged_news if item["relevance"] == "symbol"
            ),
        }
```

- [ ] **Step 6: Dùng hàm trong `VNEvidenceBuilder.build`**

Trong `src/crypto_desk/vn_data.py`, đổi dòng import từ `.data` thành:

```python
from .data import EvidenceError, Fetched, _aware, _parse_feed, tag_news_relevance
```

rồi thay `payload` của `news_item`:

```python
            payload={
                "symbol": symbol,
                "items": news_fetched.payload,
            },
```

bằng:

```python
            payload=_vn_news_payload(symbol, news_fetched.payload),
```

và thêm hàm này ngay trước `class VNEvidenceBuilder`:

```python
def _vn_news_payload(symbol: str, items: list[dict[str, str]]) -> dict[str, Any]:
    """Rổ RSS của VN là tin thị trường chung, không theo mã.

    Mọi mã HOSE nhận đúng cùng một rổ CafeF/Vietstock, nên nhãn ``relevance`` là
    thứ duy nhất cho chuyên gia news biết tin nào nói về mã nó đang xét.
    """
    tagged = tag_news_relevance(items, (symbol,))
    return {
        "symbol": symbol,
        "items": tagged,
        "symbol_news_count": sum(1 for item in tagged if item["relevance"] == "symbol"),
    }
```

- [ ] **Step 7: Viết test Review Focus 5 — item thiếu `title` không làm hàm ném**

```python
def test_news_relevance_survives_items_without_a_title():
    items = [{"url": "https://example.test/broken"}, {"title": None}, {}]

    tagged = tag_news_relevance(items, ("BTC", "bitcoin"))

    assert [item["relevance"] for item in tagged] == ["market", "market", "market"]
```

- [ ] **Step 8: Chạy test**

Run: `uv run pytest tests/test_data.py::test_news_relevance_survives_items_without_a_title -v`
Expected: PASS

- [ ] **Step 9: Mở đường cho `symbol_news_count` đi qua phép chiếu field**

`src/crypto_desk/committee.py:115`:

```python
    "news": ("news", ("symbol", "items", "symbol_news_count")),
```

`src/crypto_desk/vn_prompts.py:23`:

```python
    "news": ("news", ("symbol", "items", "symbol_news_count")),
```

- [ ] **Step 10: Nói cho model biết nhãn đó nghĩa là gì**

Thay prompt `news` trong `ROLE_PROMPTS` (`src/crypto_desk/committee.py:51-56`) bằng:

```python
    "news": (
        "Bạn là chuyên viên phân tích tin tức. Chỉ dùng title, URL, published_at, "
        "content_hash và relevance trong evidence News. relevance='symbol' là tin có "
        "nhắc trực tiếp tới mã đang xét; relevance='market' là bối cảnh thị trường "
        "chung, không phải tin của mã này — đừng quy kết nó cho mã. Khi "
        "symbol_news_count bằng 0, phải nói rõ là không có tin riêng của mã. "
        "Đánh giá độ mới và hướng tác động có thể có. Không suy đoán nội dung bài "
        "viết ngoài headline và không dùng dữ liệu giá, thanh khoản hoặc phái sinh."
    ),
```

Thay prompt `news` trong `VN_ROLE_PROMPTS` (`src/crypto_desk/vn_prompts.py`) bằng:

```python
    "news": (
        "Bạn là chuyên viên phân tích tin tức cổ phiếu VN. Chỉ dùng title, URL, published_at "
        "và relevance trong evidence News. Rổ RSS này là tin thị trường chung, không phải tin "
        "riêng của mã: relevance='symbol' là tin có nhắc trực tiếp tới mã đang xét, "
        "relevance='market' là bối cảnh chung và không được quy kết cho mã này. Khi "
        "symbol_news_count bằng 0, phải nói rõ là không có tin riêng của mã. Đánh giá độ mới "
        "và hướng tác động có thể có. Không suy đoán nội dung bài viết ngoài headline và "
        "không dùng dữ liệu giá, thanh khoản hoặc dòng tiền."
    ),
```

- [ ] **Step 11: Chạy toàn bộ suite và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **345 passed**, ruff sạch.

Không test hiện có nào phải sửa trong task này, và đó là điều cần kiểm chứng chứ không phải giả định. Hai chỗ đáng ngờ đã được soi trước: `test_committee_prompts_require_vietnamese_and_isolate_specialists` khẳng định `set(evidence["payload"]) == {"symbol", "items"}` cho vai `news`, nhưng `_specialist_payload` chiếu field bằng `if name in serialized["payload"]`, còn `valid_snapshot()` trong test không có khoá `symbol_news_count` — nên tập được chiếu vẫn đúng bằng hai khoá cũ. Và không test nào khẳng định câu chữ của prompt `news`. Nếu một test vẫn hỏng, sửa khẳng định của nó cho khớp hành vi mới — đừng khôi phục prompt hay field cũ.

- [ ] **Step 12: Commit**

```bash
git add src/crypto_desk/data.py src/crypto_desk/vn_data.py src/crypto_desk/committee.py src/crypto_desk/vn_prompts.py tests/test_data.py
git commit -m "feat: gắn nhãn tin theo mã thay vì đưa rổ tin chung cho mọi symbol

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Kiểm tra cuối cùng

- [ ] **Chạy lại toàn bộ suite và lint từ đầu**

Run: `uv run pytest && uv run ruff check src tests`
Expected: **345 passed**, ruff sạch. (332 baseline + 13 test mới: Task 1 thêm 4, Task 2 thêm 3, Task 3 thêm 2, Task 4 thêm 2, Task 5 thêm 2. Test bị sửa trong Task 3 được đổi tên và đổi khẳng định nên vẫn nằm trong 332.)

- [ ] **Kiểm tra bằng tay rằng đường ticket đã thông**

```bash
uv run desk --config config.yaml analyze BTCUSDT
ls artifacts/crypto/$(date -u +%F)/*/
```

Nếu quyết định là ACCUMULATE/REDUCE/EXIT thì hoặc có `ticket_id` trong kết quả, hoặc có `ticket_blocked.json` nói rõ lý do. Trạng thái "không ticket, không lý do" không còn được phép tồn tại.

- [ ] **Kiểm tra `daily` không còn sinh evidence "từ tương lai"**

```bash
uv run desk --config config.yaml daily
grep -l "from the future" artifacts/crypto/$(date -u +%F)/*/evidence.json | wc -l
```

Expected: `0`
