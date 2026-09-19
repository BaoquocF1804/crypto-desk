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


def test_the_fetch_script_never_touches_the_desk_environment():
    """Tính cách ly dễ bị xói mòn ở lần sửa sau; test này rẻ và giữ nó lại."""
    from pathlib import Path

    source = Path("scripts/fetch_fundamentals.py").read_text(encoding="utf-8")

    assert "load_dotenv" not in source
    assert "import crypto_desk" not in source
    assert "from crypto_desk" not in source
    assert "VNSTOCK_TELEMETRY" in source

