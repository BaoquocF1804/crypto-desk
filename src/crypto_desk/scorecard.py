"""Điểm số thực chiến của committee, gộp từ bảng ``reflections`` đã có.

Đây không phải backtest lịch sử. Evidence của một lần chạy (sổ lệnh, độ sâu,
RSS news, funding) chỉ tồn tại tại thời điểm chạy và không dựng lại được cho
quá khứ, nên không thể chạy lại committee trên một ngày đã qua. Thay vào đó
mỗi quyết định đã chốt được chấm sau ``REFLECTION_HORIZON_DAYS`` ngày rồi gộp
theo action để trả lời đúng một câu hỏi: khi desk nói ACCUMULATE, kết quả thực
tế là bao nhiêu.

Desk long-only, phương án thay thế của mọi quyết định là giữ USDT, nên "đúng
hướng" đo bằng lợi nhuận tuyệt đối (``close/entry - 1``): ACCUMULATE/HOLD đúng
khi lợi nhuận dương, REDUCE/EXIT/NO_TRADE đúng khi tài sản giảm sau khi desk
đứng ngoài. Alpha so với benchmark chỉ là cột phụ; chính benchmark bị loại khỏi
trung bình alpha vì alpha của nó luôn bằng 0 theo cấu tạo, nhưng vẫn được chấm
theo lợi nhuận.

Quyết định có entry/stop/target được chấm thêm theo mức nào chạm trước (xem
``service._barrier_outcome``): R:R chỉ là hình dạng của cược, còn lợi thế nằm
ở tỷ lệ chạm target trước, gộp lại thành kỳ vọng theo bội số rủi ro (R).

Nhiều lần chạy cùng symbol, cùng ngày, cùng action là cùng một quyết định bị
lấy mẫu lại; chỉ lần muộn nhất được tính để một ngày chạy 40 lần không át các
ngày khác.

Run nghiên cứu từ watchlist của máy quét được chấm thành nhóm riêng (`cohort`),
để thành tích của máy quét không trộn vào allowlist.
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
    mean_return: Decimal
    mean_alpha: Decimal | None
    correct_direction_rate: Decimal
    mean_worst_excursion: Decimal
    resolved: int
    target_first_rate: Decimal | None
    mean_r_multiple: Decimal | None


@dataclass(frozen=True, slots=True)
class BenchmarkGroup:
    benchmark: str
    cohort: str
    inferred: bool
    scores: tuple[ActionScore, ...]


@dataclass(frozen=True, slots=True)
class Scorecard:
    horizon_days: int
    benchmark: str
    scored: int
    collapsed: int
    skipped_no_action: int
    skipped_unknown_action: int
    groups: tuple[BenchmarkGroup, ...]

    def render(self) -> str:
        lines = [
            f"Điểm số committee — cửa sổ {self.horizon_days} ngày, "
            f"so với giữ USDT (alpha phụ so với {self.benchmark})",
            f"Đã chấm {self.scored} quyết định "
            f"(gộp {self.collapsed} lần chạy trùng symbol/ngày/action, "
            f"bỏ qua {self.skipped_no_action} thiếu action, "
            f"{self.skipped_unknown_action} action lạ).",
        ]
        if not self.groups:
            lines.append("Chưa đủ dữ liệu để chấm.")
            return "\n".join(lines)
        for group in self.groups:
            lines.append("")
            label = " · watchlist (nghiên cứu)" if group.cohort == "watchlist" else ""
            lines.append(f"Benchmark: {group.benchmark}{label}")
            if group.inferred:
                lines.append(
                    "  Benchmark của nhóm này được suy ra từ symbol, không đọc từ dữ liệu."
                )
            lines.append(
                f"{'Action':<12}{'N':>4}{'LN TB':>10}{'Alpha TB':>10}"
                f"{'Đúng hướng':>12}{'Tệ nhất':>10}{'TP trước':>12}{'Kỳ vọng':>10}"
            )
            for score in group.scores:
                target_first = (
                    "—"
                    if score.target_first_rate is None
                    else f"{score.target_first_rate * 100:.0f}% ({score.resolved})"
                )
                expectancy = (
                    "—" if score.mean_r_multiple is None else f"{score.mean_r_multiple:+.2f}R"
                )
                alpha = "—" if score.mean_alpha is None else format_pct(score.mean_alpha)
                lines.append(
                    f"{score.action:<12}{score.samples:>4}"
                    f"{format_pct(score.mean_return):>10}{alpha:>10}"
                    f"{score.correct_direction_rate * 100:>11.0f}%"
                    f"{format_pct(score.mean_worst_excursion):>10}"
                    f"{target_first:>12}{expectancy:>10}"
                )
        lines.append("")
        lines.append(
            "Đúng hướng so với giữ USDT: ACCUMULATE/HOLD đúng khi lợi nhuận dương, "
            "REDUCE/EXIT/NO_TRADE đúng khi tài sản giảm."
        )
        lines.append(
            "TP trước = tỷ lệ chạm target trước stop trong số setup đã chạm một mức "
            "(ngày chạm cả hai tính là stop); Kỳ vọng = trung bình R, target = +R:R, "
            "stop = −1R, chưa chạm = lãi/lỗ theo giá đóng cửa cuối chia rủi ro."
        )
        lines.append(
            f"Mỗi symbol/ngày/action tính một lần. Các cửa sổ {self.horizon_days} ngày "
            "vẫn chồng lấn nên N không phải số quan sát độc lập."
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
    latest: dict[tuple[str, str, str, str, str], tuple[str, dict[str, Any]]] = {}
    inferred_flags: dict[tuple[str, str], bool] = {}
    skipped_no_action = 0
    skipped_unknown_action = 0
    for item in reflections:
        bench, inferred = _benchmark_of(item)
        payload = item["payload"]
        cohort = str(payload.get("cohort") or "allowlist")
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
        when = str(payload.get("decision_cutoff") or item["created_at"])
        group = (bench, cohort)
        inferred_flags[group] = inferred_flags.get(group, False) or inferred
        key = (bench, cohort, action, item["symbol"], when[:10])
        if key not in latest or when > latest[key][0]:
            latest[key] = (when, item)
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (bench, cohort, action, _, _), (_, item) in latest.items():
        buckets.setdefault((bench, cohort, action), []).append(item)
    groups = tuple(
        BenchmarkGroup(
            benchmark=bench,
            cohort=cohort,
            inferred=inferred_flags[(bench, cohort)],
            scores=tuple(
                _score(action, bench, buckets[(bench, cohort, action)])
                for action in ACTIONS
                if (bench, cohort, action) in buckets
            ),
        )
        for bench, cohort in sorted(inferred_flags)
    )
    counted = len(reflections) - skipped_no_action - skipped_unknown_action
    return Scorecard(
        horizon_days=REFLECTION_HORIZON_DAYS,
        benchmark=BENCHMARK_SYMBOL,
        scored=len(latest),
        collapsed=counted - len(latest),
        skipped_no_action=skipped_no_action,
        skipped_unknown_action=skipped_unknown_action,
        groups=groups,
    )


def _mean(values: list[Decimal]) -> Decimal | None:
    return (sum(values, Decimal(0)) / len(values)).quantize(QUANTUM) if values else None


def _score(action: str, bench: str, rows: list[dict[str, Any]]) -> ActionScore:
    payloads = [row["payload"] for row in rows]
    returns = [Decimal(str(p["realized_return"])) for p in payloads]
    alphas = [Decimal(str(row["payload"]["alpha"])) for row in rows if row["symbol"] != bench]
    outcomes = [p["barrier_outcome"] for p in payloads if p.get("barrier_outcome")]
    resolved = [outcome for outcome in outcomes if outcome in {"target", "stop"}]
    r_multiples = [Decimal(str(p["r_multiple"])) for p in payloads if p.get("barrier_outcome")]
    correct = sum(1 for value in returns if _is_correct(action, value))
    return ActionScore(
        action=action,
        samples=len(rows),
        mean_return=_mean(returns),
        mean_alpha=_mean(alphas),
        correct_direction_rate=(Decimal(correct) / len(rows)).quantize(QUANTUM),
        mean_worst_excursion=_mean(
            [Decimal(str(p["maximum_adverse_excursion"])) for p in payloads]
        ),
        resolved=len(resolved),
        target_first_rate=(
            (Decimal(resolved.count("target")) / len(resolved)).quantize(QUANTUM)
            if resolved
            else None
        ),
        mean_r_multiple=_mean(r_multiples),
    )


def _is_correct(action: str, realized_return: Decimal) -> bool:
    """Phương án thay thế là giữ USDT, nên chiều đúng phụ thuộc vào việc desk có giữ hàng không."""
    return realized_return > 0 if action in LONG_ACTIONS else realized_return < 0
