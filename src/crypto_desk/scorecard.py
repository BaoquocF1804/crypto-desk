"""Điểm số thực chiến của committee, gộp từ bảng ``reflections`` đã có.

Đây không phải backtest lịch sử. Evidence của một lần chạy (sổ lệnh, độ sâu,
RSS news, funding) chỉ tồn tại tại thời điểm chạy và không dựng lại được cho
quá khứ, nên không thể chạy lại committee trên một ngày đã qua. Thay vào đó
mỗi quyết định đã chốt được chấm sau ``REFLECTION_HORIZON_DAYS`` ngày bằng
alpha so với ``BENCHMARK_SYMBOL``, rồi gộp theo action để trả lời đúng một câu
hỏi: khi desk nói ACCUMULATE, alpha thực tế là bao nhiêu.

``BENCHMARK_SYMBOL`` bị loại khỏi thống kê vì alpha của nó luôn bằng 0 theo
cấu tạo; giữ lại sẽ kéo mọi trung bình về 0.

Alpha luôn được đo theo chiều long (``close/entry - 1`` trừ benchmark), không
đảo dấu theo action. Vì vậy "đúng hướng" phải phụ thuộc vào action: desk giữ
hàng (ACCUMULATE/HOLD) đúng khi alpha dương, còn desk đứng ngoài hoặc cắt
(REDUCE/EXIT/NO_TRADE) đúng khi alpha âm — tài sản kém benchmark sau khi desk
rời đi.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, get_args

from .config import BENCHMARK_SYMBOL, REFLECTION_HORIZON_DAYS
from .domain import Action, format_pct

ACTIONS = get_args(Action)
LONG_ACTIONS = frozenset({"ACCUMULATE", "HOLD"})
QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ActionScore:
    action: str
    samples: int
    distinct_days: int
    mean_alpha: Decimal
    median_alpha: Decimal
    correct_direction_rate: Decimal
    mean_worst_excursion: Decimal


@dataclass(frozen=True, slots=True)
class BenchmarkGroup:
    benchmark: str
    inferred: bool
    scores: tuple[ActionScore, ...]


@dataclass(frozen=True, slots=True)
class Scorecard:
    horizon_days: int
    benchmark: str
    scored: int
    skipped_no_action: int
    skipped_unknown_action: int
    skipped_benchmark: int
    groups: tuple[BenchmarkGroup, ...]

    def render(self) -> str:
        lines = [
            f"Điểm số committee — cửa sổ {self.horizon_days} ngày, "
            f"alpha so với {self.benchmark}",
            f"Đã chấm {self.scored} quyết định "
            f"(bỏ qua {self.skipped_no_action} thiếu action, "
            f"{self.skipped_unknown_action} action lạ, "
            f"{self.skipped_benchmark} thuộc chính benchmark).",
        ]
        if not self.groups:
            lines.append("Chưa đủ dữ liệu để chấm.")
            return "\n".join(lines)
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
                lines.append(
                    f"{score.action:<12}{score.samples:>4}{score.distinct_days:>6}"
                    f"{format_pct(score.mean_alpha):>11}"
                    f"{format_pct(score.median_alpha):>11}"
                    f"{score.correct_direction_rate * 100:>11.0f}%"
                    f"{format_pct(score.mean_worst_excursion):>11}"
                )
        lines.append("")
        lines.append(
            "Alpha đo theo chiều long và không đảo dấu theo action: "
            "ACCUMULATE/HOLD đúng hướng khi alpha dương, còn REDUCE/EXIT/NO_TRADE "
            "đúng hướng khi alpha âm (desk rời đi và tài sản kém benchmark)."
        )
        lines.append(
            f"Ngày = số cặp (symbol, ngày quyết định) riêng biệt. Các cửa sổ "
            f"{self.horizon_days} ngày chồng lấn nhau nên N không phải số quan sát độc lập."
        )
        lines.append(
            "Tệ nhất = trung bình điểm tệ nhất trong cửa sổ (min lợi nhuận), "
            "có thể dương nếu cả cửa sổ đều lãi."
        )
        return "\n".join(lines)


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


def build_scorecard(reflections: list[dict[str, Any]]) -> Scorecard:
    buckets: dict[tuple[str, str], list[tuple[Decimal, Decimal, tuple[str, str]]]] = {}
    inferred_flags: dict[str, bool] = {}
    skipped_no_action = 0
    skipped_unknown_action = 0
    skipped_benchmark = 0
    for item in reflections:
        bench, inferred = _benchmark_of(item)
        if item["symbol"] == bench:
            skipped_benchmark += 1
            continue
        payload = item["payload"]
        action = payload.get("decision_action")
        if not action:
            skipped_no_action += 1
            continue
        action = str(action)
        if action not in ACTIONS:
            skipped_unknown_action += 1
            continue
        # Hàng cũ không có decision_cutoff: lùi về created_at, thô hơn nhưng vẫn
        # gộp được các lần chạy cùng ngày của cùng symbol.
        day = str(payload.get("decision_cutoff") or item["created_at"])[:10]
        inferred_flags[bench] = inferred_flags.get(bench, False) or inferred
        buckets.setdefault((bench, action), []).append(
            (
                Decimal(str(payload["alpha"])),
                Decimal(str(payload["maximum_adverse_excursion"])),
                (item["symbol"], day),
            )
        )
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
    return Scorecard(
        horizon_days=REFLECTION_HORIZON_DAYS,
        benchmark=BENCHMARK_SYMBOL,
        scored=sum(s.samples for g in groups for s in g.scores),
        skipped_no_action=skipped_no_action,
        skipped_unknown_action=skipped_unknown_action,
        skipped_benchmark=skipped_benchmark,
        groups=groups,
    )


def _score(action: str, rows: list[tuple[Decimal, Decimal, tuple[str, str]]]) -> ActionScore:
    alphas = sorted(alpha for alpha, _, _ in rows)
    count = len(alphas)
    middle = count // 2
    median = (
        alphas[middle] if count % 2 else (alphas[middle - 1] + alphas[middle]) / Decimal(2)
    )
    correct = sum(1 for alpha in alphas if _is_correct(action, alpha))
    return ActionScore(
        action=action,
        samples=count,
        distinct_days=len({key for _, _, key in rows}),
        mean_alpha=(sum(alphas, Decimal(0)) / count).quantize(QUANTUM),
        median_alpha=median.quantize(QUANTUM),
        correct_direction_rate=(Decimal(correct) / count).quantize(QUANTUM),
        mean_worst_excursion=(sum((mae for _, mae, _ in rows), Decimal(0)) / count).quantize(
            QUANTUM
        ),
    )


def _is_correct(action: str, alpha: Decimal) -> bool:
    """Alpha là long-only, nên chiều đúng phụ thuộc vào việc desk có giữ hàng không."""
    return alpha > 0 if action in LONG_ACTIONS else alpha < 0
