from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from .config import V1_SYMBOLS
from .data import EvidenceSnapshot


MIN_QUOTE_VOLUME = Decimal("50000000")
MAX_SPREAD = Decimal("0.002")
VOLUME_NORMALIZER = Decimal("500000000")


@dataclass(frozen=True, slots=True)
class ScreenResult:
    symbol: str
    passes: bool
    score: Decimal
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def screen(
    snapshot: EvidenceSnapshot,
    *,
    allowlist: Collection[str] = V1_SYMBOLS,
) -> ScreenResult:
    reasons: list[str] = []
    if snapshot.symbol not in allowlist or snapshot.symbol not in V1_SYMBOLS:
        reasons.append("allowlist")
    if snapshot.quote_volume < MIN_QUOTE_VOLUME:
        reasons.append("quote_volume")
    if snapshot.spread > MAX_SPREAD:
        reasons.append("spread")
    if len(snapshot.daily_closes) < 90:
        reasons.append("daily_history")
    if any(item.stale for item in snapshot.items):
        reasons.append("stale_evidence")

    momentum_20d = _momentum(snapshot.daily_closes, 20)
    momentum_60d = _momentum(snapshot.daily_closes, 60)
    if momentum_20d is None or momentum_20d <= 0:
        reasons.append("momentum_20d")

    if reasons or momentum_60d is None:
        return ScreenResult(
            symbol=snapshot.symbol,
            passes=False,
            score=Decimal("0"),
            reasons=tuple(reasons),
            evidence_ids=snapshot.evidence_ids,
        )

    normalized_log_volume = min(
        Decimal("1"),
        snapshot.quote_volume / VOLUME_NORMALIZER,
    )
    score = (
        Decimal("0.45") * momentum_20d
        + Decimal("0.25") * momentum_60d
        + Decimal("0.20") * normalized_log_volume
        - Decimal("0.10") * snapshot.spread
    )
    return ScreenResult(
        symbol=snapshot.symbol,
        passes=True,
        score=score,
        reasons=(),
        evidence_ids=snapshot.evidence_ids,
    )


def _momentum(closes: tuple[Decimal, ...], periods: int) -> Decimal | None:
    if len(closes) <= periods:
        return None
    previous = closes[-(periods + 1)]
    latest = closes[-1]
    if previous <= 0:
        return None
    return latest / previous - Decimal("1")
