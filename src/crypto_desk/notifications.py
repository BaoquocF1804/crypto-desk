from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any


def _price(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = f"{number:,.8f}".rstrip("0").rstrip(".")
    return text


def _short(value: Any, limit: int = 650) -> str:
    text = " ".join(str(value or "N/A").split())
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def analysis_summary(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    symbol = str(decision.get("symbol") or "N/A")
    action = str(decision.get("action") or "NO_TRADE")
    catalysts = decision.get("catalysts") or []
    catalyst_lines = [f"• {_short(item, 220)}" for item in catalysts[:3]] or ["• Không có"]
    ticket = payload.get("ticket_id") or "Không tạo"
    return "\n".join(
        (
            f"📊 CRYPTO DESK · {symbol}",
            f"💵 Giá hiện tại: {_price(payload.get('current_price'))} USDT",
            f"🧭 Quyết định: {action}",
            f"🎯 Độ tin cậy: {decision.get('conviction', '0')}/10",
            "",
            "🟢 LUẬN ĐIỂM TĂNG",
            _short(decision.get("bull_case")),
            "",
            "🔴 LUẬN ĐIỂM GIẢM / RỦI RO",
            _short(decision.get("bear_case")),
            "",
            "⚡ CHẤT XÚC TÁC",
            *catalyst_lines,
            "",
            "🛡 KẾ HOẠCH & QUẢN TRỊ RỦI RO",
            f"• Entry: {_price(decision.get('entry'))}",
            f"• Stop: {_price(decision.get('stop'))}",
            f"• Target: {_price(decision.get('target'))}",
            f"• Vô hiệu khi: {_short(decision.get('invalidation'), 350)}",
            "",
            f"🎫 Ticket: {ticket}",
            f"🧾 Run ID: {payload.get('run_id', 'N/A')}",
            "⚠️ Chỉ hỗ trợ quyết định, không phải tư vấn tài chính.",
        )
    )


def health_summary(payload: dict[str, Any]) -> str:
    lines = ["🩺 Crypto Desk health", f"Bucket: {payload.get('bucket', 'n/a')}"]
    alerts = payload.get("alerts") or []
    lines.append(f"Cảnh báo: {', '.join(alerts) if alerts else 'không có'}")
    if snapshot := payload.get("snapshot"):
        lines.extend(
            (
                f"NAV: {snapshot.get('nav_usdt', 'n/a')} USDT",
                f"USDT khả dụng: {snapshot.get('free_usdt', 'n/a')}",
                f"Vị thế: {len(snapshot.get('positions') or [])}",
                f"Lệnh mở: {len(snapshot.get('open_orders') or [])}",
            )
        )
    lines.append(f"Reconcile: {len(payload.get('reconciled') or [])}")
    return "\n".join(lines)


def daily_summary(payload: dict[str, Any]) -> str:
    lines = ["📊 Crypto Desk daily", f"Bucket: {payload.get('bucket', 'n/a')}"]
    for item in payload.get("screen") or []:
        mark = "✅" if item.get("passes") else "⛔"
        reason = ", ".join(item.get("reasons") or [])
        suffix = f" — {reason}" if reason else ""
        lines.append(f"{mark} {item.get('symbol')}: {item.get('score', '0')}{suffix}")
    lines.append(f"Analysis runs: {len(payload.get('run_ids') or [])}")
    return "\n".join(lines)


if __name__ == "__main__":
    formatter = {
        "analysis": analysis_summary,
        "health": health_summary,
        "daily": daily_summary,
    }[sys.argv[1]]
    print(formatter(json.load(sys.stdin)))
