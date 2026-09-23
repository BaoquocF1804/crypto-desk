"""Script độc lập lấy báo cáo tài chính từ vnstock, chạy trong venv riêng.

CÁCH DÙNG:
    python3 -m venv .venv-fundamentals
    .venv-fundamentals/bin/pip install vnstock
    .venv-fundamentals/bin/python scripts/fetch_fundamentals.py FPT MBB --out data/fundamentals

RÀNG BUỘC CÁCH LY:
    - Không import package chính của desk
    - Không nạp biến môi trường hay đọc file .env
    - Tắt telemetry vnstock trước khi import
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

# Bắt buộc: Tắt telemetry của vnstock trước khi import bất kỳ package con nào
os.environ["VNSTOCK_TELEMETRY"] = "0"


def _parse_period_col(col_name: str) -> tuple[int, int]:
    """Parse a period column name into (year, quarter) for chronological sorting.
    Examples:
        '2026-Q2' -> (2026, 2)
        '2025-Q4_1' -> (2025, 4)
        '2025' -> (2025, 0)
    Returns (0, 0) if not a recognizable period column.
    """
    col_str = str(col_name).strip()
    match = re.search(r"(\d{4})(?:[-_]Q(\d))?", col_str, re.IGNORECASE)
    if match:
        year = int(match.group(1))
        quarter = int(match.group(2)) if match.group(2) else 0
        return (year, quarter)
    return (0, 0)


def _extract_latest_metrics(df_or_dict: object) -> dict[str, str]:
    if df_or_dict is None:
        return {}
    try:
        import pandas as pd

        if isinstance(df_or_dict, pd.DataFrame):
            if df_or_dict.empty:
                return {}
            res: dict[str, str] = {}
            if "item" in df_or_dict.columns or "Chỉ tiêu" in df_or_dict.columns:
                key_col = "item" if "item" in df_or_dict.columns else "Chỉ tiêu"
                metadata_cols = {"item", "item_en", "item_id", "Chỉ tiêu", "STT"}
                val_cols = [c for c in df_or_dict.columns if c != key_col and c not in metadata_cols]
                period_cols = [c for c in val_cols if _parse_period_col(c) > (0, 0)]
                if period_cols:
                    latest_col = max(period_cols, key=_parse_period_col)
                elif val_cols:
                    latest_col = val_cols[0]
                else:
                    latest_col = None

                if latest_col is not None:
                    parsed_year, parsed_q = _parse_period_col(latest_col)
                    for _, row in df_or_dict.iterrows():
                        res[str(row[key_col])] = str(row[latest_col])
                    if parsed_year > 0 and "Năm" not in res:
                        res["Năm"] = str(parsed_year)
                    if parsed_q > 0 and "Quý" not in res:
                        res["Quý"] = str(parsed_q)
                    res["period"] = str(latest_col)
            else:
                cols = list(df_or_dict.columns)
                period_cols = [c for c in cols if _parse_period_col(c) > (0, 0)]
                latest_col = max(period_cols, key=_parse_period_col) if period_cols else (cols[-1] if cols else None)
                if latest_col is not None:
                    for idx, val in df_or_dict[latest_col].items():
                        res[str(idx)] = str(val)
                    parsed_year, parsed_q = _parse_period_col(latest_col)
                    if parsed_year > 0 and "Năm" not in res:
                        res["Năm"] = str(parsed_year)
                    if parsed_q > 0 and "Quý" not in res:
                        res["Quý"] = str(parsed_q)
                    res["period"] = str(latest_col)
                else:
                    for col in cols:
                        res[str(col)] = str(df_or_dict[col].iloc[0])
            return res
    except ImportError:
        pass
    if isinstance(df_or_dict, dict):
        return {str(k): str(v) for k, v in df_or_dict.items()}
    return {}


def fetch_symbol_data(symbol: str) -> dict[str, object]:
    from vnstock.api.financial import Finance

    now_iso = datetime.now(timezone.utc).isoformat()
    ratio_df = None
    inc_df = None
    bal_df = None

    for source in ("KBS", "VCI"):
        try:
            fin = Finance(symbol=symbol, source=source)
            if ratio_df is None:
                try:
                    df = fin.ratio(period="quarter", lang="vi")
                    if df is not None and not df.empty:
                        ratio_df = df
                except Exception:
                    pass
            if inc_df is None:
                try:
                    df = fin.income_statement(period="quarter", lang="vi")
                    if df is not None and not df.empty:
                        inc_df = df
                except Exception:
                    pass
            if bal_df is None:
                try:
                    df = fin.balance_sheet(period="quarter", lang="vi")
                    if df is not None and not df.empty:
                        bal_df = df
                except Exception:
                    pass
        except Exception:
            continue

    return {
        "symbol": symbol,
        "fetched_at": now_iso,
        "ratio": _extract_latest_metrics(ratio_df),
        "income_statement": _extract_latest_metrics(inc_df),
        "balance_sheet": _extract_latest_metrics(bal_df),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch VN financial data independently")
    parser.add_argument("symbols", nargs="+", help="Symbols to fetch, e.g. FPT MBB")
    parser.add_argument("--out", default="data/fundamentals", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for symbol in args.symbols:
        sym = symbol.upper()
        print(f"Fetching {sym}...", flush=True)
        try:
            data = fetch_symbol_data(sym)
            out_file = out_dir / f"{sym}.json"
            out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved {sym} to {out_file}", flush=True)
        except Exception as exc:
            print(f"Error fetching {sym}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
