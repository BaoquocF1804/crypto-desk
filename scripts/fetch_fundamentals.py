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
import sys

# Bắt buộc: Tắt telemetry của vnstock trước khi import bất kỳ package con nào
os.environ["VNSTOCK_TELEMETRY"] = "0"


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
                val_cols = [c for c in df_or_dict.columns if c != key_col]
                latest_col = val_cols[-1] if val_cols else None
                if latest_col is not None:
                    for _, row in df_or_dict.iterrows():
                        res[str(row[key_col])] = str(row[latest_col])
            else:
                cols = list(df_or_dict.columns)
                latest_col = cols[-1] if cols else None
                if latest_col is not None:
                    for idx, val in df_or_dict[latest_col].items():
                        res[str(idx)] = str(val)
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

    fin = Finance(symbol=symbol, source="VCI")
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        ratio_df = fin.ratio(period="quarter", lang="vi")
    except Exception:
        try:
            ratio_df = fin.ratio(period="year", lang="vi")
        except Exception:
            ratio_df = None

    try:
        inc_df = fin.income_statement(period="quarter", lang="vi")
    except Exception:
        inc_df = None

    try:
        bal_df = fin.balance_sheet(period="quarter", lang="vi")
    except Exception:
        bal_df = None

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
