"""Điểm số thực chiến của committee, gộp từ bảng ``reflections`` đã có.

Đây không phải backtest lịch sử. Evidence của một lần chạy (sổ lệnh, độ sâu,
RSS news, funding) chỉ tồn tại tại thời điểm chạy và không dựng lại được cho
quá khứ, nên không thể chạy lại committee trên một ngày đã qua. Thay vào đó
mỗi quyết định đã chốt được chấm sau ``REFLECTION_HORIZON_DAYS`` ngày bằng
alpha so với ``BENCHMARK_SYMBOL``, rồi gộp theo action để trả lời đúng một câu
hỏi: khi desk nói ACCUMULATE, alpha thực tế là bao nhiêu.

``BENCHMARK_SYMBOL`` bị loại khỏi thống kê vì alpha của nó luôn bằng 0 theo
cấu tạo; giữ lại sẽ kéo mọi trung bình về 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import BENCHMARK_SYMBOL, REFLECTION_HORIZON_DAYS
from .domain import format_pct

ACTIONS = ("ACCUMULATE", "HOLD", "REDUCE", "EXIT", "NO_TRADE")


@dataclass(frozen=True, slots=True)
class ActionScore:
    action: str
    samples: int
    mean_alpha: Decimal
    median_alpha: Decimal
    hit_rate: Decimal
    mean_adverse_excursion: Decimal


@dataclass(frozen=True, slots=True)
class Scorecard:
    horizon_days: int
    benchmark: str
    scored: int
    skipped_no_action: int
    skipped_benchmark: int
    scores: tuple[ActionScore, ...]

    def render(self) -> str:
        lines = [
            f"Điểm số committee — cửa sổ {self.horizon_days} ngày, "
            f"alpha so với {self.benchmark}",
            f"Đã chấm {self.scored} quyết định "
            f"(bỏ qua {self.skipped_no_action} thiếu action, "
            f"{self.skipped_benchmark} thuộc chính benchmark).",
        ]
        if not self.scores:
            lines.append("Chưa đủ dữ liệu để chấm.")
            return "\n".join(lines)
        lines.append("")
        lines.append(
            f"{'Action':<12}{'N':>4}{'Alpha TB':>11}{'Alpha TV':>11}"
            f"{'Thắng':>8}{'Sụt sâu':>11}"
        )
        for score in self.scores:
            lines.append(
                f"{score.action:<12}{score.samples:>4}"
                f"{format_pct(score.mean_alpha):>11}"
                f"{format_pct(score.median_alpha):>11}"
                f"{score.hit_rate * 100:>7.0f}%"
                f"{format_pct(score.mean_adverse_excursion):>11}"
            )
        return "\n".join(lines)


def build_scorecard(reflections: list[dict[str, Any]]) -> Scorecard:
    buckets: dict[str, list[tuple[Decimal, Decimal]]] = {}
    skipped_no_action = 0
    skipped_benchmark = 0
    for item in reflections:
        if item["symbol"] == BENCHMARK_SYMBOL:
            skipped_benchmark += 1
            continue
        payload = item["payload"]
        action = payload.get("decision_action")
        if not action:
            skipped_no_action += 1
            continue
        buckets.setdefault(str(action), []).append(
            (
                Decimal(str(payload["alpha"])),
                Decimal(str(payload["maximum_adverse_excursion"])),
            )
        )
    scores = tuple(_score(action, buckets[action]) for action in ACTIONS if action in buckets)
    return Scorecard(
        horizon_days=REFLECTION_HORIZON_DAYS,
        benchmark=BENCHMARK_SYMBOL,
        scored=sum(score.samples for score in scores),
        skipped_no_action=skipped_no_action,
        skipped_benchmark=skipped_benchmark,
        scores=scores,
    )


def _score(action: str, rows: list[tuple[Decimal, Decimal]]) -> ActionScore:
    alphas = sorted(alpha for alpha, _ in rows)
    count = len(alphas)
    middle = count // 2
    median = (
        alphas[middle] if count % 2 else (alphas[middle - 1] + alphas[middle]) / Decimal(2)
    )
    wins = sum(1 for alpha in alphas if alpha > 0)
    return ActionScore(
        action=action,
        samples=count,
        mean_alpha=sum(alphas, Decimal(0)) / count,
        median_alpha=median,
        hit_rate=Decimal(wins) / count,
        mean_adverse_excursion=sum((mae for _, mae in rows), Decimal(0)) / count,
    )
