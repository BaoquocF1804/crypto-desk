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
import re
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

    # Check staleness: compare report year to fetched_at year
    report_year = None
    if "Năm" in ratio:
        try:
            report_year = int(ratio["Năm"])
        except (ValueError, TypeError):
            pass
    elif "period" in ratio:
        m = re.search(r"(\d{4})", str(ratio["period"]))
        if m:
            report_year = int(m.group(1))

    fetched_at_str = str(raw.get("fetched_at", ""))
    fetched_year = None
    m_fetched = re.search(r"(\d{4})", fetched_at_str)
    if m_fetched:
        fetched_year = int(m_fetched.group(1))

    stale = False
    if report_year and fetched_year:
        if fetched_year - report_year > 1:
            stale = True

    return EvidenceItem.create(
        kind="fundamentals",
        provider="vnstock",
        source=str(path),
        fetched_at=fetched_at_str,
        as_of=fetched_at_str,
        delayed=True,
        stale=stale,
        payload={
            "symbol": symbol,
            "industry": industry,
            "fetched_at": raw.get("fetched_at"),
            "ratio": ratio,
            "income_statement": raw.get("income_statement"),
            "balance_sheet": raw.get("balance_sheet"),
        },
    )
