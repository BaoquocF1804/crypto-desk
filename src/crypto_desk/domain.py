from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal


Environment = Literal["testnet", "mainnet"]
Action = Literal["HOLD", "ACCUMULATE", "REDUCE", "EXIT", "NO_TRADE"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat()


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def format_pct(value: Any) -> str:
    """Tỉ lệ thập phân thành phần trăm có dấu, ví dụ Decimal('0.0312') -> '+3.12%'."""
    return f"{Decimal(str(value)) * 100:+.2f}%"


# Lý do của một quyết định thật, đủ cả hai bản: chuỗi tiếng Anh cũ và bản tiếng
# Việt hiện hành. Dùng để suy ra ``decided`` cho hàng ghi trước khi có cờ đó.
DECIDED_REASONS = frozenset({"committee decision", "Quyết định của hội đồng."})


def is_decided(decision: dict[str, Any]) -> bool:
    """Hội đồng có thật sự ra quyết định này không, hay đây là một lần chạy hỏng.

    ``_no_trade`` sinh ra một ``ResearchDecision`` trông y hệt quyết định thật,
    nên nếu không phân biệt được hai thứ thì bảng chấm điểm sẽ đo giá đi đâu sau
    một lần rate limit và gọi đó là kết quả của một quyết định.
    """
    flag = decision.get("decided")
    if flag is not None:
        return bool(flag)
    return str(decision.get("reason", "")) in DECIDED_REASONS


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if self.quote_asset != "USDT" or not self.symbol.endswith("USDT"):
            raise ValueError("V1 supports USDT quote symbols only")
        if min(self.tick_size, self.step_size, self.min_qty, self.min_notional) <= 0:
            raise ValueError("Binance symbol filters must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    environment: Environment
    nav_usdt: Decimal
    free_usdt: Decimal
    positions: tuple[dict[str, str], ...]
    open_orders: tuple[dict[str, Any], ...]
    as_of: str


EvidenceKind = Literal["spot", "news", "derivatives", "reference", "flow", "fundamentals"]


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    id: str
    kind: EvidenceKind
    provider: str
    source: str
    fetched_at: str
    as_of: str
    delayed: bool
    stale: bool
    payload: dict[str, Any]
    payload_hash: str

    @classmethod
    def create(
        cls,
        *,
        kind: EvidenceKind,
        provider: str,
        source: str,
        fetched_at: str,
        as_of: str,
        delayed: bool,
        stale: bool,
        payload: dict[str, Any],
    ) -> EvidenceItem:
        encoded = repr(to_jsonable(payload)).encode()
        payload_hash = hashlib.sha256(encoded).hexdigest()
        return cls(
            id=payload_hash[:16],
            kind=kind,
            provider=provider,
            source=source,
            fetched_at=fetched_at,
            as_of=as_of,
            delayed=delayed,
            stale=stale,
            payload=payload,
            payload_hash=payload_hash,
        )


@dataclass(frozen=True, slots=True)
class FuturesTradeSetup:
    direction: Literal["LONG", "SHORT"]
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_reward_ratio: Decimal
    rationale: str


ThesisContinuity = Literal["NEW", "CONTINUED", "PIVOTED", "INVALIDATED"]


@dataclass(frozen=True, slots=True)
class PriorThesisContext:
    run_id: str
    cutoff: str
    hours_ago: Decimal
    action: Action
    conviction: Decimal
    prior_price: Decimal | None
    current_price: Decimal
    price_change_pct: Decimal | None
    bull_case: str
    bear_case: str
    catalysts: tuple[str, ...]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    futures_bias: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    symbol: str
    action: Action
    conviction: Decimal
    bull_case: str
    bear_case: str
    catalysts: tuple[str, ...]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    evidence_ids: tuple[str, ...]
    reason: str
    futures_bias: Literal["BULLISH", "BEARISH", "NEUTRAL"] | None = None
    futures_setups: tuple[FuturesTradeSetup, ...] = ()
    thesis_continuity: ThesisContinuity = "NEW"
    prior_run_id: str | None = None
    decided: bool = True


@dataclass(frozen=True, slots=True)
class TradeTicket:
    id: str
    environment: Environment
    symbol: str
    intent: Literal["OPEN", "ADD", "REDUCE", "CLOSE"]
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    notional_usdt: Decimal
    risk_snapshot: dict[str, Any]
    created_at: str
    expires_at: str
    status: str = "PENDING"

    @classmethod
    def create(cls, *, ttl_minutes: int, **values: Any) -> TradeTicket:
        created = utcnow()
        return cls(
            id=str(uuid.uuid4()),
            created_at=iso(created),
            expires_at=iso(created + timedelta(minutes=ttl_minutes)),
            **values,
        )
