# VN Equities Research Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `desk vn-analyze FPT` chạy hội đồng 5 chuyên gia trên cổ phiếu HOSE và ghi quyết định vào cùng kho với crypto, không đụng đường thực thi.

**Architecture:** Đường song song dùng chung `CryptoCommittee`. Ba module mới cho VN cộng một script độc lập lấy báo cáo tài chính ở venv riêng. Một chỗ chạm có kiểm soát vào `committee.py`: tham số hoá bộ chuyên gia và cho phép bỏ qua chuyên gia thiếu evidence, mặc định giữ nguyên hành vi crypto.

**Tech Stack:** Python 3.12, `httpx`, `Decimal`, dataclass `frozen=True, slots=True`, SQLite qua `Store`, Typer, pytest. `vnstock` **chỉ** trong venv riêng của script, không vào `pyproject.toml`.

**Spec:** `docs/superpowers/specs/2026-09-19-vn-equities-research-design.md` — đọc cùng plan này. Mười bất biến trong đó là ràng buộc, không phải gợi ý.

## Điều kiện tiên quyết

**Plan `2026-09-20-reflection-pipeline-integrity.md` phải chạy xong trước.** Plan này tiêu thụ ba thứ do nó tạo ra:

- `crypto_desk.domain.is_decided(decision) -> bool`
- `Store.unreflected_runs(completed_before, symbols)`
- khoá `benchmark_symbol` trong payload reflection

Nếu chưa có, dừng và báo — đừng tự dựng bản thay thế.

## Global Constraints

- Python `>=3.12,<3.13`. Ruff `line-length = 100`, `target-version = "py312"`.
- Mọi số tiền và tỉ lệ dùng `Decimal`, không `float`.
- Dataclass mới: `@dataclass(frozen=True, slots=True)`.
- Mọi file `.py` mở đầu bằng `from __future__ import annotations`.
- Văn bản hướng tới người dùng và hướng tới model: **tiếng Việt có dấu**.
- Không đụng `risk.py`, `execution.py`, `broker.py`, `dispatcher.py`, `SymbolRules`.
- Không đổi schema SQLite.
- **Không thêm dependency vào `pyproject.toml`.** `vnstock` sống trong `.venv-fundamentals/`, cách ly khỏi process của desk.
- Chạy test: `uv run pytest`. Lint: `uv run ruff check src tests`.

---

## Dữ liệu đã khảo sát thật (không phải giả định)

Mọi endpoint dưới đây đã gọi thật ngày 2026-09-19/20, không cần API key.

| Endpoint | Dùng cho |
|---|---|
| `https://iboard-api.ssi.com.vn/statistics/charts/history?resolution=1D&symbol=X&from=<epoch>&to=<epoch>` | chuỗi OHLCV; cũng phục vụ `VN30` |
| `https://iboard-api.ssi.com.vn/statistics/company/ssmi/stock-info?symbol=X&fromDate=DD/MM/YYYY&toDate=DD/MM/YYYY` | phiên: OHLCV, khối ngoại, số lệnh, trần/sàn |
| `https://iboard-api.ssi.com.vn/statistics/company/ssmi/company-profile?symbol=X` | `industryName` để phân loại ngành |
| `https://iboard-query.ssi.com.vn/stock/exchange/hose` | `matchedPrice` để kiểm chéo |

Số liệu phiên 18/09/2026, dùng làm fixture test:

```
FPT  close 65182.47  closeRaw 71700  floorPrice 69100  ceilingPrice 79500  refPrice 74300
MBB  close 19900     closeRaw 19900  floorPrice 19150  ceilingPrice 21950  refPrice 20550
     MBB foreignBuyVolTotal 2753257  foreignSellVolTotal 7935500  netBuySellVol -5182243
```

`industryName`: FPT = `"Công nghệ Thông tin"`, MBB = `"Ngân hàng"`.

---

## File Structure

**Tạo:**
- `src/crypto_desk/vn_data.py` — `SSIClient`, `VNEvidenceSnapshot`, `VNEvidenceBuilder`
- `src/crypto_desk/vn_fundamentals.py` — đọc cache, lọc theo ngành (`BANK_METRICS`, `NONBANK_METRICS`)
- `src/crypto_desk/vn_prompts.py` — 5 chuyên gia + bull/bear/manager bản VN
- `src/crypto_desk/vn_service.py` — `VNDeskService`
- `scripts/fetch_fundamentals.py` — script độc lập, venv riêng
- `tests/test_vn_data.py`, `tests/test_vn_fundamentals.py`, `tests/test_vn_service.py`

**Sửa:**
- `src/crypto_desk/committee.py` — tham số hoá bộ chuyên gia, bỏ qua chuyên gia thiếu evidence
- `src/crypto_desk/domain.py` — thêm `"flow"`, `"fundamentals"` vào `EvidenceKind`
- `src/crypto_desk/data.py` — `EvidenceSnapshot` thêm `@property mid`
- `src/crypto_desk/config.py` — `vn_symbols`, `vn_news_feeds`, hằng số VN
- `src/crypto_desk/cli.py` — `vn-analyze`, `vn-daily`

---

### Task 1: Committee nhận bộ chuyên gia và bỏ qua chuyên gia thiếu evidence

**Files:**
- Modify: `src/crypto_desk/committee.py` (`CryptoCommittee.__init__`, `run`, `_specialist_payload`, `_system_prompt`, `CommitteeResult`)
- Modify: `src/crypto_desk/data.py` (`EvidenceSnapshot`, thêm property)
- Modify: `src/crypto_desk/domain.py` (`EvidenceKind`)
- Test: `tests/test_committee.py`

**Interfaces:**
- Produces:
  - `CryptoCommittee(..., specialists=SPECIALISTS, role_prompts=ROLE_PROMPTS, specialist_evidence=SPECIALIST_EVIDENCE, optional_kinds=frozenset(), mid_label="Binance mid")`
  - `CommitteeResult.skipped_specialists: tuple[str, ...]`
  - `EvidenceSnapshot.mid -> Decimal`
  - `EvidenceKind` thêm `"flow"`, `"fundamentals"`

- [ ] **Step 1: Viết test thất bại**

Thêm vào `tests/test_committee.py`:

```python
def test_crypto_behaviour_is_unchanged_when_no_new_arguments_are_passed():
    from crypto_desk.committee import (
        ROLE_PROMPTS,
        SPECIALISTS,
        SPECIALIST_EVIDENCE,
        CryptoCommittee,
    )

    committee = CryptoCommittee(object())

    assert committee.specialists == SPECIALISTS
    assert committee.role_prompts == ROLE_PROMPTS
    assert committee.specialist_evidence == SPECIALIST_EVIDENCE
    assert committee.optional_kinds == frozenset()
    assert committee.mid_label == "Binance mid"


def test_required_kinds_are_derived_from_the_evidence_map_excluding_optional():
    from crypto_desk.committee import SPECIALIST_EVIDENCE, CryptoCommittee

    committee = CryptoCommittee(object())

    assert committee.required_kinds == {
        kind for kind, _ in SPECIALIST_EVIDENCE.values()
    } | {"reference"}
    assert committee.required_kinds == {"spot", "news", "derivatives", "reference"}

    vn_committee = CryptoCommittee(
        object(),
        specialists=("technical", "cơ bản"),
        specialist_evidence={
            "technical": ("spot", ("symbol", "mid")),
            "cơ bản": ("fundamentals", ("symbol",)),
        },
        optional_kinds=frozenset({"fundamentals"}),
    )
    assert vn_committee.required_kinds == {"spot", "reference"}
    assert vn_committee.optional_kinds == frozenset({"fundamentals"})


def test_a_specialist_whose_evidence_is_absent_is_skipped_not_fatal():
    from crypto_desk.committee import CryptoCommittee

    committee = CryptoCommittee(
        object(),
        specialists=("technical", "cơ bản"),
        specialist_evidence={
            "technical": ("spot", ("symbol", "mid")),
            "cơ bản": ("fundamentals", ("symbol",)),
        },
        optional_kinds=frozenset({"fundamentals"}),
    )
    snapshot = make_snapshot("BTCUSDT")  # chỉ có spot/news/derivatives/reference

    payload = committee._specialist_payload(snapshot, "cơ bản")

    assert payload is None
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_committee.py -k "unchanged or required_kinds or absent" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'specialists'`

- [ ] **Step 3: Tham số hoá `__init__`**

Trong `src/crypto_desk/committee.py`, thêm vào `CryptoCommittee.__init__` sau `debate_rounds`:

```python
        specialists: tuple[str, ...] = SPECIALISTS,
        role_prompts: dict[str, str] | None = None,
        specialist_evidence: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        optional_kinds: frozenset[str] = frozenset(),
        mid_label: str = "Binance mid",
```

và trong thân hàm:

```python
        self.specialists = specialists
        self.role_prompts = role_prompts or ROLE_PROMPTS
        self.specialist_evidence = specialist_evidence or SPECIALIST_EVIDENCE
        self.optional_kinds = optional_kinds
        self.mid_label = mid_label
        # Suy ra thay vì ghi cứng: một bộ chuyên gia khác kéo theo một tập kind
        # khác, loại trừ các kind tùy chọn (optional_kinds) như fundamentals của VN.
        self.required_kinds = {
            kind for kind, _ in self.specialist_evidence.values()
            if kind not in self.optional_kinds
        } | {"reference"}
```

- [ ] **Step 4: Dùng chúng trong `run()`**

Trong `run()`:

- `required_kinds = {"spot", "news", "derivatives", "reference"}` và kiểm tra snapshot ở đầu `run()`:
  Thay khối kiểm tra:
  ```python
          required_kinds = {"spot", "news", "derivatives", "reference"}
          if {item.kind for item in snapshot.items} != required_kinds or any(
              item.stale for item in snapshot.items
          ):
  ```
  bằng:
  ```python
          present_kinds = {item.kind for item in snapshot.items}
          allowed_kinds = self.required_kinds | self.optional_kinds
          if not self.required_kinds.issubset(present_kinds) or not present_kinds.issubset(allowed_kinds) or any(
              item.stale for item in snapshot.items
          ):
  ```
  (Với crypto, `optional_kinds` rỗng nên tương đương hoàn toàn so sánh bằng `== required_kinds`. Với VN, snapshot thiếu `fundamentals` vẫn thoả mãn `required_kinds.issubset(present_kinds)` để committee chạy tiếp với 4 chuyên gia còn lại theo Bất biến 10).
- `for role in SPECIALISTS:` → `for role in self.specialists:`
- `snapshot.binance_mid` (3 chỗ) → `snapshot.mid`
- `system_prompt=_system_prompt(role)` → `system_prompt=self._role_system_prompt(role)`

Thêm phương thức:

```python
    def _role_system_prompt(self, role: str) -> str:
        return f"{OUTPUT_CONTRACT}\n\n{self.role_prompts[role]}"
```

Vòng lặp chuyên gia bỏ qua khi thiếu evidence:

```python
            skipped: list[str] = []
            for role in self.specialists:
                specialist = self._specialist_payload(snapshot, role)
                if specialist is None:
                    skipped.append(role)
                    continue
                role_payload, role_evidence_ids = specialist
                reports[role] = self._call(...)   # giữ nguyên lời gọi hiện có
```

và `CommitteeResult` mang `skipped_specialists=tuple(skipped)` ở mọi đường trả về.

- [ ] **Step 5: `_specialist_payload` trả `None` thay vì ném**

Đổi chữ ký sang `-> tuple[dict[str, Any], tuple[str, ...]] | None` và thay:

```python
        evidence = next(item for item in snapshot.items if item.kind == kind)
```

bằng:

```python
        evidence = next((item for item in snapshot.items if item.kind == kind), None)
        if evidence is None:
            return None
```

- [ ] **Step 6: `CommitteeResult` thêm trường**

```python
    skipped_specialists: tuple[str, ...] = ()
```

- [ ] **Step 7: Sửa thông báo lệch giá entry**

Trong `_validate_manager`, đổi `raise ValueError("entry price deviates more than 2% from Binance mid")` thành thông báo dựng từ `mid_label`. Vì `_validate_manager` đang là `@staticmethod`, đổi nó thành method thường và truyền `self.mid_label` vào; cập nhật chỗ gọi trong `_call`.

- [ ] **Step 8: `EvidenceSnapshot.mid` và `EvidenceKind`**

Trong `src/crypto_desk/data.py`, thêm vào `EvidenceSnapshot`:

```python
    @property
    def mid(self) -> Decimal:
        """Giá tham chiếu chung cho committee, không phụ thuộc sàn nào."""
        return self.binance_mid
```

Trong `src/crypto_desk/domain.py`:

```python
EvidenceKind = Literal["spot", "news", "derivatives", "reference", "flow", "fundamentals"]
```

- [ ] **Step 9: Chạy toàn bộ test và lint**

Run: `uv run pytest && uv run ruff check src tests`
Expected: toàn bộ pass. Crypto không được đổi hành vi — nếu một test committee vỡ, đó là dấu hiệu tham số mặc định chưa đúng bằng giá trị module-level.

- [ ] **Step 10: Commit**

```bash
git add src/crypto_desk/committee.py src/crypto_desk/data.py \
        src/crypto_desk/domain.py tests/test_committee.py
git commit -m "feat: let the committee take its own panel and skip absent evidence

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Config cho đường VN

**Files:**
- Modify: `src/crypto_desk/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: không.
- Produces:
  - `config.VN_BENCHMARK_SYMBOL = "VN30"`
  - `config.VN_V1_SYMBOLS = frozenset({"FPT", "MBB"})`
  - `Settings.vn_symbols: tuple[str, ...]`, mặc định `("FPT", "MBB")`
  - `Settings.vn_news_feeds: tuple[str, ...]`
  - `Settings.fundamentals_dir: Path`, mặc định `Path("data/fundamentals")`

- [ ] **Step 1: Viết test thất bại**

Thêm vào `tests/test_config.py`:

```python
def test_vn_symbols_do_not_go_through_the_usdt_validation(tmp_path):
    from crypto_desk.config import load_settings

    config = tmp_path / "config.yaml"
    config.write_text(
        "database: db.sqlite3\n"
        "symbols: [BTCUSDT]\n"
        "coingecko_ids: {BTCUSDT: bitcoin}\n"
        "vn_symbols: [FPT, MBB]\n",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.vn_symbols == ("FPT", "MBB")


def test_vn_symbol_outside_the_v1_allowlist_is_rejected(tmp_path):
    import pytest

    from crypto_desk.config import load_settings

    config = tmp_path / "config.yaml"
    config.write_text(
        "database: db.sqlite3\n"
        "symbols: [BTCUSDT]\n"
        "coingecko_ids: {BTCUSDT: bitcoin}\n"
        "vn_symbols: [FPT, VIC]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="VN"):
        load_settings(config)
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_config.py -k vn_symbol -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'vn_symbols'`

- [ ] **Step 3: Thêm hằng số và trường**

Trong `src/crypto_desk/config.py`, cạnh `HARD_MAINNET_CAP_USDT`:

```python
VN_BENCHMARK_SYMBOL = "VN30"
VN_V1_SYMBOLS = frozenset({"FPT", "MBB"})
DEFAULT_VN_NEWS_FEEDS = (
    "https://cafef.vn/thi-truong-chung-khoan.rss",
    "https://vietstock.vn/144/chung-khoan/co-phieu.rss",
)
```

Trong `Settings`:

```python
    vn_symbols: tuple[str, ...] = ("FPT", "MBB")
    vn_news_feeds: tuple[str, ...] = DEFAULT_VN_NEWS_FEEDS
    fundamentals_dir: Path = Path("data/fundamentals")
```

Trong hàm validate, thêm — **tách hoàn toàn khỏi kiểm tra USDT của crypto**:

```python
    if any(symbol not in VN_V1_SYMBOLS for symbol in settings.vn_symbols):
        raise ValueError("VN symbol is outside the V1 allowlist (FPT, MBB)")
```

Trong hàm đọc YAML, đọc `vn_symbols`, `vn_news_feeds`, `fundamentals_dir` theo đúng khuôn các khoá sẵn có.

- [ ] **Step 4: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_config.py -k vn_symbol -v`
Expected: 2 passed

- [ ] **Step 5: Thêm vào `config.example.yaml`**

```yaml
vn_symbols: [FPT, MBB]
vn_news_feeds:
  - https://cafef.vn/thi-truong-chung-khoan.rss
  - https://vietstock.vn/144/chung-khoan/co-phieu.rss
fundamentals_dir: data/fundamentals
```

- [ ] **Step 6: Chạy toàn bộ test, lint, commit**

```bash
uv run pytest && uv run ruff check src tests
git add src/crypto_desk/config.py config.example.yaml tests/test_config.py
git commit -m "feat: configure the VN allowlist apart from the USDT one

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `vn_data.py` — evidence thị trường từ SSI

**Files:**
- Create: `src/crypto_desk/vn_data.py`
- Test: `tests/test_vn_data.py`

**Interfaces:**
- Consumes: `EvidenceItem`, `EvidenceError`, `Fetched` từ `data.py`/`domain.py`; `VN_BENCHMARK_SYMBOL` từ Task 2.
- Produces:
  - `VNEvidenceSnapshot` — `symbol`, `cutoff`, `items`, `mid`, `.evidence_ids`
  - `VNEvidenceBuilder.build(symbol, cutoff) -> VNEvidenceSnapshot`
  - `VNEvidenceBuilder.reflection_closes(symbol, start, periods=20) -> tuple[Decimal, ...]`
  - `SSIClient` với `charts_history`, `stock_info`, `company_profile`, `board_snapshot`

Đây là task lớn nhất và mang bốn bất biến. Đọc mục "Bất biến bắt buộc" 1–4 trong spec trước khi viết dòng nào.

- [ ] **Step 1: Viết test cho bất biến 1 — giá điều chỉnh vs giá thô**

Tạo `tests/test_vn_data.py`. Fixture dùng **FPT**, không dùng MBB: MBB không lệch nên test trên nó sẽ pass mà không kiểm được gì.

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_desk.data import EvidenceError

FPT_SESSION = {
    "symbol": "FPT",
    "tradingDate": "18/09/2026",
    "close": "65182.47",
    "closeRaw": "71700",
    "open": "67727.95",
    "openRaw": "74500",
    "high": "68000.68",
    "highRaw": "74800",
    "low": "65182.47",
    "lowRaw": "71700",
    "ceilingPrice": "79500",
    "floorPrice": "69100",
    "refPrice": "74300",
    "totalMatchVol": "15500700",
    "totalMatchVal": "1129617650000",
    "avgPrice": "66250.663",
    "totalBuyTrade": "10000",
    "totalSellTrade": "9000",
    "foreignBuyVolTotal": "100000",
    "foreignSellVolTotal": "200000",
    "foreignCurrentRoom": "50000000",
    "netBuySellVol": "-100000",
}


def test_mid_is_the_raw_traded_price_never_the_adjusted_one():
    from crypto_desk.vn_data import session_mid

    assert session_mid(FPT_SESSION) == Decimal("71700")


def test_the_adjusted_close_would_sit_below_the_floor_and_must_never_be_compared_to_it():
    """Bất biến 1: FPT close 65182.47 < floor 69100. Trộn hai loại là kết luận điều bất khả."""
    from crypto_desk.vn_data import session_mid

    mid = session_mid(FPT_SESSION)
    floor = Decimal(FPT_SESSION["floorPrice"])
    ceiling = Decimal(FPT_SESSION["ceilingPrice"])

    assert floor <= mid <= ceiling
    assert Decimal(FPT_SESSION["close"]) < floor
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_vn_data.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crypto_desk.vn_data'`

- [ ] **Step 3: Viết phần giá của `vn_data.py`**

```python
"""Evidence thị trường cho cổ phiếu HOSE, lấy từ SSI iboard.

Hai quy tắc chi phối module này, cả hai đến từ dữ liệu thật chứ không từ suy luận:

1. Giá điều chỉnh và giá thô không được trộn. Phiên 18/09/2026 của FPT có
   ``close`` 65182.47 trong khi ``floorPrice`` là 69100 — giá điều chỉnh nằm
   *dưới* giá sàn. Lợi nhuận tính từ giá điều chỉnh; mọi so sánh với biên độ,
   mọi đối chiếu, mọi hiển thị giá khớp dùng giá thô.
2. Giá khớp phải khớp giữa hai service SSI. Yếu hơn cross-vendor vì cùng nhà
   cung cấp — giới hạn này được ghi vào báo cáo, không giấu.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

MAX_SOURCE_DEVIATION = Decimal("0.005")


def session_mid(session: dict[str, Any]) -> Decimal:
    """Giá khớp của phiên, luôn là giá THÔ.

    ``close`` là giá đã điều chỉnh theo sự kiện quyền và có thể nằm ngoài biên
    độ của chính phiên đó, nên nó không bao giờ được dùng làm giá khớp.
    """
    return Decimal(str(session["closeRaw"]))


def adjusted_closes(rows: list[dict[str, Any]]) -> tuple[Decimal, ...]:
    """Chuỗi giá đóng cửa ĐÃ ĐIỀU CHỈNH, dùng để tính lợi nhuận và alpha."""
    return tuple(Decimal(str(row["close"])) for row in rows)
```

- [ ] **Step 4: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_vn_data.py -v`
Expected: 2 passed

- [ ] **Step 5: Viết test cho bất biến 2 — kiểm chéo hai endpoint**

```python
def test_cross_source_check_passes_when_both_ssi_services_agree():
    from crypto_desk.vn_data import assert_sources_agree

    assert_sources_agree(Decimal("71700"), Decimal("71700"))


def test_cross_source_check_catches_comparing_against_the_adjusted_price():
    """Nhầm close (65182.47) thay cho closeRaw (71700) lệch ~9%, guard phải kêu."""
    from crypto_desk.vn_data import assert_sources_agree

    with pytest.raises(EvidenceError, match="lệch"):
        assert_sources_agree(Decimal("71700"), Decimal("65182.47"))


def test_cross_source_check_tolerates_half_a_percent():
    from crypto_desk.vn_data import assert_sources_agree

    assert_sources_agree(Decimal("1000"), Decimal("1004"))
    with pytest.raises(EvidenceError):
        assert_sources_agree(Decimal("1000"), Decimal("1006"))
```

- [ ] **Step 6: Chạy test (FAIL), rồi viết `assert_sources_agree`**

Run: `uv run pytest tests/test_vn_data.py -k cross_source -v` → FAIL với `ImportError`.

```python
def assert_sources_agree(matched_price: Decimal, close_raw: Decimal) -> None:
    """Hai service SSI phải báo cùng một giá khớp.

    Cùng nhà cung cấp nên yếu hơn cross-vendor: SSI sai đồng bộ cả hai thì
    không phát hiện được. Bù lại nó bắt được đúng cái bẫy giá điều chỉnh, vì
    nhầm ``close`` thay ``closeRaw`` lệch tới ~9% trên một mã vừa có sự kiện quyền.
    """
    if matched_price <= 0 or close_raw <= 0:
        raise EvidenceError("Giá khớp phải dương")
    deviation = abs(matched_price - close_raw) / close_raw
    if deviation > MAX_SOURCE_DEVIATION:
        raise EvidenceError(
            f"Hai nguồn SSI lệch {deviation:.4%}, vượt ngưỡng {MAX_SOURCE_DEVIATION:.2%}"
        )
```

Chạy lại: 3 passed.

- [ ] **Step 7: Viết test cho bất biến 3 — lịch giao dịch**

```python
def test_friday_session_is_fresh_when_the_cutoff_falls_on_saturday():
    """Bất biến 3: quy tắc theo đồng hồ sẽ báo stale mọi cuối tuần."""
    from datetime import datetime, timezone

    from crypto_desk.vn_data import assert_latest_session_closed

    assert_latest_session_closed(
        latest_session_date="18/09/2026",          # thứ Sáu
        cutoff=datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc),
    )


def test_a_session_more_recent_than_the_cutoff_is_rejected():
    from datetime import datetime, timezone

    from crypto_desk.vn_data import assert_latest_session_closed

    with pytest.raises(EvidenceError):
        assert_latest_session_closed(
            latest_session_date="21/09/2026",
            cutoff=datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc),
        )
```

- [ ] **Step 8: Chạy test (FAIL), rồi viết `assert_latest_session_closed`**

Hàm parse `DD/MM/YYYY`, so ngày phiên với ngày của `cutoff`, và **chỉ** từ chối khi ngày phiên **sau** ngày cutoff. Không đặt ngưỡng tuổi tối đa theo giờ: cuối tuần và lễ tết khiến phiên gần nhất có thể cách cutoff nhiều ngày mà vẫn là phiên hợp lệ gần nhất.

Chạy lại: 2 passed.

- [ ] **Step 9: Viết `SSIClient`, `VNEvidenceSnapshot`, `VNEvidenceBuilder`**

`SSIClient` theo đúng khuôn `PublicDataClient` trong `data.py`: `httpx.Client` với `httpx.Timeout(30.0, connect=10.0, read=30.0)`, một `_get` có retry một lần cho lỗi transport và cho 429/5xx, dựng `Fetched(provider, source, fetched_at, as_of, payload)`.

Bốn phương thức tương ứng bốn endpoint ở bảng đầu plan. `charts_history` nhận `from`/`to` là epoch giây.

`VNEvidenceSnapshot`:

```python
@dataclass(frozen=True, slots=True)
class VNEvidenceSnapshot:
    symbol: str
    cutoff: str
    items: tuple[EvidenceItem, ...]
    mid: Decimal
    industry: str

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)
```

`build(symbol, cutoff)` dựng bốn evidence item **bắt buộc** — `spot`, `news`, `flow`, `reference` — theo đúng thứ tự: lấy `stock_info` phiên gần nhất, `assert_latest_session_closed`, `charts_history` cho `daily_closes` (giá điều chỉnh), `board_snapshot` lấy `matchedPrice`, `assert_sources_agree(matchedPrice, closeRaw)`, RSS tin tức qua `_parse_feed` sẵn có trong `data.py`, và `company_profile` cho `industry`.

Item `fundamentals` **không** dựng ở đây — Task 4.

`reflection_closes(symbol, start, periods=20)` gọi `charts_history` và trả **20 phiên giao dịch đã đóng** (bất biến 4 — đếm phiên, không đếm ngày lịch), raise `EvidenceError` nếu không đủ.

- [ ] **Step 10: Chạy toàn bộ test, lint, commit**

```bash
uv run pytest && uv run ruff check src tests
git add src/crypto_desk/vn_data.py tests/test_vn_data.py
git commit -m "feat: build HOSE market evidence from SSI iboard

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Báo cáo tài chính — script cách ly và bộ lọc theo ngành

**Files:**
- Create: `scripts/fetch_fundamentals.py`
- Create: `src/crypto_desk/vn_fundamentals.py`
- Test: `tests/test_vn_fundamentals.py`

**Interfaces:**
- Consumes: `EvidenceItem`.
- Produces:
  - `load_fundamentals(symbol: str, industry: str, cache_dir: Path) -> EvidenceItem | None`
  - `BANK_METRICS`, `NONBANK_METRICS`, `BANK_INDUSTRIES`, `NONBANK_INDUSTRIES` — đều là `frozenset[str]` khai trong `vn_fundamentals.py` định nghĩa bộ chỉ tiêu và danh sách ngành để lọc báo cáo tài chính.

- [ ] **Step 1: Viết test cho bất biến 9 — test quan trọng nhất của task này**

Tạo `tests/test_vn_fundamentals.py`:

```python
from __future__ import annotations

import json

BANK_ONLY = ("Nợ xấu (%)", "LDR (%)", "CAR", "Tỷ lệ CASA", "Tăng trưởng cho vay (%)")
NONBANK_ONLY = ("Số ngày tồn kho", "Biên LN gộp (%)", "Chu kỳ tiền")


def _write_cache(tmp_path, symbol: str):
    """vnstock trả cùng một schema 54 chỉ tiêu cho mọi doanh nghiệp, ô không áp
    dụng bằng 0.0 chứ không phải null."""
    payload = {
        "fetched_at": "2026-09-20T00:00:00+00:00",
        "ratio": {name: "0.0" for name in BANK_ONLY + NONBANK_ONLY}
        | {"P/E": "18.5", "ROE (%)": "22.1"},
        "income_statement": {"Doanh thu thuần": "1000"},
        "balance_sheet": {"Tổng tài sản": "5000"},
    }
    path = tmp_path / f"{symbol}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_bank_metrics_are_absent_from_a_software_company_not_zero(tmp_path):
    """FPT không cho ai vay. 'Nợ xấu 0.0%' đọc như tín dụng hoàn hảo."""
    from crypto_desk.vn_fundamentals import load_fundamentals

    cache = _write_cache(tmp_path, "FPT")
    item = load_fundamentals("FPT", "Công nghệ Thông tin", cache)

    assert item is not None
    ratio = item.payload["ratio"]
    for name in BANK_ONLY:
        assert name not in ratio, f"{name} phải bị loại khỏi payload của FPT, không phải bằng 0"
    assert "Biên LN gộp (%)" in ratio
    assert ratio["P/E"] == "18.5"


def test_inventory_metrics_are_absent_from_a_bank(tmp_path):
    from crypto_desk.vn_fundamentals import load_fundamentals

    cache = _write_cache(tmp_path, "MBB")
    item = load_fundamentals("MBB", "Ngân hàng", cache)

    assert item is not None
    ratio = item.payload["ratio"]
    for name in NONBANK_ONLY:
        assert name not in ratio, f"{name} phải bị loại khỏi payload của MBB"
    assert "Nợ xấu (%)" in ratio


def test_missing_cache_returns_none_rather_than_raising(tmp_path):
    from crypto_desk.vn_fundamentals import load_fundamentals

    assert load_fundamentals("FPT", "Công nghệ Thông tin", tmp_path) is None


def test_corrupt_cache_returns_none_rather_than_raising(tmp_path):
    from crypto_desk.vn_fundamentals import load_fundamentals

    (tmp_path / "FPT.json").write_text("{không phải json", encoding="utf-8")

    assert load_fundamentals("FPT", "Công nghệ Thông tin", tmp_path) is None


def test_unknown_industry_yields_no_evidence_rather_than_an_unfiltered_table(tmp_path):
    """Không biết ngành thì không lọc được; đưa cả bảng ra là đúng thứ bất biến 9 cấm."""
    from crypto_desk.vn_fundamentals import load_fundamentals

    cache = _write_cache(tmp_path, "FPT")

    assert load_fundamentals("FPT", "Ngành Lạ", cache) is None
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_vn_fundamentals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crypto_desk.vn_fundamentals'`

- [ ] **Step 3: Viết `vn_fundamentals.py`**

```python
"""Báo cáo tài chính cho cổ phiếu VN, đọc từ cache do một process khác ghi.

``vnstock`` không phải dependency của desk. Nó kéo theo 41 package và một
package telemetry bật mặc định, nên nó chạy ở venv riêng qua
``scripts/fetch_fundamentals.py`` và không bao giờ nhìn thấy ``.env`` của
desk. Module này chỉ đọc file mà script đó ghi ra.

Bộ lọc theo ngành nằm ở đây chứ không nằm trong script, vì nó là một tính
chất an toàn: bảng ``ratio()`` của vnstock là hợp phẳng của chỉ tiêu ngân
hàng và phi ngân hàng, và ô không áp dụng **bằng 0.0 chứ không phải null**.
FPT — một công ty phần mềm — báo ``Nợ xấu (%) = 0.0``. Đưa nguyên bảng cho
model thì nó kết luận FPT có chất lượng tín dụng hoàn hảo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import EvidenceItem

BANK_METRICS = frozenset({
    "Nợ xấu (%)", "LDR (%)", "CAR", "Tỷ lệ CASA",
    "Tăng trưởng cho vay (%)", "Tăng trưởng tiền gửi (%)",
    "Biên lãi thuần", "Tỷ lệ CIR", "CIR",
    "Lãi suất bình quân tài sản sinh lãi", "Chi phí vốn bình quân",
    "Thu nhập ngoài lãi", "Vốn chủ/Cho vay",
    "DP rủi ro/Nợ xấu", "DP rủi ro/Cho vay", "Trích lập DP/Cho vay",
})

NONBANK_METRICS = frozenset({
    "Số ngày tồn kho", "Số ngày phải thu", "Số ngày phải trả",
    "Biên LN gộp (%)", "Chu kỳ tiền", "Vòng quay TS cố định",
    "Hệ số thanh toán nhanh", "Hệ số thanh toán hiện hành", "Hệ số thanh toán tiền",
    "EV/EBITDA", "EBIT", "EBITDA", "Biên EBIT (%)",
})

BANK_INDUSTRIES = frozenset({"Ngân hàng"})
NONBANK_INDUSTRIES = frozenset({
    "Công nghệ Thông tin", "Bán lẻ", "Thực phẩm và đồ uống",
    "Tài nguyên Cơ bản", "Bất động sản", "Xây dựng và Vật liệu",
    "Hàng cá nhân & Gia dụng", "Điện, nước & xăng dầu khí đốt",
    "Hóa chất", "Dịch vụ tài chính", "Ô tô và phụ tùng",
    "Du lịch và Giải trí", "Hàng & Dịch vụ Công nghiệp", "Y tế",
    "Viễn thông", "Dầu khí", "Truyền thông", "Bảo hiểm",
})


def _drop(ratio: dict[str, Any], names: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in ratio.items() if k not in names}


def load_fundamentals(
    symbol: str,
    industry: str,
    cache_dir: Path,
) -> EvidenceItem | None:
    """Evidence cơ bản cho ``symbol``, hoặc ``None`` khi không dựng được.

    Trả ``None`` thay vì ném ở mọi đường hỏng: thiếu file, JSON sai cú pháp,
    ngành không nhận ra. Chuyên gia cơ bản khi đó bị bỏ qua và run vẫn chạy —
    một thư viện bên thứ ba không được quyền quyết định desk có chạy hay không.
    """
    path = Path(cache_dir) / f"{symbol}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    ratio = raw.get("ratio")
    if not isinstance(ratio, dict):
        return None

    if industry in BANK_INDUSTRIES:
        ratio = _drop(ratio, NONBANK_METRICS)
    elif industry in NONBANK_INDUSTRIES:
        ratio = _drop(ratio, BANK_METRICS)
    else:
        # Không biết ngành thì không lọc được, và đưa cả bảng ra là đúng thứ
        # bất biến 9 cấm. Thà không có evidence cơ bản.
        return None

    return EvidenceItem.create(
        kind="fundamentals",
        provider="vnstock",
        source=str(path),
        fetched_at=str(raw.get("fetched_at", "")),
        as_of=str(raw.get("fetched_at", "")),
        delayed=True,
        stale=False,
        payload={
            "symbol": symbol,
            "industry": industry,
            "fetched_at": raw.get("fetched_at"),
            "ratio": ratio,
            "income_statement": raw.get("income_statement"),
            "balance_sheet": raw.get("balance_sheet"),
        },
    )
```

- [ ] **Step 4: Chạy test, xác nhận pass**

Run: `uv run pytest tests/test_vn_fundamentals.py -v`
Expected: 5 passed

- [ ] **Step 5: Viết script cách ly**

Tạo `scripts/fetch_fundamentals.py`. Ràng buộc bắt buộc, được test ở Step 6:

- **Không** `import crypto_desk` bất cứ thứ gì
- **Không** `load_dotenv`, không đọc `.env`
- Đặt `os.environ["VNSTOCK_TELEMETRY"] = "0"` **trước** khi import vnstock
- Nhận symbol từ `sys.argv`, thư mục ra từ `--out`
- Ghi `{"fetched_at": <iso>, "ratio": {...}, "income_statement": {...}, "balance_sheet": {...}}`
- Dùng `vnstock.api.financial.Finance(symbol=..., source="VCI")`, **không** dùng lớp `Vnstock()` đã deprecated

Kèm docstring nêu cách dựng venv:

```
python3 -m venv .venv-fundamentals
.venv-fundamentals/bin/pip install vnstock
.venv-fundamentals/bin/python scripts/fetch_fundamentals.py FPT MBB --out data/fundamentals
```

- [ ] **Step 6: Test giữ tính cách ly**

```python
def test_the_fetch_script_never_touches_the_desk_environment():
    """Tính cách ly dễ bị xói mòn ở lần sửa sau; test này rẻ và giữ nó lại."""
    from pathlib import Path

    source = Path("scripts/fetch_fundamentals.py").read_text(encoding="utf-8")

    assert "load_dotenv" not in source
    assert "import crypto_desk" not in source
    assert "from crypto_desk" not in source
    assert "VNSTOCK_TELEMETRY" in source
```

Run: `uv run pytest tests/test_vn_fundamentals.py -v` → 6 passed.

- [ ] **Step 7: Chạy script thật một lần**

```bash
python3 -m venv .venv-fundamentals
.venv-fundamentals/bin/pip install -q vnstock
.venv-fundamentals/bin/python scripts/fetch_fundamentals.py FPT MBB --out data/fundamentals
```

Kiểm `data/fundamentals/FPT.json` có `ratio` chứa `"Nợ xấu (%)"`, và sau khi qua `load_fundamentals("FPT", "Công nghệ Thông tin", ...)` thì khoá đó **biến mất**. Dán kết quả vào báo cáo.

Thêm `.venv-fundamentals/` và `data/fundamentals/` vào `.gitignore`.

- [ ] **Step 8: Chạy toàn bộ test, lint, commit**

```bash
uv run pytest && uv run ruff check src tests
git add src/crypto_desk/vn_fundamentals.py scripts/fetch_fundamentals.py \
        tests/test_vn_fundamentals.py .gitignore
git commit -m "feat: read VN financials from an isolated fetcher, filtered by industry

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `vn_prompts.py` — năm chuyên gia bản VN

**Files:**
- Create: `src/crypto_desk/vn_prompts.py`
- Test: `tests/test_vn_service.py` (phần prompt)

**Interfaces:**
- Consumes: không (thuần dữ liệu).
- Produces: `VN_SPECIALISTS`, `VN_ROLE_PROMPTS`, `VN_SPECIALIST_EVIDENCE`, `VN_OPTIONAL_KINDS`.

- [ ] **Step 1: Viết test cho bất biến 8**

Tạo `tests/test_vn_service.py`:

```python
from __future__ import annotations


def test_the_vn_manager_is_never_offered_reduce_or_exit():
    """Bất biến 8: không có nguồn vị thế cho cổ phiếu, nên REDUCE/EXIT luôn bị
    _validate_manager từ chối và biến một quyết định thật thành lỗi kỹ thuật."""
    from crypto_desk.vn_prompts import VN_ROLE_PROMPTS

    manager = VN_ROLE_PROMPTS["manager"]

    assert "ACCUMULATE" in manager
    assert "HOLD" in manager
    assert "NO_TRADE" in manager
    assert "REDUCE" not in manager
    assert "EXIT" not in manager


def test_every_vn_specialist_has_a_prompt_and_an_evidence_mapping():
    from crypto_desk.vn_prompts import (
        VN_ROLE_PROMPTS,
        VN_SPECIALISTS,
        VN_SPECIALIST_EVIDENCE,
    )

    assert len(VN_SPECIALISTS) == 5
    for role in VN_SPECIALISTS:
        assert role in VN_ROLE_PROMPTS
        assert role in VN_SPECIALIST_EVIDENCE
    for role in ("bull", "bear", "manager"):
        assert role in VN_ROLE_PROMPTS


def test_fundamentals_is_the_only_optional_kind():
    from crypto_desk.vn_prompts import VN_OPTIONAL_KINDS, VN_SPECIALIST_EVIDENCE

    kinds = {kind for kind, _ in VN_SPECIALIST_EVIDENCE.values()}

    assert kinds == {"spot", "news", "flow", "fundamentals"}
    assert VN_OPTIONAL_KINDS == frozenset({"fundamentals"})
```

- [ ] **Step 2: Chạy test (FAIL), rồi viết `vn_prompts.py`**

```python
VN_SPECIALISTS = ("technical", "liquidity", "news", "flow", "fundamentals")

VN_SPECIALIST_EVIDENCE = {
    "technical": ("spot", ("symbol", "mid", "daily_closes", "ref_price")),
    "liquidity": ("spot", ("symbol", "mid", "total_match_vol", "total_match_val",
                           "avg_price", "buy_trades", "sell_trades",
                           "ceiling_price", "floor_price")),
    "news": ("news", ("symbol", "items")),
    "flow": ("flow", ("symbol", "foreign_buy_vol", "foreign_sell_vol",
                      "foreign_room", "net_buy_sell_vol")),
    "fundamentals": ("fundamentals", ("symbol", "industry", "fetched_at",
                                      "ratio", "income_statement", "balance_sheet")),
}

VN_OPTIONAL_KINDS = frozenset({"fundamentals"})
```

Prompt từng vai, tiếng Việt có dấu, theo đúng giọng của `ROLE_PROMPTS` bên crypto. Điểm bắt buộc trong từng prompt:

- **technical** — chỉ giá đóng cửa đã điều chỉnh và giá khớp thô; nêu rõ khi tín hiệu yếu.
- **liquidity** — khối lượng khớp, giá bình quân, số lệnh mua/bán, khoảng cách tới trần/sàn. **Không có sổ lệnh**; nói rõ khi không đủ dữ liệu ước lượng.
- **news** — chỉ tiêu đề, thời điểm đăng và nguồn; không suy đoán nội dung bài.
- **flow** — khối ngoại mua/bán ròng, room còn lại, mất cân đối lệnh. Đây là tín hiệu định vị, không phải tín hiệu giá.
- **fundamentals** — chỉ đọc chỉ tiêu có trong payload. Prompt phải nói rõ: *"Payload đã lọc theo loại hình doanh nghiệp. Chỉ tiêu không có mặt nghĩa là không áp dụng cho doanh nghiệp này, không phải bằng không."* Và: *"Số liệu tài chính lấy từ cache ngày `fetched_at`, không phải thời điểm chạy."*
- **manager** — chỉ ba action `ACCUMULATE`, `HOLD`, `NO_TRADE`; không nhắc `futures_setups`; nêu `thesis_continuity` như bản crypto.

- [ ] **Step 3: Chạy test, lint, commit**

```bash
uv run pytest tests/test_vn_service.py -v && uv run ruff check src tests
git add src/crypto_desk/vn_prompts.py tests/test_vn_service.py
git commit -m "feat: write the VN panel's prompts

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `vn_service.py` và CLI

**Files:**
- Create: `src/crypto_desk/vn_service.py`
- Modify: `src/crypto_desk/cli.py`
- Test: `tests/test_vn_service.py`

**Interfaces:**
- Consumes: mọi thứ từ Task 1–5, cộng `is_decided` và `Store.unreflected_runs(completed_before, symbols)` từ plan điều kiện tiên quyết.
- Produces: `VNDeskService.analyze(symbol, cutoff=None)`, `VNDeskService.refresh_reflections(cutoff)`, lệnh `desk vn-analyze`, `desk vn-daily`.

- [ ] **Step 1: Viết test cho VNDeskService**

Bổ sung imports, helpers (`make_vn_snapshot`, `make_vn_decision` với `decided=True`), và test cases vào `tests/test_vn_service.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from crypto_desk.committee import CommitteeResult
from crypto_desk.config import VN_BENCHMARK_SYMBOL, Settings
from crypto_desk.domain import EvidenceItem, ResearchDecision
from crypto_desk.store import Store
from crypto_desk.vn_data import VNEvidenceSnapshot
from crypto_desk.vn_service import VNDeskService

NOW = datetime(2026, 9, 20, 0, 15, tzinfo=UTC)


def make_vn_snapshot(symbol: str = "FPT") -> VNEvidenceSnapshot:
    items = tuple(
        EvidenceItem.create(
            kind=kind,
            provider="fixture",
            source=f"fixture://{kind}",
            fetched_at=NOW.isoformat(),
            as_of=NOW.isoformat(),
            delayed=False,
            stale=False,
            payload={"symbol": symbol, "value": value},
        )
        for kind, value in (
            ("spot", "100"),
            ("news", "current"),
            ("flow", "100"),
            ("reference", "100"),
        )
    )
    return VNEvidenceSnapshot(
        symbol=symbol,
        cutoff=NOW.isoformat(),
        items=items,
        mid=Decimal("100"),
        industry="Công nghệ Thông tin",
    )


def make_vn_decision(symbol: str = "FPT", action: str = "HOLD") -> ResearchDecision:
    return ResearchDecision(
        symbol=symbol,
        action=action,
        conviction=Decimal("5"),
        bull_case="Bull",
        bear_case="Bear",
        catalysts=(),
        invalidation="Invalidation",
        entry=None,
        stop=None,
        target=None,
        evidence_ids=("fixture-1",),
        reason="Quyết định của hội đồng.",
        decided=True,
    )


def test_a_missing_fundamentals_cache_still_produces_a_run_and_says_so(tmp_path):
    """Bất biến 10: vnstock vắng mặt không được làm chết run, nhưng phải nói ra."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "artifacts",
        fundamentals_dir=tmp_path / "no-such-dir",
    )
    store = Store(settings.database)

    class _Builder:
        def build(self, symbol, cutoff=None):
            return make_vn_snapshot(symbol)

    class _Committee:
        def run(self, snapshot, **kwargs):
            return CommitteeResult(
                decision=make_vn_decision(snapshot.symbol, "HOLD"),
                reports={},
                calls=(),
                skipped_specialists=("fundamentals",),
            )

    service = VNDeskService(
        settings, store, evidence_builder=_Builder(), committee=_Committee()
    )
    try:
        run = service.analyze("FPT")
        report = (run.report_dir / "report.md").read_text(encoding="utf-8")
    finally:
        store.close()

    assert run.decision.action == "HOLD"
    assert "KHÔNG đọc được báo cáo tài chính" in report
    assert report.splitlines()[0].startswith(">")


def test_committee_runs_with_missing_optional_fundamentals():
    """Bất biến 10: CryptoCommittee thật chạy bình thường khi thiếu fundamentals."""
    from crypto_desk.committee import CryptoCommittee
    from crypto_desk.vn_prompts import (
        VN_OPTIONAL_KINDS,
        VN_ROLE_PROMPTS,
        VN_SPECIALIST_EVIDENCE,
        VN_SPECIALISTS,
    )

    class _MockModel:
        def generate(self, **kwargs):
            return (
                '{"action": "HOLD", "conviction": "5", "bull_case": "ok", '
                '"bear_case": "ok", "catalysts": [], "invalidation": "none", '
                '"entry": null, "stop": null, "target": null, '
                '"futures_bias": "NEUTRAL", "futures_setups": []}'
            )

    committee = CryptoCommittee(
        _MockModel(),
        specialists=VN_SPECIALISTS,
        role_prompts=VN_ROLE_PROMPTS,
        specialist_evidence=VN_SPECIALIST_EVIDENCE,
        optional_kinds=VN_OPTIONAL_KINDS,
        mid_label="giá khớp SSI",
    )
    snapshot = make_vn_snapshot("FPT")  # 4 items bắt buộc, KHÔNG có fundamentals
    result = committee.run(snapshot)

    assert result.decision.action == "HOLD"
    assert "fundamentals" in result.skipped_specialists


def test_vn_reflections_are_benchmarked_against_vn30_not_btc(tmp_path):
    settings = Settings(database=tmp_path / "vn.sqlite3", artifacts=tmp_path / "a")
    store = Store(settings.database)
    seen: dict[str, str] = {}

    class _Builder:
        def reflection_closes(self, symbol, start, periods=20):
            seen.setdefault("benchmark_asked", symbol)
            return tuple(Decimal("100") for _ in range(20))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    payload = service.save_reflection(
        run_id="r1",
        symbol="FPT",
        entry=Decimal("100"),
        closes=tuple(Decimal("110") for _ in range(20)),
        benchmark_closes=tuple(Decimal("100") for _ in range(20)),
        benchmark_symbol=VN_BENCHMARK_SYMBOL,
    )
    store.close()

    assert VN_BENCHMARK_SYMBOL == "VN30"
    assert payload["benchmark_symbol"] == "VN30"


def test_vn_reflection_sweep_never_picks_up_crypto_runs(tmp_path):
    """Bảng research_runs dùng chung; quét không lọc sẽ vớ phải run crypto."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        symbols=("BTCUSDT",),
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)
    swept: list[str] = []

    for run_id, symbol in (("r-btc", "BTCUSDT"), ("r-fpt", "FPT")):
        report_dir = tmp_path / run_id
        report_dir.mkdir()
        (report_dir / "evidence.json").write_text(
            json.dumps({"closeRaw": "100", "binance_mid": "100"}), encoding="utf-8"
        )
        store.save_run(
            run_id,
            (NOW - timedelta(days=40)).isoformat(),
            make_vn_decision(symbol, "HOLD"),
            report_dir,
        )

    class _Builder:
        def reflection_closes(self, symbol, start, periods=20):
            swept.append(symbol)
            return tuple(Decimal("100") for _ in range(20))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    try:
        saved = service.refresh_reflections(NOW)
    finally:
        store.close()

    assert saved == ["r-fpt"]
    assert "BTCUSDT" not in swept
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `uv run pytest tests/test_vn_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crypto_desk.vn_service'`

- [ ] **Step 3: Viết `VNDeskService`**

`analyze(symbol, cutoff=None)`:
1. `VNEvidenceBuilder.build(symbol, cutoff)` — lỗi thì `_no_trade` với `decided=False`
2. `fund_item = load_fundamentals(symbol, snapshot.industry, settings.fundamentals_dir)` — nếu có thì `snapshot = dataclasses.replace(snapshot, items=(*snapshot.items, fund_item))` (do `VNEvidenceSnapshot` là frozen dataclass)
3. `CryptoCommittee` được khởi tạo với `specialists=VN_SPECIALISTS`, `role_prompts=VN_ROLE_PROMPTS`, `specialist_evidence=VN_SPECIALIST_EVIDENCE`, `optional_kinds=VN_OPTIONAL_KINDS`, `mid_label="giá khớp SSI"`
4. Gọi `committee.run(snapshot, ...)`
5. `Store.save_run` + artifacts như `CryptoDeskService.analyze`
6. `report.md`: khi `skipped_specialists` không rỗng, **dòng đầu tiên** là cảnh báo, ví dụ `> Phân tích này KHÔNG đọc được báo cáo tài chính (thiếu: cơ bản).` Cũng ghi một dòng nêu giới hạn kiểm chéo cùng nhà cung cấp (bất biến 2).

`refresh_reflections(cutoff)` giống bản crypto nhưng: `unreflected_runs(completed_before, tuple(settings.vn_symbols))`, entry lấy từ `closeRaw` trong evidence.json, benchmark `VN_BENCHMARK_SYMBOL`, và truyền `benchmark_symbol=VN_BENCHMARK_SYMBOL` vào `save_reflection`.

- [ ] **Step 4: Thêm lệnh CLI**

```python
@app.command("vn-analyze")
def vn_analyze(ctx: typer.Context, symbol: str) -> None:
    settings = _load(ctx)
    normalized = symbol.upper()
    if normalized not in settings.vn_symbols:
        _fail("Symbol is outside the configured VN allowlist")
    _emit(ctx, _vn_service(settings).analyze(normalized))


@app.command("vn-daily")
def vn_daily(ctx: typer.Context) -> None:
    settings = _load(ctx)
    _emit(ctx, _vn_service(settings).daily())
```

`vn-daily` chạy `refresh_reflections` rồi phân tích **toàn bộ** `vn_symbols` không lọc (V1 không có screener), và `mark_scheduled("vn_daily", bucket)` — bucket khác `"daily"` của crypto.

`_vn_service(settings)` theo khuôn `_service(settings)` sẵn có.

- [ ] **Step 5: Chạy test, lint**

Run: `uv run pytest && uv run ruff check src tests`

- [ ] **Step 6: Chạy thật**

```bash
uv run desk vn-analyze FPT
uv run desk vn-analyze MBB
```

Kiểm trong báo cáo: quyết định thuộc `{ACCUMULATE, HOLD, NO_TRADE}`, có dòng giới hạn kiểm chéo, và nếu cache cơ bản vắng thì có dòng cảnh báo ở đầu. Dán cả hai output vào báo cáo.

- [ ] **Step 7: Commit**

```bash
git add src/crypto_desk/vn_service.py src/crypto_desk/cli.py tests/test_vn_service.py
git commit -m "feat: analyse HOSE equities through the shared committee

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Sau plan này

- `desk doctor` chưa báo tuổi cache `data/fundamentals/`. Nhỏ, làm sau.
- Chạy `scripts/fetch_fundamentals.py` vẫn là thao tác tay hoặc cron riêng. Cho `vn-daily` gọi nó qua subprocess sẽ kéo môi trường của desk sang process con và phá đúng tính cách ly vừa dựng — nếu muốn tự động, phải scrub môi trường một cách tường minh, và đó là việc riêng.
- Universe vẫn là FPT và MBB. Mở rộng là quyết định riêng, theo thanh khoản và độ phủ dữ liệu, không theo kỳ vọng giá.
