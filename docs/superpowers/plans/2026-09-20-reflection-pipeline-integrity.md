# Reflection Pipeline Integrity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bảng `reflections` chỉ chứa quyết định thật của hội đồng, tách theo benchmark, và không bị quét nhầm giữa các asset class.

**Architecture:** Ba sửa chữa độc lập trên đường reflection đang chạy. `unreflected_runs` nhận bộ lọc symbol. `ResearchDecision` mang cờ `decided` để phân biệt quyết định với lỗi, và `refresh_reflections` bỏ qua run không có nó. `save_reflection` ghi `benchmark_symbol`, `build_scorecard` gộp theo benchmark trước rồi mới theo action.

**Tech Stack:** Python 3.12, `Decimal`, dataclass `frozen=True, slots=True`, SQLite qua `Store`, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-vn-equities-research-design.md` — bất biến 6 và 7, cùng mục "Store — cần một bộ lọc, không dùng lại nguyên vẹn". Plan này hiện thực phần điều kiện tiên quyết của spec đó; đường VN là plan riêng đi sau.

## Global Constraints

- Python `>=3.12,<3.13`. Ruff `line-length = 100`, `target-version = "py312"`.
- Mọi số tiền và tỉ lệ dùng `Decimal`, không `float`.
- Dataclass mới: `@dataclass(frozen=True, slots=True)`.
- Mọi file `.py` mở đầu bằng `from __future__ import annotations`.
- Văn bản hướng tới người dùng và hướng tới model: **tiếng Việt có dấu**.
- Không đụng `risk.py`, `execution.py`, `broker.py`, `dispatcher.py`.
- Không đổi schema SQLite. `reflections.payload` là cột JSON mờ; thêm khoá không cần migration.
- Chạy test: `uv run pytest`. Lint: `uv run ruff check src tests`.
- Baseline tại `d271327`: **281 passed**, ruff sạch.

---

## Bối cảnh: vì sao cần plan này

Soi dữ liệu thật ngày 2026-09-19: **7 trong 9 reflection đang được `desk scorecard` chấm điểm là run hỏng**, không phải quyết định. Chúng chết vì `provider:rate_limit` và `provider:model_unavailable`.

Root cause chính xác, đã truy tới dòng:

`refresh_reflections` có một lớp chặn duy nhất — `if not run["decision"].get("evidence_ids"): continue`. Lớp này chặn được `service._no_trade` vì nó truyền `evidence_ids=()`. Nhưng `committee._no_trade` ([committee.py:769](../../../src/crypto_desk/committee.py)) truyền `evidence_ids=snapshot.evidence_ids` — **không rỗng**. Nên mọi lỗi ở tầng committee (rate limit, model unavailable, structured output bị từ chối) đều lọt qua và được chấm điểm như một quyết định.

Hậu quả đang thấy được trên `desk scorecard`: hàng `NO_TRADE` với N=4 là bốn lần Gemini chặn rate limit, và con số `+4.39%` là giá BTC/ETH đi đâu sau khi API bị chặn.

Có đúng ba chỗ dựng `ResearchDecision` trong toàn bộ `src/`:

| Vị trí | Là gì | `decided` |
|---|---|---|
| `committee.py:585` | quyết định thật, `reason="Quyết định của hội đồng."` | `True` |
| `committee.py:769` | `committee._no_trade` — evidence thiếu/cũ, provider lỗi, structured output bị từ chối | `False` |
| `service.py:875` | `service._no_trade` — evidence builder ném lỗi | `False` |

Hàng cũ trong DB không có khoá `decided`. Chúng suy ra từ `reason`: `"committee decision"` (bản tiếng Anh cũ của site 1) và `"Quyết định của hội đồng."` là thật, mọi giá trị khác là hỏng.

---

## File Structure

**Sửa:**
- `src/crypto_desk/store.py` — `unreflected_runs` nhận `symbols`
- `src/crypto_desk/domain.py` — `ResearchDecision.decided`, hàm `is_decided`
- `src/crypto_desk/committee.py` — `_no_trade` truyền `decided=False`
- `src/crypto_desk/service.py` — `_no_trade` truyền `decided=False`; `refresh_reflections` lọc theo `decided` và truyền `symbols`; `save_reflection` ghi `benchmark_symbol`
- `src/crypto_desk/scorecard.py` — gộp theo benchmark trước, rồi theo action
- `tests/test_domain_store.py`, `tests/test_service_cli.py`, `tests/test_scorecard.py`

**Không tạo file mới.** Ba thay đổi đều nằm trong module đã có trách nhiệm tương ứng.

---

### Task 1: `unreflected_runs` lọc theo symbol

**Files:**
- Modify: `src/crypto_desk/store.py` (hàm `unreflected_runs`, quanh dòng 239)
- Modify: `src/crypto_desk/service.py` (`refresh_reflections`, chỗ gọi `self.store.unreflected_runs`)
- Test: `tests/test_domain_store.py`

**Interfaces:**
- Produces: `Store.unreflected_runs(completed_before: str, symbols: tuple[str, ...] | None = None) -> list[dict[str, Any]]` — `None` giữ hành vi cũ (không lọc), tuple lọc `WHERE r.symbol IN (...)`.

- [ ] **Step 1: Viết test thất bại**

Thêm vào cuối `tests/test_domain_store.py`:

```python
def test_unreflected_runs_filters_by_symbol(tmp_path):
    from crypto_desk.store import Store

    store = Store(tmp_path / "t.sqlite3")
    try:
        for run_id, symbol in (("r-btc", "BTCUSDT"), ("r-fpt", "FPT")):
            store.db.execute(
                "INSERT INTO research_runs(id,symbol,cutoff,decision,report_dir)"
                " VALUES (?,?,?,?,?)",
                (run_id, symbol, "2026-01-01T00:00:00+00:00", "{}", "/tmp"),
            )
        store.db.commit()

        both = store.unreflected_runs("2026-02-01T00:00:00+00:00")
        crypto = store.unreflected_runs("2026-02-01T00:00:00+00:00", ("BTCUSDT",))
        vn = store.unreflected_runs("2026-02-01T00:00:00+00:00", ("FPT",))
    finally:
        store.close()

    assert {r["symbol"] for r in both} == {"BTCUSDT", "FPT"}
    assert [r["symbol"] for r in crypto] == ["BTCUSDT"]
    assert [r["symbol"] for r in vn] == ["FPT"]


def test_unreflected_runs_with_empty_symbol_tuple_returns_nothing(tmp_path):
    from crypto_desk.store import Store

    store = Store(tmp_path / "t.sqlite3")
    try:
        store.db.execute(
            "INSERT INTO research_runs(id,symbol,cutoff,decision,report_dir)"
            " VALUES (?,?,?,?,?)",
            ("r-btc", "BTCUSDT", "2026-01-01T00:00:00+00:00", "{}", "/tmp"),
        )
        store.db.commit()
        rows = store.unreflected_runs("2026-02-01T00:00:00+00:00", ())
    finally:
        store.close()

    assert rows == []
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_domain_store.py -k unreflected_runs -v`
Expected: FAIL — `TypeError: unreflected_runs() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Sửa `unreflected_runs`**

Trong `src/crypto_desk/store.py`, thay toàn bộ phần truy vấn của `unreflected_runs`:

```python
    def unreflected_runs(
        self,
        completed_before: str,
        symbols: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Run chưa có reflection, tuỳ chọn giới hạn trong một tập symbol.

        Bảng ``research_runs`` dùng chung cho mọi asset class, nên job chấm điểm
        của một đường phải nói rõ nó nhận symbol nào; nếu không nó sẽ vớ phải
        run của đường khác, ném lỗi trên một khoá evidence không tồn tại, và vì
        lỗi bị nuốt nên run đó kẹt lại "chưa reflect" vĩnh viễn, chiếm suất
        trong ``LIMIT 100`` ở mọi lần chạy sau.
        """
        if symbols is not None and not symbols:
            return []
        params: list[Any] = [completed_before]
        clause = ""
        if symbols is not None:
            clause = f" AND r.symbol IN ({','.join('?' * len(symbols))})"
            params.extend(symbols)
        rows = self.db.execute(
            f"""
            SELECT r.* FROM research_runs AS r
            LEFT JOIN reflections AS f ON f.run_id = r.id
            WHERE f.run_id IS NULL AND r.cutoff <= ?{clause}
            ORDER BY r.cutoff
            LIMIT 100
            """,
            params,
        ).fetchall()
        return [
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "cutoff": row["cutoff"],
                "decision": json.loads(row["decision"]),
                "report_dir": row["report_dir"],
            }
            for row in rows
        ]
```

- [ ] **Step 4: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_domain_store.py -k unreflected_runs -v`
Expected: 2 passed

- [ ] **Step 5: Truyền allowlist từ `refresh_reflections`**

Trong `src/crypto_desk/service.py`, trong `refresh_reflections`, đổi:

```python
        for run in self.store.unreflected_runs(completed_before):
```

thành:

```python
        for run in self.store.unreflected_runs(completed_before, tuple(self.settings.symbols)):
```

- [ ] **Step 6: Chạy toàn bộ test**

Run: `uv run pytest && uv run ruff check src tests`
Expected: toàn bộ pass, ruff sạch

- [ ] **Step 7: Commit**

```bash
git add src/crypto_desk/store.py src/crypto_desk/service.py tests/test_domain_store.py
git commit -m "fix: scope the reflection sweep to one asset class

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Chỉ quyết định thật mới sinh reflection

**Files:**
- Modify: `src/crypto_desk/domain.py` (`ResearchDecision`, thêm `is_decided` sau `to_jsonable`)
- Modify: `src/crypto_desk/committee.py` (`_no_trade`, quanh dòng 769)
- Modify: `src/crypto_desk/service.py` (`_no_trade` quanh dòng 875; `refresh_reflections`)
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Consumes: `Store.unreflected_runs(completed_before, symbols)` từ Task 1.
- Produces:
  - `ResearchDecision.decided: bool = True`
  - `crypto_desk.domain.is_decided(decision: dict[str, Any]) -> bool` — đọc khoá `decided` khi có; hàng cũ suy ra từ `reason`.
  - `crypto_desk.domain.DECIDED_REASONS: frozenset[str]` — hai chuỗi lý do của quyết định thật.

- [ ] **Step 1: Viết test thất bại**

Thêm vào cuối `tests/test_service_cli.py`:

```python
def test_is_decided_reads_the_flag_when_present():
    from crypto_desk.domain import is_decided

    assert is_decided({"decided": True, "reason": "bất kỳ"}) is True
    assert is_decided({"decided": False, "reason": "Quyết định của hội đồng."}) is False


def test_is_decided_falls_back_to_reason_for_legacy_rows():
    from crypto_desk.domain import is_decided

    assert is_decided({"reason": "Quyết định của hội đồng."}) is True
    assert is_decided({"reason": "committee decision"}) is True
    assert is_decided({"reason": "provider:rate_limit"}) is False
    assert is_decided({"reason": "provider:model_unavailable"}) is False
    assert is_decided({"reason": "evidence snapshot is stale or incomplete"}) is False
    assert is_decided({}) is False


def test_committee_no_trade_is_not_a_decision():
    from crypto_desk.committee import CryptoCommittee
    from crypto_desk.domain import is_decided, to_jsonable

    decision = CryptoCommittee._no_trade(make_snapshot("BTCUSDT"), "provider:rate_limit")

    assert decision.decided is False
    assert is_decided(to_jsonable(decision)) is False


def test_service_no_trade_is_not_a_decision():
    from crypto_desk.domain import is_decided, to_jsonable
    from crypto_desk.service import CryptoDeskService

    decision = CryptoDeskService._no_trade("BTCUSDT", "evidence_provider:ReadTimeout")

    assert decision.decided is False
    assert is_decided(to_jsonable(decision)) is False
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_service_cli.py -k "is_decided or no_trade_is_not" -v`
Expected: FAIL — `ImportError: cannot import name 'is_decided' from 'crypto_desk.domain'`

- [ ] **Step 3: Thêm trường và hàm vào `domain.py`**

Trong `src/crypto_desk/domain.py`, thêm vào cuối `ResearchDecision` (sau `prior_run_id`):

```python
    decided: bool = True
```

Và thêm sau hàm `to_jsonable`:

```python
# Lý do của một quyết định thật, đủ cả hai bản: chuỗi tiếng Anh cũ và bản tiếng
# Việt hiện hành. Dùng để suy ra ``decided`` cho hàng ghi trước khi có cờ đó.
DECIDED_REASONS = frozenset({"committee decision", "Quyết định của hội đồng."})


def is_decided(decision: dict[str, Any]) -> bool:
    """Hội đồng có thật sự ra quyết định này không, hay đây là một lần chạy hỏng.

    ``_no_trade`` sinh ra một ``ResearchDecision`` trông y hệt quyết định thật,
    nên nếu không phân biệt được hai thứ thì bảng chấm điểm sẽ đo giá đi đâu sau
    một lần rate limit và gọi đó là kết quả của một quyết định.
    """
    flag = decision.get("decided")
    if flag is not None:
        return bool(flag)
    return str(decision.get("reason", "")) in DECIDED_REASONS
```

- [ ] **Step 4: Đánh dấu hai đường hỏng**

Trong `src/crypto_desk/committee.py`, trong `_no_trade`, thêm sau `reason=reason,`:

```python
            decided=False,
```

Trong `src/crypto_desk/service.py`, trong `_no_trade`, thêm sau `reason=reason,`:

```python
            decided=False,
```

- [ ] **Step 5: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_service_cli.py -k "is_decided or no_trade_is_not" -v`
Expected: 4 passed

- [ ] **Step 6: Viết test thất bại cho bộ lọc trong `refresh_reflections`**

Thêm vào cuối `tests/test_service_cli.py`:

```python
def test_refresh_reflections_skips_runs_the_committee_never_decided(tmp_path: Path):
    """Lỗi tầng committee mang evidence_ids không rỗng nên lớp chặn cũ không bắt được."""
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    report_dir = tmp_path / "failed-run"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(json.dumps({"binance_mid": "100"}), encoding="utf-8")
    failed = ResearchDecision(
        symbol="BTCUSDT",
        action="NO_TRADE",
        conviction=Decimal("0"),
        bull_case="Bull",
        bear_case="Bear",
        catalysts=(),
        invalidation="Invalidation",
        entry=None,
        stop=None,
        target=None,
        evidence_ids=("evidence-1",),
        reason="provider:rate_limit",
        decided=False,
    )
    store.save_run(
        "failed-run",
        (NOW - timedelta(days=21)).isoformat(),
        failed,
        report_dir,
    )

    class _Builder:
        def reflection_closes(self, symbol, start, periods=20):
            return tuple(Decimal("100") for _ in range(20))

    service = CryptoDeskService(settings, store, evidence_builder=_Builder())
    try:
        saved = service.refresh_reflections(NOW)
        rows = store.list_reflections()
    finally:
        store.close()

    assert saved == []
    assert rows == []
```

> **Lưu ý cho người thực hiện:** `make_settings`, `Store`, `ResearchDecision`, `NOW`, `timedelta`, `Decimal`, `json`, `Path` và `CryptoDeskService` đều đã import sẵn ở đầu `tests/test_service_cli.py` — file này đã có một test dựng `research_runs` theo đúng khuôn này (`test_daily_schedules_due_reflections_once`, quanh dòng 696). Đọc nó để lấy đúng cách khởi tạo `CryptoDeskService` mà file đang dùng, và dùng lại; đừng tạo helper mới.

- [ ] **Step 7: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_service_cli.py -k skips_runs_the_committee -v`
Expected: FAIL — `assert saved == []` thất bại vì run hỏng vẫn được chấm

- [ ] **Step 8: Lọc trong `refresh_reflections`**

Trong `src/crypto_desk/service.py`, thêm `is_decided` vào khối import từ `.domain`, rồi đổi:

```python
        for run in self.store.unreflected_runs(completed_before, tuple(self.settings.symbols)):
            if not run["decision"].get("evidence_ids"):
                continue
```

thành:

```python
        for run in self.store.unreflected_runs(completed_before, tuple(self.settings.symbols)):
            # Lớp chặn evidence_ids không đủ: committee._no_trade truyền
            # evidence_ids của snapshot nên một lần rate limit vẫn lọt qua và
            # được chấm điểm như một quyết định.
            if not is_decided(run["decision"]):
                continue
            if not run["decision"].get("evidence_ids"):
                continue
```

- [ ] **Step 9: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_service_cli.py -k skips_runs_the_committee -v`
Expected: PASS

- [ ] **Step 10: Chạy toàn bộ test và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: toàn bộ pass, ruff sạch.
Nếu một test sẵn có vỡ vì nó dựng `ResearchDecision` rồi mong reflection được ghi, kiểm xem test đó có đặt `reason` là lý do quyết định thật không — nếu không thì cập nhật `reason` của test cho đúng ý định, đừng nới lỏng bộ lọc.

- [ ] **Step 11: Commit**

```bash
git add src/crypto_desk/domain.py src/crypto_desk/committee.py \
        src/crypto_desk/service.py tests/test_service_cli.py
git commit -m "fix: score only the runs the committee actually decided

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Scorecard tách nhóm theo benchmark

**Files:**
- Modify: `src/crypto_desk/service.py` (`save_reflection`, `refresh_reflections`)
- Modify: `src/crypto_desk/scorecard.py` (`Scorecard`, `build_scorecard`, `render`)
- Test: `tests/test_scorecard.py`

**Interfaces:**
- Consumes: `is_decided` và `Store.unreflected_runs(completed_before, symbols)` từ Task 1 và 2.
- Produces:
  - payload reflection thêm khoá `benchmark_symbol: str`
  - `Scorecard.groups: tuple[BenchmarkGroup, ...]` thay cho `Scorecard.scores`
  - `BenchmarkGroup(benchmark: str, inferred: bool, scores: tuple[ActionScore, ...])`

- [ ] **Step 1: Viết test thất bại**

Thêm vào cuối `tests/test_scorecard.py`:

```python
def _row(symbol: str, action: str, alpha: str, benchmark: str | None = None) -> dict:
    payload = {
        "realized_return": "0.05",
        "maximum_adverse_excursion": "-0.02",
        "maximum_favorable_excursion": "0.08",
        "benchmark_return": "0.01",
        "alpha": alpha,
        "decision_action": action,
    }
    if benchmark is not None:
        payload["benchmark_symbol"] = benchmark
    return {
        "run_id": f"{symbol}-{action}-{alpha}",
        "symbol": symbol,
        "created_at": "2026-09-01T00:00:00+00:00",
        "payload": payload,
    }


def test_alpha_against_different_benchmarks_never_shares_a_mean():
    card = build_scorecard(
        [
            _row("ETHUSDT", "ACCUMULATE", "0.04", "BTCUSDT"),
            _row("FPT", "ACCUMULATE", "-0.10", "VN30"),
        ]
    )
    by = {g.benchmark: g for g in card.groups}

    assert set(by) == {"BTCUSDT", "VN30"}
    assert by["BTCUSDT"].scores[0].mean_alpha == Decimal("0.04")
    assert by["VN30"].scores[0].mean_alpha == Decimal("-0.10")


def test_legacy_row_without_benchmark_is_inferred_from_the_symbol_and_marked():
    card = build_scorecard([_row("ETHUSDT", "HOLD", "0.01")])
    group = card.groups[0]

    assert group.benchmark == "BTCUSDT"
    assert group.inferred is True
    assert "suy ra" in card.render()


def test_render_shows_one_table_per_benchmark():
    rendered = build_scorecard(
        [
            _row("ETHUSDT", "ACCUMULATE", "0.04", "BTCUSDT"),
            _row("FPT", "ACCUMULATE", "-0.10", "VN30"),
        ]
    ).render()

    assert "BTCUSDT" in rendered
    assert "VN30" in rendered
    assert rendered.count("Action") == 2
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_scorecard.py -k "benchmark" -v`
Expected: FAIL — `AttributeError: 'Scorecard' object has no attribute 'groups'`

- [ ] **Step 3: Ghi `benchmark_symbol` vào payload**

Trong `src/crypto_desk/service.py`, đổi chữ ký `save_reflection` thêm một tham số sau `decision_cutoff`:

```python
        benchmark_symbol: str | None = None,
```

và thêm vào phần dựng payload, ngay sau khối `decision_cutoff`:

```python
        if benchmark_symbol is not None:
            payload["benchmark_symbol"] = benchmark_symbol
```

Trong `refresh_reflections`, thêm vào lời gọi `self.save_reflection(...)`:

```python
                    benchmark_symbol=BENCHMARK_SYMBOL,
```

- [ ] **Step 4: Gộp theo benchmark trong `scorecard.py`**

Trong `src/crypto_desk/scorecard.py`, thêm import và dataclass mới, rồi viết lại `build_scorecard` và `Scorecard.render`:

```python
@dataclass(frozen=True, slots=True)
class BenchmarkGroup:
    benchmark: str
    inferred: bool
    scores: tuple[ActionScore, ...]
```

Thêm hàm suy ra benchmark cho hàng cũ:

```python
def _benchmark_of(item: dict[str, Any]) -> tuple[str, bool]:
    """Benchmark của hàng, và có phải suy ra hay không.

    Hàng ghi trước khi ``benchmark_symbol`` tồn tại chỉ có thể suy ra từ symbol.
    Gộp alpha đo với hai benchmark khác nhau vào một trung bình là vô nghĩa, nên
    khi không chắc thì phải nói ra chứ không đoán thầm.
    """
    stored = item["payload"].get("benchmark_symbol")
    if stored:
        return str(stored), False
    return (BENCHMARK_SYMBOL, True) if item["symbol"].endswith("USDT") else ("?", True)
```

`Scorecard` đổi trường `scores: tuple[ActionScore, ...]` thành `groups: tuple[BenchmarkGroup, ...]`. Mọi trường đếm khác (`horizon_days`, `benchmark`, `scored`, `skipped_no_action`, `skipped_unknown_action`, `skipped_benchmark`) giữ nguyên tên và ý nghĩa.

Trong `build_scorecard`, phần duyệt hàng giữ nguyên mọi `continue` và bộ đếm đang có; chỉ đổi khoá của `buckets` và ghi thêm cờ suy luận. Thay chỗ `buckets.setdefault(...)` hiện tại bằng:

```python
        bench, inferred = _benchmark_of(item)
        inferred_flags[bench] = inferred_flags.get(bench, False) or inferred
        buckets.setdefault((bench, str(action)), []).append(...)
```

với hai biến khởi tạo cạnh `buckets` ở đầu hàm:

```python
    buckets: dict[tuple[str, str], list[tuple[Decimal, Decimal, tuple[str, str]]]] = {}
    inferred_flags: dict[str, bool] = {}
```

Phần tử append giữ nguyên bộ ba hiện có: `(alpha, maximum_adverse_excursion, (symbol, day))` — phần tử thứ ba là cặp dùng cho cột `Ngày`, đừng làm phẳng nó.

Rồi dựng kết quả:

```python
    groups = tuple(
        BenchmarkGroup(
            benchmark=bench,
            inferred=inferred_flags[bench],
            scores=tuple(
                _score(action, buckets[(bench, action)])
                for action in ACTIONS
                if (bench, action) in buckets
            ),
        )
        for bench in sorted(inferred_flags)
    )
```

`scored` đổi thành `sum(s.samples for g in groups for s in g.scores)`.

Còn một chỗ dễ bỏ sót: bộ đếm `skipped_benchmark` hiện so `item["symbol"] == BENCHMARK_SYMBOL`, tức chỉ biết một benchmark duy nhất. Khi có hai, điều kiện đúng là "symbol trùng với benchmark của chính hàng đó". Đổi thành so với `bench` vừa suy ra, và vì `_benchmark_of` phải chạy trước, chuyển khối `skipped_benchmark` xuống sau nó. Hôm nay hành vi không đổi (VN30 là chỉ số, không bao giờ là một mã trong `vn_symbols`), nhưng để nguyên là gài sẵn một lỗi câm cho đường VN.

`render()` in phần đầu như cũ, rồi một bảng cho mỗi group:

```python
        for group in self.groups:
            lines.append("")
            lines.append(f"Benchmark: {group.benchmark}")
            if group.inferred:
                lines.append(
                    "  Benchmark của nhóm này được suy ra từ symbol, không đọc từ dữ liệu."
                )
            lines.append(
                f"{'Action':<12}{'N':>4}{'Ngày':>6}{'Alpha TB':>11}{'Alpha TV':>11}"
                f"{'Đúng hướng':>12}{'Tệ nhất':>11}"
            )
            for score in group.scores:
                lines.append(...)   # giữ nguyên dòng dựng score hiện có
```

Ba dòng chú giải cuối (alpha long-only, `Ngày` là số ngày quyết định riêng biệt, `Tệ nhất` là `min(returns)`) giữ nguyên nguyên văn, in một lần sau tất cả các bảng.

- [ ] **Step 5: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_scorecard.py -v`
Expected: toàn bộ pass. Các test scorecard cũ dùng `card.scores` sẽ phải đổi sang `card.groups[0].scores` — cập nhật chúng, đây là đổi interface có chủ ý.

- [ ] **Step 6: Chạy toàn bộ test và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: toàn bộ pass, ruff sạch

- [ ] **Step 7: Kiểm trên dữ liệu thật**

Run: `uv run desk scorecard`

Expected: hàng `NO_TRADE` N=4 **biến mất** — bốn hàng đó là `provider:rate_limit`, Task 2 đã loại chúng khỏi bảng. Chỉ còn các hàng `HOLD` từ hai run `committee decision` thật. Ghi bảng trước và sau vào phần báo cáo.

> Reflection đã ghi vào DB từ trước vẫn nằm đó — Task 2 chặn hàng mới, không xoá hàng cũ. Nếu bảng vẫn còn hàng rate limit, đó là 7 hàng cũ; dọn chúng là việc riêng, đừng tự ý xoá dữ liệu trong plan này.

- [ ] **Step 8: Commit**

```bash
git add src/crypto_desk/service.py src/crypto_desk/scorecard.py tests/test_scorecard.py
git commit -m "fix: never average alpha across two different benchmarks

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Sau plan này

Bảy hàng reflection bẩn đã nằm trong `data/crypto_desk.sqlite3`. Plan này chặn hàng mới chứ không dọn hàng cũ — xoá dữ liệu là quyết định của người dùng, không phải của plan. Sau khi plan chạy xong, đưa họ con số cụ thể (hàng nào, vì sao) rồi hỏi.

Đường VN là plan riêng, viết sau khi plan này chạy xong, vì nó tiêu thụ `is_decided`, `unreflected_runs(symbols=...)` và `benchmark_symbol` do plan này tạo ra.
