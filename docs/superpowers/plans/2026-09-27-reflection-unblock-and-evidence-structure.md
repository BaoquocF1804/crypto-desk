# Gỡ kẹt reflection, sửa thang giá VN và làm gọn evidence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scorecard có dữ liệu thật để đo độ chính xác, gồm cả crypto lẫn VN. Bull/bear/manager chỉ thấy evidence đúng horizon. Stop/target được neo vào mức cấu trúc do code tính.

**Architecture:** Năm thay đổi code độc lập cộng một bước vận hành.
- `Store.unreflected_runs` bỏ `LIMIT 100`, vì run hỏng đang chặn vĩnh viễn cửa sổ đó.
- Reflection VN đổi sang `stock_info`: lấy giá điều chỉnh làm gốc, quy entry/stop/target thô về cùng thang, rồi chấm theo mức nào chạm trước.
- Thêm lệnh `desk reflect` để chấm ngay, không phải chạy phân tích.
- Committee lọc các trường khung giờ và các trường trùng lặp khỏi payload của bull/bear/manager.
- Evidence crypto có thêm `price_structure` (swing high/low, biên 20/55 ngày), đặt cạnh `technical_indicators` để dashboard không bị ảnh hưởng.

**Tech Stack:** Python 3.12, `Decimal`, SQLite qua `Store`, Typer, pytest, ruff.

**Spec:** Không có spec riêng. Bối cảnh và số liệu nằm ở mục "Bối cảnh" bên dưới, rút từ đánh giá ngày 2026-09-27 và từ lần thực hiện trước (scorecard mới, cổng ATR ngày, vòng xác nhận ACCUMULATE).

## Global Constraints

- Python `>=3.12,<3.13`. Ruff `line-length = 100`. Chạy test: `uv run pytest`. Lint: `uv run ruff check src tests`.
- Mọi giá và tỉ lệ dùng `Decimal`, không `float`.
- Không thêm dependency.
- Prompt gửi model viết bằng tiếng Anh. Văn bản cho người dùng (CLI, báo cáo, docstring hiện có) viết tiếng Việt có dấu, theo đúng file đang sửa.
- Không đổi schema SQLite. `reflections.payload` là cột JSON; thêm khoá không cần migration.
- Không đổi contract dashboard: `TechnicalIndicators` trong `dashboard.py` là `extra="forbid"` với `version: Literal["technical-v1"]`, và web đọc các trường `oi_change_1h_pct`, `long_short_ratio`, `top_trader_ratio`, `taker_buy_sell_ratio`. Chỉ được **thêm** key song song, không đổi tên hay xoá các trường đó khỏi evidence.
- Không đụng `risk.py`, `execution.py`, `broker.py`.
- Commit message kết thúc bằng `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Baseline: working tree hiện tại, **395 passed**, ruff sạch.

## Điều kiện tiên quyết

- [ ] Working tree đang chứa thay đổi chưa commit của phiên trước và của lần thực hiện vừa rồi (scorecard, ATR ngày, `manager_confirm`, dữ liệu phái sinh 24h). **Hỏi người dùng** trước khi commit baseline này lên `codex/crypto-desk-rewrite`. Nếu được đồng ý:

```bash
uv run pytest -q && uv run ruff check src tests
git add -A src tests README.md docs
git commit -m "feat: grade setups by barrier, confirm ACCUMULATE, 24h positioning evidence

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Bối cảnh: vì sao cần plan này

Bốn điểm còn treo sau lần sửa trước. Khi soi dữ liệu thật thì ba trong số đó hoá ra là lỗi, không phải chuyện chờ đủ ngày.

1. **Pipeline reflection đang kẹt.**
   - `unreflected_runs` trả 100 run cũ nhất (`ORDER BY cutoff LIMIT 100`).
   - DB có 232 run tháng 7 hỏng (undecided). `refresh_reflections` bỏ qua chúng nhưng không đánh dấu, nên chúng chiếm trọn 100 suất mỗi lần chạy.
   - Hậu quả: 11 run tháng 7 chấm được (7 có đủ entry/stop/target, 5 là ACCUMULATE) chưa bao giờ được chấm. Các run tháng 9 cũng sẽ không bao giờ được chấm.
   - Thêm vào đó, cron `daily` (job duy nhất gọi `refresh_reflections`) không chạy từ bucket `2026-09-04`.
2. **7 trong 9 reflection hiện có là run hỏng.** Các run ETH/BTC ngày 17/07 có reason `provider:rate_limit` hoặc `provider:model_unavailable`. Chúng được ghi trước khi có cờ `decided` và vẫn đang được scorecard chấm như quyết định. Hai hàng còn lại là quyết định thật nhưng thiếu `decision_cutoff` và kết quả chạm mức.
3. **Reflection VN sẽ ghi lợi nhuận ≈ −99.9%.**
   - `VNEvidenceBuilder.reflection_closes` đọc `charts_history`, giá tính theo **nghìn đồng** và đã điều chỉnh (FPT `c: 64.7`).
   - Entry trong `vn_service.refresh_reflections` là `mid` theo **VND thô** (`20000` cho MBB).
   - Ngay cả khi cùng thang, giá điều chỉnh vẫn lệch giá thô khi có sự kiện quyền. FPT phiên 18/09: `close` 65182.47 so với `closeRaw` 71700.
   - Chưa có hàng VN nào được ghi: run VN sớm nhất là 19/09, cần 20 phiên.
   - `stock_info` trả cả `close/high/low` (điều chỉnh, VND) lẫn `closeRaw` trong cùng một response, mới nhất trước, `pageSize` tối đa 40. Đã kiểm chứng trên API thật.
4. **Bull/bear/manager vẫn thấy số khung giờ và sổ lệnh thô.** `_base_payload` gửi `to_jsonable(snapshot)`, gồm các trường top-level trùng item (`daily_closes`, `four_hour_closes`, 1h ratios…), `spot.depth` 20 mức và `derivatives.oi_change_1h_pct/taker_buy_sell_ratio`. Chuỗi close bị gửi hai lần.
5. **Stop/target không có mức cấu trúc để neo.** Technical specialist chỉ có chuỗi close (không có râu nến). Stop của các lệnh ACCUMULATE gần đây nằm sát đúng ngưỡng tối thiểu chỉ để qua cổng R:R.

## Review Focus

- **VN có sự kiện quyền giữa phiên quyết định và cuối cửa sổ:** cú giảm giá thô vì chia cổ tức không được tính là lỗ, và stop thô không được coi là "chạm". Task 2, test `test_vn_reflection_measures_return_and_setup_on_the_adjusted_scale`.
- **Cửa sổ VN bị Tết/lễ cắt ngắn:** thiếu phiên thì ném `EvidenceError` để lần sau thử lại, và khoảng ngày truy vấn đủ rộng để 20 phiên luôn lọt vào. Task 2, `test_vn_reflection_window_*`.
- **Evidence VN kiểu cũ thiếu `trading_date`:** bỏ qua, không làm hỏng cả lượt quét. Task 2, `test_vn_reflection_skips_evidence_without_a_decision_session`.
- **Lọc payload không được xoá thứ prompt bắt manager dùng:** `technical_indicators.atr14_1d`, `depth_summary`, `price_structure`, các trường 24h/percentile phải còn. Task 4, `test_debate_payloads_hide_hour_scale_fields_but_keep_what_prompts_require`.
- **Coin mới niêm yết (<55 nến ngày):** `price_structure` trả `null` cho biên 55 ngày, không crash. Task 5, `test_price_structure_keeps_the_nearest_swings_on_each_side_of_mid`.

---

### Task 1: Run hỏng không còn chặn hàng đợi reflection

**Files:**
- Modify: `src/crypto_desk/store.py:239-278` (`unreflected_runs`)
- Test: `tests/test_domain_store.py`

**Interfaces:**
- Produces: `Store.unreflected_runs(completed_before: str, symbols: tuple[str, ...] | None = None) -> list[dict[str, Any]]`. Chữ ký không đổi, giờ trả **mọi** run chưa reflect, theo thứ tự `cutoff` tăng dần.

- [ ] **Step 1: Viết test fail** — thêm vào cuối `tests/test_domain_store.py`:

```python
def test_unreflected_runs_are_not_starved_by_old_runs_that_can_never_be_graded(tmp_path: Path):
    # Run hỏng bị bỏ qua nhưng không bao giờ được đánh dấu; với LIMIT 100 thì 100 run hỏng
    # cũ nhất chiếm trọn cửa sổ và run chấm được đứng sau chúng không bao giờ tới lượt.
    store = Store(tmp_path / "crypto.db")
    failed = make_decision()
    for index in range(101):
        store.save_run(
            f"failed-{index:03d}",
            f"2026-06-01T{index // 60:02d}:{index % 60:02d}:00+00:00",
            failed,
            Path("artifacts/failed"),
        )
    store.save_run(
        "gradeable",
        "2026-06-02T00:15:00+00:00",
        make_decision(evidence_ids=("evidence-1",)),
        Path("artifacts/gradeable"),
    )

    rows = store.unreflected_runs("2026-06-30T00:15:00+00:00")

    assert "gradeable" in [row["id"] for row in rows]
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_domain_store.py::test_unreflected_runs_are_not_starved_by_old_runs_that_can_never_be_graded -v`
Expected: FAIL (`'gradeable' in [...]` sai, vì chỉ trả 100 run `failed-*`).

- [ ] **Step 3: Sửa code** — trong `unreflected_runs`, thay docstring và bỏ `LIMIT 100`:

```python
    def unreflected_runs(
        self,
        completed_before: str,
        symbols: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Run chưa có reflection, tuỳ chọn giới hạn trong một tập symbol.

        Bảng ``research_runs`` dùng chung cho mọi asset class, nên job chấm điểm
        của một đường phải nói rõ nó nhận symbol nào; nếu không nó sẽ vớ phải
        run của đường khác và ném lỗi trên một khoá evidence không tồn tại.

        Không có LIMIT: run hỏng bị bỏ qua nhưng không bao giờ được đánh dấu,
        nên ``LIMIT 100`` từng để 100 run hỏng cũ nhất chiếm trọn cửa sổ và
        không run nào sau chúng được chấm nữa.
        """
        # ponytail: quét cả bảng mỗi lần chạy; thêm cờ "không chấm được" vào
        # research_runs nếu bảng lên tới hàng trăm nghìn dòng.
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
            """,
            params,
        ).fetchall()
```

Giữ nguyên phần `return [...]` phía sau.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_domain_store.py -v && uv run pytest -q`
Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/store.py tests/test_domain_store.py
git commit -m "fix: stop failed runs from starving the reflection queue

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Reflection VN đo trên cùng một thang giá và chấm theo mức giá

**Files:**
- Modify: `src/crypto_desk/vn_data.py:153-167` (`SSIClient.stock_info`), và thêm `VNEvidenceBuilder.reflection_window` ngay sau `reflection_closes` (dòng 386-407)
- Modify: `src/crypto_desk/vn_service.py:174-207` (`save_reflection`), `:209-262` (`refresh_reflections`)
- Test: `tests/test_vn_data.py`, `tests/test_vn_service.py`

**Interfaces:**
- Consumes: `calculate_reflection(*, entry, closes, benchmark_closes, highs=(), lows=(), levels=None)` trong `service.py` (đã có).
- Produces:
  - `SSIClient.stock_info(symbol: str, from_date: str, to_date: str, *, page_size: int | None = None) -> Fetched`
  - `VNEvidenceBuilder.reflection_window(symbol: str, session_date: str, periods: int = 20) -> tuple[tuple[Decimal, Decimal], tuple[tuple[Decimal, Decimal, Decimal], ...]]`: trả `((close điều chỉnh, close thô) của phiên quyết định, (high, low, close) điều chỉnh của periods phiên sau)`, đơn vị VND.
  - `VNDeskService.save_reflection(..., highs=(), lows=(), levels=None)`: thêm ba keyword, cùng nghĩa với bản crypto.

- [ ] **Step 1: Viết test fail cho builder** — thêm vào cuối `tests/test_vn_data.py`:

```python
def _session(date: str, close: str, close_raw: str, high: str, low: str) -> dict:
    return {"tradingDate": date, "close": close, "closeRaw": close_raw, "high": high, "low": low}


def _window_client(rows: list[dict]):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from crypto_desk.data import Fetched

    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    client = MagicMock()
    client.stock_info.return_value = Fetched("ssi", "stock-info", at, at, rows)
    return client


def test_vn_reflection_window_returns_the_decision_session_and_adjusted_bars_after_it():
    from crypto_desk.vn_data import VNEvidenceBuilder

    decision = _session("01/09/2026", "90", "100", "91", "89")
    after = [
        _session(f"{day:02d}/09/2026", str(100 + day), str(110 + day), str(101 + day), str(99 + day))
        for day in range(2, 22)
    ]
    # SSI trả phiên mới nhất trước.
    client = _window_client(list(reversed(after)) + [decision])

    base, bars = VNEvidenceBuilder(client).reflection_window("FPT", "01/09/2026", periods=20)

    assert base == (Decimal("90"), Decimal("100"))
    assert len(bars) == 20
    # high/low/close điều chỉnh, không phải closeRaw.
    assert bars[0] == (Decimal("103"), Decimal("101"), Decimal("102"))
    # 50 ngày lịch đủ 20 phiên kể cả khi có Tết, và không vượt 40 dòng SSI cho phép.
    client.stock_info.assert_called_once_with("FPT", "01/09/2026", "21/10/2026", page_size=40)


def test_vn_reflection_window_waits_until_enough_sessions_have_closed():
    from crypto_desk.vn_data import VNEvidenceBuilder

    rows = [_session(f"{day:02d}/09/2026", "100", "100", "101", "99") for day in range(15, 0, -1)]

    with pytest.raises(EvidenceError, match="Cần 20 phiên"):
        VNEvidenceBuilder(_window_client(rows)).reflection_window("FPT", "01/09/2026")


def test_vn_reflection_window_requires_the_decision_session_itself():
    from crypto_desk.vn_data import VNEvidenceBuilder

    rows = [_session(f"{day:02d}/09/2026", "100", "100", "101", "99") for day in range(25, 1, -1)]

    with pytest.raises(EvidenceError, match="phiên quyết định"):
        VNEvidenceBuilder(_window_client(rows)).reflection_window("FPT", "01/09/2026")
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_vn_data.py -k reflection_window -v`
Expected: FAIL với `AttributeError: 'VNEvidenceBuilder' object has no attribute 'reflection_window'`.

- [ ] **Step 3: Sửa `stock_info` và thêm `reflection_window`** trong `src/crypto_desk/vn_data.py`

Thay `stock_info`:

```python
    def stock_info(
        self,
        symbol: str,
        from_date: str,
        to_date: str,
        *,
        page_size: int | None = None,
    ) -> Fetched:
        params: dict[str, Any] = {"symbol": symbol, "fromDate": from_date, "toDate": to_date}
        if page_size is not None:
            # SSI trả phiên mới nhất trước, mặc định 20 dòng và chặn ở 40.
            params["pageSize"] = page_size
        source, payload, fetched_at = self._json(
            SSI_IBOARD_API,
            "/statistics/company/ssmi/stock-info",
            params=params,
        )
```

Giữ nguyên phần còn lại của hàm. Thêm vào `VNEvidenceBuilder`, ngay sau `reflection_closes`:

```python
    def reflection_window(
        self,
        symbol: str,
        session_date: str,
        periods: int = 20,
    ) -> tuple[tuple[Decimal, Decimal], tuple[tuple[Decimal, Decimal, Decimal], ...]]:
        """Phiên quyết định và ``periods`` phiên sau đó, từ cùng một lần gọi stock_info.

        Trả về ((close điều chỉnh, close thô) của phiên quyết định, các (high, low,
        close) điều chỉnh sau đó), tất cả theo VND. Cùng một response nên cùng một
        gốc điều chỉnh: lợi nhuận tính trên giá điều chỉnh, còn tỉ số hai giá của
        phiên quyết định quy entry/stop/target thô về cùng thang. ``charts_history``
        không dùng được ở đây vì nó tính theo nghìn đồng.
        """
        start = datetime.strptime(session_date, "%d/%m/%Y")
        # 50 ngày lịch vẫn đủ 20 phiên khi có Tết, và tối đa ~37 phiên, dưới trần 40 dòng.
        to_date = (start + timedelta(days=periods * 2 + 10)).strftime("%d/%m/%Y")
        rows = self.client.stock_info(symbol, session_date, to_date, page_size=40).payload
        ordered = sorted(rows, key=lambda row: datetime.strptime(row["tradingDate"], "%d/%m/%Y"))
        if not ordered or ordered[0]["tradingDate"] != session_date:
            raise EvidenceError(f"Không có phiên quyết định {session_date} cho {symbol}")
        after = ordered[1 : periods + 1]
        if len(after) < periods:
            raise EvidenceError(
                f"Cần {periods} phiên sau {session_date} cho {symbol} nhưng chỉ có {len(after)}"
            )
        base = ordered[0]
        return (
            (Decimal(str(base["close"])), Decimal(str(base["closeRaw"]))),
            tuple(
                tuple(Decimal(str(row[key])) for key in ("high", "low", "close")) for row in after
            ),
        )
```

- [ ] **Step 4: Chạy test builder**

Run: `uv run pytest tests/test_vn_data.py -v`
Expected: PASS.

- [ ] **Step 5: Viết test fail cho service** — trong `tests/test_vn_service.py`, thay toàn bộ `test_vn_reflection_sweep_never_picks_up_crypto_runs` bằng bản dưới và thêm hai test mới ngay sau nó:

```python
def _vn_evidence(session_date: str = "01/08/2026", close_raw: str = "100") -> str:
    return json.dumps(
        {
            "symbol": "FPT",
            "mid": close_raw,
            "binance_mid": close_raw,
            "items": [
                {
                    "kind": "spot",
                    "payload": {"trading_date": session_date, "close_raw": close_raw},
                }
            ],
        }
    )


def test_vn_reflection_sweep_never_picks_up_crypto_runs(tmp_path):
    """Bảng research_runs dùng chung; quét không lọc sẽ vớ phải run crypto."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
        symbols=("BTCUSDT",),
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)
    swept: list[str] = []

    for run_id, symbol in (("r-btc", "BTCUSDT"), ("r-fpt", "FPT")):
        report_dir = tmp_path / run_id
        report_dir.mkdir()
        (report_dir / "evidence.json").write_text(_vn_evidence(), encoding="utf-8")
        store.save_run(
            run_id,
            (NOW - timedelta(days=40)).isoformat(),
            make_vn_decision(symbol, "HOLD"),
            report_dir,
        )

    class _Builder:
        def reflection_window(self, symbol, session_date, periods=20):
            swept.append(symbol)
            bar = (Decimal("101"), Decimal("99"), Decimal("100"))
            return (Decimal("100"), Decimal("100")), (bar,) * periods

        def reflection_closes(self, symbol, start, periods=20):
            swept.append(symbol)
            return tuple(Decimal("100") for _ in range(periods))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    try:
        saved = service.refresh_reflections(NOW)
    finally:
        store.close()

    assert saved == ["r-fpt"]
    assert "BTCUSDT" not in swept


def test_vn_reflection_measures_return_and_setup_on_the_adjusted_scale(tmp_path):
    # FPT 18/09/2026: close điều chỉnh 64530 = 0,9 × closeRaw 71700 (chia cổ tức sau phiên).
    # So thẳng mức thô với giá điều chỉnh sẽ ghi lỗ ảo −10% và báo chạm stop 68000.
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)
    report_dir = tmp_path / "r-fpt"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(
        _vn_evidence("18/09/2026", "71700"), encoding="utf-8"
    )
    decision = dataclasses.replace(
        make_vn_decision("FPT", "ACCUMULATE"),
        entry=Decimal("71700"),
        stop=Decimal("68000"),
        target=Decimal("80000"),
    )
    store.save_run("r-fpt", (NOW - timedelta(days=40)).isoformat(), decision, report_dir)
    asked: list[str] = []

    class _Builder:
        def reflection_window(self, symbol, session_date, periods=20):
            asked.append(session_date)
            bar = (Decimal("64600"), Decimal("63000"), Decimal("64530"))
            return (Decimal("64530"), Decimal("71700")), (bar,) * periods

        def reflection_closes(self, symbol, start, periods=20):
            return tuple(Decimal("1900") for _ in range(periods))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    try:
        assert service.refresh_reflections(NOW) == ["r-fpt"]
        payload = store.list_reflections("FPT")[0]["payload"]
    finally:
        store.close()

    assert asked == ["18/09/2026"]
    assert Decimal(payload["realized_return"]) == 0
    # Stop quy đổi = 68000 × 0,9 = 61200, dưới đáy 63000 của cửa sổ.
    assert payload["barrier_outcome"] == "open"
    assert Decimal(payload["r_multiple"]) == 0


def test_vn_reflection_skips_evidence_without_a_decision_session(tmp_path):
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)
    report_dir = tmp_path / "r-legacy"
    report_dir.mkdir()
    (report_dir / "evidence.json").write_text(json.dumps({"mid": "100"}), encoding="utf-8")
    store.save_run(
        "r-legacy", (NOW - timedelta(days=40)).isoformat(), make_vn_decision(), report_dir
    )

    service = VNDeskService(settings, store, evidence_builder=object())
    try:
        assert service.refresh_reflections(NOW) == []
    finally:
        store.close()
```

- [ ] **Step 6: Chạy để thấy fail**

Run: `uv run pytest tests/test_vn_service.py -k "reflection" -v`
Expected:
- `test_vn_reflection_sweep_never_picks_up_crypto_runs` FAIL: `saved == []`, vì code cũ gọi `reflection_closes` cho symbol và đọc entry từ `closeRaw` top-level.
- `test_vn_reflection_measures_return_and_setup_on_the_adjusted_scale` FAIL.
- `test_vn_reflection_skips_evidence_without_a_decision_session` FAIL với `AttributeError`: code cũ gọi `reflection_closes` trên `object()` và không bắt lỗi đó.

- [ ] **Step 7: Sửa `vn_service.py`**

Trong `save_reflection`, thêm ba keyword và truyền xuống:

```python
    def save_reflection(
        self,
        run_id: str,
        symbol: str,
        entry: Decimal,
        closes: tuple[Decimal, ...],
        benchmark_closes: tuple[Decimal, ...],
        decision_action: str | None = None,
        decision_cutoff: str | None = None,
        benchmark_symbol: str | None = None,
        highs: tuple[Decimal, ...] = (),
        lows: tuple[Decimal, ...] = (),
        levels: tuple[Decimal, Decimal, Decimal] | None = None,
    ) -> dict[str, Any]:
        if len(closes) < REFLECTION_HORIZON_DAYS:
            raise ValueError(
                f"Reflection requires {REFLECTION_HORIZON_DAYS} completed daily periods"
            )
        payload: dict[str, Any] = calculate_reflection(
            entry=entry,
            closes=closes,
            benchmark_closes=benchmark_closes,
            highs=highs,
            lows=lows,
            levels=levels,
        )
```

Giữ nguyên phần còn lại. Trong `refresh_reflections`, thay toàn bộ khối `try:` … `except …: continue` bằng:

```python
            try:
                evidence = json.loads(
                    (Path(run["report_dir"]) / "evidence.json").read_text(encoding="utf-8")
                )
                spot = next(item["payload"] for item in evidence["items"] if item["kind"] == "spot")
                session_date = str(spot["trading_date"])
                (base_close, base_raw), bars = builder.reflection_window(
                    run["symbol"], session_date, periods=REFLECTION_HORIZON_DAYS
                )
                highs, lows, closes = zip(*bars)
                session_start = datetime.strptime(session_date, "%d/%m/%Y").replace(tzinfo=UTC)
                benchmark = (
                    ()
                    if run["symbol"] == VN_BENCHMARK_SYMBOL
                    else builder.reflection_closes(
                        VN_BENCHMARK_SYMBOL, session_start, periods=REFLECTION_HORIZON_DAYS + 1
                    )
                )
                # Mức giá của quyết định là VND thô, cửa sổ là giá điều chỉnh; tỉ số của
                # chính phiên quyết định đưa chúng về một thang.
                factor = base_close / base_raw
                decision = run["decision"]
                raw_levels = tuple(decision.get(key) for key in ("entry", "stop", "target"))
                self.save_reflection(
                    run_id=run["id"],
                    symbol=run["symbol"],
                    entry=base_close,
                    closes=closes,
                    benchmark_closes=benchmark,
                    highs=highs,
                    lows=lows,
                    levels=(
                        None
                        if None in raw_levels
                        else tuple(Decimal(str(level)) * factor for level in raw_levels)
                    ),
                    decision_action=str(decision["action"]),
                    decision_cutoff=str(run["cutoff"]),
                    benchmark_symbol=VN_BENCHMARK_SYMBOL,
                )
            except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration):
                continue
```

`benchmark` bắt đầu từ chính phiên quyết định (`periods + 1`), để lợi nhuận benchmark và lợi nhuận cổ phiếu cùng tính từ close của phiên đó. Evidence kiểu cũ không có `items` rơi vào `KeyError` trước khi builder được gọi. Không bắt `AttributeError`, vì nó sẽ che lỗi lập trình thật.

- [ ] **Step 8: Chạy test**

Run: `uv run pytest tests/test_vn_service.py tests/test_vn_data.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: PASS, ruff sạch.

- [ ] **Step 9: Commit**

```bash
git add src/crypto_desk/vn_data.py src/crypto_desk/vn_service.py tests/test_vn_data.py tests/test_vn_service.py
git commit -m "fix: measure VN reflections on one adjusted VND scale and grade their setups

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Lệnh `desk reflect` chấm ngay, không cần chạy phân tích

**Files:**
- Modify: `src/crypto_desk/cli.py` (thêm command ngay trước `def reflections(`, dòng ~307)
- Modify: `scripts/daily.sh`
- Modify: `README.md` (mục "Luồng sử dụng")
- Test: `tests/test_service_cli.py`

**Interfaces:**
- Consumes: `CryptoDeskService.refresh_reflections(cutoff: datetime) -> list[str]`, `VNDeskService.refresh_reflections(cutoff: datetime) -> list[str]` (Task 2), `_service(settings, committee=False)`, `_vn_service(settings, committee=False)`, `_utcnow()`.
- Produces: CLI `desk [--json] reflect` in ra `{"crypto": [run_id...], "vn": [run_id...]}`.

- [ ] **Step 1: Viết test fail** — thêm vào cuối `tests/test_service_cli.py`:

```python
def test_reflect_command_grades_both_markets_without_running_an_analysis(
    tmp_path: Path, monkeypatch
):
    config = _write_config(tmp_path)
    calls: list[tuple[str, bool]] = []

    class _Service:
        def __init__(self, name: str):
            self.name = name

        def refresh_reflections(self, cutoff):
            calls.append((self.name, cutoff.tzinfo is not None))
            return [f"{self.name}-run"]

    monkeypatch.setattr("crypto_desk.cli._service", lambda settings, **kw: _Service("crypto"))
    monkeypatch.setattr("crypto_desk.cli._vn_service", lambda settings, **kw: _Service("vn"))

    result = CliRunner().invoke(app, ["--config", str(config), "--json", "reflect"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"crypto": ["crypto-run"], "vn": ["vn-run"]}
    assert calls == [("crypto", True), ("vn", True)]
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_service_cli.py::test_reflect_command_grades_both_markets_without_running_an_analysis -v`
Expected: FAIL, exit code 2 ("No such command 'reflect'").

- [ ] **Step 3: Thêm command** vào `src/crypto_desk/cli.py`, ngay trước `@app.command()` của `reflections`:

```python
@app.command()
def reflect(ctx: typer.Context) -> None:
    """Chấm ngay các quyết định đã đủ horizon, không chạy phân tích mới."""
    settings = _load(ctx)
    now = _utcnow()
    _emit(
        ctx,
        {
            "crypto": _service(settings, committee=False).refresh_reflections(now),
            "vn": _vn_service(settings, committee=False).refresh_reflections(now),
        },
    )
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_service_cli.py -k reflect -v`
Expected: PASS.

- [ ] **Step 5: Tự động hoá chấm VN.** Chưa có job nào gọi `vn-daily`, nên reflection VN chưa từng được chấm tự động. Thêm dòng cuối vào `scripts/daily.sh`, sau lệnh gửi thông báo (để lỗi ở bước này không nuốt mất thông báo daily):

```bash
"$(dirname "$0")/desk" --json reflect > /dev/null
```

- [ ] **Step 6: Cập nhật README** — trong khối lệnh của mục "Luồng sử dụng", thêm dòng `uv run desk --config config.yaml --json reflect` ngay trước dòng `tickets`. Thêm câu này dưới đoạn mô tả `desk scorecard`:

```markdown
`desk reflect` chấm ngay mọi quyết định crypto và VN đã đủ 20 ngày mà không chạy phân
tích mới; `scripts/daily.sh` gọi nó sau job daily.
```

- [ ] **Step 7: Chạy toàn bộ và commit**

Run: `uv run pytest -q && uv run ruff check src tests`

```bash
git add src/crypto_desk/cli.py scripts/daily.sh README.md tests/test_service_cli.py
git commit -m "feat: add desk reflect to grade due decisions without an analysis run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Bull/bear/manager không còn thấy số khung giờ và sổ lệnh thô

**Files:**
- Modify: `src/crypto_desk/committee.py`: thêm hằng và hàm `_debate_snapshot` ngay trên `def _system_prompt(` (dòng ~210); sửa `_base_payload` (dòng ~1063-1071)
- Test: `tests/test_committee.py`

**Interfaces:**
- Consumes: `to_jsonable` từ `.domain` (đã import).
- Produces: `_debate_snapshot(snapshot: Any) -> dict[str, Any]` (module-level). `payload["snapshot"]` của bull/bear/manager chỉ còn `symbol`, `cutoff`, `binance_mid` hoặc `mid`, `industry` (VN) và `items` đã lọc.

- [ ] **Step 1: Viết test fail** — thêm vào `tests/test_committee.py`, sau `test_committee_prompts_require_english_and_isolate_specialists`:

```python
def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_debate_payloads_hide_hour_scale_fields_but_keep_what_prompts_require():
    fake_llm = FakeLLM()
    snapshot = valid_snapshot()

    CryptoCommittee(fake_llm).run(snapshot)

    debate = [r for r in fake_llm.requests if r["stage"].startswith(("bull", "bear", "manager"))]
    assert debate
    for request in debate:
        keys = _all_keys(request["payload"]["snapshot"])
        # Specialists đã đọc các trường này; ở horizon 20 ngày chúng chỉ là nhiễu bị viện dẫn.
        assert not {"depth", "oi_change_1h_pct", "taker_buy_sell_ratio"} & keys
        # Chuỗi close chỉ đi một lần, trong item spot.
        assert "daily_closes" not in request["payload"]["snapshot"]
        assert {
            "atr14_1d",
            "depth_summary",
            "taker_buy_sell_ratio_24h",
            "long_short_ratio_pctile_20d",
            "daily_closes",
        } <= keys
        assert request["payload"]["evidence_ids"] == list(snapshot.evidence_ids)
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_committee.py::test_debate_payloads_hide_hour_scale_fields_but_keep_what_prompts_require -v`
Expected: FAIL, vì `{'depth', 'oi_change_1h_pct', 'taker_buy_sell_ratio'}` vẫn có mặt.

- [ ] **Step 3: Sửa code** — thêm vào `committee.py`, ngay trên `def _system_prompt(`:

```python
# Specialists already read these; at the desk's horizon the debate kept citing them as
# reasons (hourly taker flow, a 20-level book spanning ~0.01% of price).
DEBATE_HIDDEN_FIELDS = {
    "spot": ("depth",),
    "derivatives": ("oi_change_1h_pct", "taker_buy_sell_ratio"),
}
# Top-level snapshot fields repeat the item payloads (closes twice, hourly ratios again),
# so the debate reads items only.
DEBATE_SNAPSHOT_FIELDS = ("symbol", "cutoff", "binance_mid", "mid", "industry")


def _debate_snapshot(snapshot: Any) -> dict[str, Any]:
    full = to_jsonable(snapshot)
    items = [
        {
            **item,
            "payload": {
                name: value
                for name, value in item["payload"].items()
                if name not in DEBATE_HIDDEN_FIELDS.get(item["kind"], ())
            },
        }
        for item in full["items"]
    ]
    return {name: full[name] for name in DEBATE_SNAPSHOT_FIELDS if name in full} | {
        "items": items
    }
```

Trong `_base_payload`, đổi `"snapshot": to_jsonable(snapshot),` thành `"snapshot": _debate_snapshot(snapshot),`.

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_committee.py tests/test_vn_service.py -v && uv run pytest -q`
Expected: PASS. Test VN phải xanh: `VNEvidenceSnapshot` có `symbol, cutoff, items, mid, industry`, đều được giữ.

- [ ] **Step 5: Commit**

```bash
git add src/crypto_desk/committee.py tests/test_committee.py
git commit -m "feat: keep hour-scale and duplicated evidence out of the debate payload

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Code tự tính mức cấu trúc (swing high/low, biên 20/55 ngày)

**Files:**
- Modify: `src/crypto_desk/indicators.py` (thêm `swing_points`)
- Modify: `src/crypto_desk/data.py`: import `swing_points` (dòng 17); thêm `price_structure` ngay trên `def news_aliases(` (dòng ~392); thêm key vào `spot_payload` trong `EvidenceBuilder.build` (dòng ~562)
- Modify: `src/crypto_desk/committee.py`: `SPECIALIST_EVIDENCE["technical"]`, `ROLE_PROMPTS["technical"]`, `ROLE_PROMPTS["manager"]`
- Modify: `README.md`
- Test: `tests/test_indicators.py`, `tests/test_data.py`, `tests/test_committee.py`

**Interfaces:**
- Produces:
  - `swing_points(highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], width: int = 2) -> tuple[tuple[int, ...], tuple[int, ...]]` (chỉ số swing high, chỉ số swing low).
  - `price_structure(highs, lows, dates: tuple[str, ...], mid: Decimal, tick: Decimal) -> dict[str, Any]` với các khoá `version="structure-v1"`, `high_20d`, `low_20d`, `high_55d`, `low_55d` (`Decimal | None`), `swing_supports`, `swing_resistances` (mỗi phần tử là `list[{"price": Decimal, "date": "YYYY-MM-DD"}]`, tối đa 3, gần giá nhất trước).
  - `spot.payload["price_structure"]`: key song song với `technical_indicators`. Dashboard chỉ đọc `technical_indicators` nên không bị ảnh hưởng.

- [ ] **Step 1: Viết test fail cho `swing_points`** — thêm vào `tests/test_indicators.py` (sửa dòng import thành `from crypto_desk.indicators import atr, ema, rsi, swing_points`):

```python
def test_swing_points_mark_confirmed_daily_extremes_once():
    highs = tuple(map(Decimal, (10, 11, 15, 15, 11, 13, 12)))
    lows = tuple(map(Decimal, (9, 8, 12, 10, 7, 11, 10)))

    swing_highs, swing_lows = swing_points(highs, lows, width=2)

    # Đỉnh 15 lặp ở hai nến chỉ tính nến đầu; hai nến cuối chưa đủ nến xác nhận.
    assert swing_highs == (2,)
    assert swing_lows == (4,)
```

- [ ] **Step 2: Chạy để thấy fail**

Run: `uv run pytest tests/test_indicators.py -v`
Expected: FAIL với `ImportError: cannot import name 'swing_points'`.

- [ ] **Step 3: Thêm `swing_points`** vào cuối `src/crypto_desk/indicators.py`:

```python
def swing_points(
    highs: tuple[Decimal, ...],
    lows: tuple[Decimal, ...],
    width: int = 2,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Indices of confirmed swing highs and lows.

    A swing high beats the ``width`` bars before it strictly and is not exceeded by the
    ``width`` bars after it, so a flat top counts once and the last ``width`` bars are
    never swings: nothing has confirmed them yet.
    """
    last = len(highs) - width
    swing_highs = tuple(
        index
        for index in range(width, last)
        if highs[index] > max(highs[index - width : index])
        and highs[index] >= max(highs[index + 1 : index + width + 1])
    )
    swing_lows = tuple(
        index
        for index in range(width, last)
        if lows[index] < min(lows[index - width : index])
        and lows[index] <= min(lows[index + 1 : index + width + 1])
    )
    return swing_highs, swing_lows
```

- [ ] **Step 4: Chạy test**

Run: `uv run pytest tests/test_indicators.py -v`
Expected: PASS.

- [ ] **Step 5: Viết test fail cho `price_structure` và builder** — thêm vào cuối `tests/test_data.py`:

```python
def test_price_structure_keeps_the_nearest_swings_on_each_side_of_mid():
    from crypto_desk.data import price_structure

    highs = tuple(map(Decimal, (105, 108, 112, 109, 104, 103, 106, 110, 107, 105, 104)))
    lows = tuple(map(Decimal, (100, 103, 106, 101, 96, 98, 101, 104, 99, 97, 98)))
    dates = tuple(f"2026-07-{day:02d}" for day in range(1, 12))

    structure = price_structure(highs, lows, dates, Decimal("105"), Decimal("1"))

    assert structure["swing_supports"] == [{"price": Decimal("96"), "date": "2026-07-05"}]
    assert structure["swing_resistances"] == [
        {"price": Decimal("110"), "date": "2026-07-08"},
        {"price": Decimal("112"), "date": "2026-07-03"},
    ]
    # 11 nến: chưa đủ lịch sử cho biên 20/55 ngày (coin mới niêm yết).
    assert structure["high_20d"] is None
    assert structure["low_55d"] is None


def test_spot_evidence_carries_price_structure_beside_technical_indicators():
    snapshot = EvidenceBuilder(FakePublicClient(), {"BTCUSDT": "bitcoin"}).build("BTCUSDT", CUTOFF)

    spot = next(item for item in snapshot.items if item.kind == "spot")
    structure = spot.payload["price_structure"]

    # Fixture: close = 90000 + 100 × index, high/low = close ± 20, 120 nến tăng đều.
    assert structure["version"] == "structure-v1"
    assert structure["high_20d"] == "101920.00"
    assert structure["low_20d"] == "99980.00"
    assert structure["high_55d"] == "101920.00"
    assert structure["low_55d"] == "96480.00"
    assert structure["swing_supports"] == []
    assert structure["swing_resistances"] == []
    assert spot.payload["technical_indicators"]["version"] == "technical-v1"
```

- [ ] **Step 6: Chạy để thấy fail**

Run: `uv run pytest tests/test_data.py -k price_structure -v`
Expected: FAIL (`ImportError` cho test đầu, `KeyError: 'price_structure'` cho test thứ hai).

- [ ] **Step 7: Thêm `price_structure` và nối vào builder**

Trong `src/crypto_desk/data.py`, đổi import thành `from .indicators import atr, ema, rsi, swing_points`. Thêm ngay trên `def news_aliases(`:

```python
def price_structure(
    highs: tuple[Decimal, ...],
    lows: tuple[Decimal, ...],
    dates: tuple[str, ...],
    mid: Decimal,
    tick: Decimal,
) -> dict[str, Any]:
    """Hỗ trợ/kháng cự từ nến ngày đã đóng, do code tính thay vì để model suy từ close.

    Không có mức nào để neo, manager từng đặt stop đúng ở ngưỡng tối thiểu của cổng
    R:R. Danh sách này cho nó mức thật, gồm cả râu nến mà chuỗi close không có.
    """
    swing_highs, swing_lows = swing_points(highs, lows)

    def extreme(pick: Callable[..., Decimal], values: tuple[Decimal, ...], window: int):
        return pick(values[-window:]).quantize(tick) if len(values) >= window else None

    def level(index: int, values: tuple[Decimal, ...]) -> dict[str, Any]:
        return {"price": values[index].quantize(tick), "date": dates[index]}

    supports = sorted(
        (level(index, lows) for index in swing_lows if lows[index] < mid),
        key=lambda item: item["price"],
        reverse=True,
    )
    resistances = sorted(
        (level(index, highs) for index in swing_highs if highs[index] > mid),
        key=lambda item: item["price"],
    )
    return {
        "version": "structure-v1",
        "high_20d": extreme(max, highs, 20),
        "low_20d": extreme(min, lows, 20),
        "high_55d": extreme(max, highs, 55),
        "low_55d": extreme(min, lows, 55),
        "swing_supports": supports[:3],
        "swing_resistances": resistances[:3],
    }
```

`Callable` đã được import trong `data.py`. Trong `EvidenceBuilder.build`, ngay sau dòng `daily_closes, four_hour_closes = daily_hlc[2], four_hour_hlc[2]`, thêm:

```python
        daily_dates = tuple(_utc_from_ms(row[0]).date().isoformat() for row in daily.payload)
```

Trong `spot_payload`, thêm ngay sau `"technical_indicators": technical_indicators,`:

```python
            "price_structure": price_structure(
                daily_hlc[0], daily_hlc[1], daily_dates, mid, tick
            ),
```

- [ ] **Step 8: Chạy test data**

Run: `uv run pytest tests/test_data.py tests/test_dashboard.py -v`
Expected: PASS. Dashboard vẫn xanh vì chỉ đọc `technical_indicators`.

- [ ] **Step 9: Viết test fail cho committee** — trong `tests/test_committee.py`:

(a) Trong `valid_snapshot`, thêm vào payload `"spot"` ngay sau khối `"technical_indicators": {...},`:

```python
            "price_structure": {
                "version": "structure-v1",
                "high_20d": "101000",
                "low_20d": "96000",
                "high_55d": "104000",
                "low_55d": "90000",
                "swing_supports": [{"price": "97500", "date": "2026-07-10"}],
                "swing_resistances": [{"price": "102500", "date": "2026-07-05"}],
            },
```

(b) Trong `test_committee_prompts_require_english_and_isolate_specialists`, thêm `"price_structure",` vào tập `expected["technical"]`. Thêm hai assert cuối test:

```python
    assert "price_structure" in requests["technical"]["system_prompt"]
    assert "price_structure" in requests["manager"]["system_prompt"]
```

- [ ] **Step 10: Chạy để thấy fail**

Run: `uv run pytest tests/test_committee.py::test_committee_prompts_require_english_and_isolate_specialists -v`
Expected: FAIL, vì tập field của technical thiếu `price_structure`.

- [ ] **Step 11: Sửa committee**

Trong `SPECIALIST_EVIDENCE["technical"]`, thêm `"price_structure",` sau `"technical_indicators",`.

Trong `ROLE_PROMPTS["technical"]`, thêm câu sau ngay sau câu `"Identify each indicator's timeframe and closed-candle as_of timestamp. "`:

```python
        "price_structure lists confirmed daily swing supports below and swing resistances "
        "above Spot mid, with dates, plus 20/55-day high/low ranges, all from closed daily "
        "candles including wicks; prefer these levels over support/resistance inferred "
        "from closes. "
```

Trong `ROLE_PROMPTS["manager"]` (dòng ~149), tách dòng `"technical_indicators.atr14_1d below entry; tighter stops are rejected. Place the stop "` thành:

```python
        "technical_indicators.atr14_1d below entry; tighter stops are rejected. "
        "Prefer a stop just below a price_structure swing support, low_20d or low_55d, and "
        "a target at or below a swing resistance, high_20d or high_55d. Place the stop "
```

Chèn vào chỗ này để câu "if none does…" phía sau vẫn chỉ tới việc tìm target.

- [ ] **Step 12: README** — sau đoạn mô tả evidence kỹ thuật (câu kết thúc bằng "…và không tự tạo tín hiệu giao dịch."), thêm:

```markdown
`price_structure` đi cạnh các chỉ báo: tối đa ba swing low dưới giá và ba swing high trên
giá (xác nhận bởi hai nến ngày mỗi bên, có ngày), cùng biên cao/thấp 20 và 55 ngày, tính
từ high/low nến đã đóng. Manager được yêu cầu neo stop/target vào các mức này.
```

- [ ] **Step 13: Chạy toàn bộ và commit**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS, ruff sạch.

```bash
git add src/crypto_desk/indicators.py src/crypto_desk/data.py src/crypto_desk/committee.py README.md tests/test_indicators.py tests/test_data.py tests/test_committee.py
git commit -m "feat: compute daily swing levels so stops anchor to structure

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Vận hành — dọn reflection hỏng, chạy lại cron, chấm bù

Không có code. Mỗi bước có tác động ra ngoài hoặc xoá dữ liệu cần **người dùng xác nhận** trước khi chạy.

- [ ] **Step 1: Kiểm tra cron daily.** Bucket `daily` gần nhất là `2026-09-04`.

```bash
hermes cron list
```

Nếu job `crypto-desk-daily` mất hoặc bị tắt, tạo lại theo mục "Lịch Hermes" trong README. Nếu job còn đó, xem log gateway của Hermes để tìm lý do dừng từ 04/09.

- [ ] **Step 2: Sao lưu DB (WAL mode, không `cp`).**

```bash
uv run python -c "import sqlite3; s = sqlite3.connect('data/crypto_desk.sqlite3'); d = sqlite3.connect('data/crypto_desk.sqlite3.bak-2026-09-27'); s.backup(d); d.close(); s.close()"
```

- [ ] **Step 3: Xoá 9 reflection kiểu cũ (cần xác nhận).**
  - 7 hàng là run hỏng: `provider:rate_limit` / `provider:model_unavailable`, ETH/BTC ngày 17/07.
  - 2 hàng là HOLD thật nhưng thiếu `decision_cutoff` và kết quả chạm mức.
  - Mọi số liệu đều tính lại được từ nến Binance và `evidence.json` còn lưu. `desk reflect` chỉ tạo lại 2 hàng thật.

```bash
uv run python -c "import sqlite3; db = sqlite3.connect('data/crypto_desk.sqlite3'); print(db.execute(\"DELETE FROM reflections WHERE json_extract(payload, '\$.decision_cutoff') IS NULL\").rowcount); db.commit()"
```

Expected: in ra `9`.

- [ ] **Step 4: Chấm bù.**

```bash
uv run desk --config config.yaml --json reflect
```

Expected: `crypto` chứa khoảng 13 run id (2 HOLD vừa xoá cộng 11 run tháng 7 từng bị kẹt), `vn` là `[]` cho tới khi run VN đầu tiên đủ 20 phiên (khoảng giữa tháng 10).

- [ ] **Step 5: Xác nhận scorecard.**

```bash
uv run desk --config config.yaml scorecard
```

Expected:
- Không còn nhóm `NO_TRADE` gồm 4 run ETH bị rate limit.
- Hàng `ACCUMULATE` có số ở các cột "TP trước" và "Kỳ vọng" (từ 5 ACCUMULATE có mức giá của tháng 7).
- Các nhóm có dòng "Benchmark của nhóm này được suy ra…" biến mất, vì hàng mới ghi `benchmark_symbol`.

## Ngoài phạm vi (và khi nào làm)

- **Code ép stop/target phải nằm gần một mức `price_structure`:** hiện chỉ yêu cầu trong prompt. Làm khi scorecard cho thấy setup có "Kỳ vọng" âm và stop không nằm ở mức cấu trúc nào.
- **`price_structure` cho VN:** evidence VN chỉ có close điều chỉnh, không có high/low. Làm khi reflection VN (Task 2) có đủ mẫu.
- **Cờ "không chấm được" trên `research_runs`:** chỉ cần khi quét cả bảng (Task 1) trở nên chậm.
