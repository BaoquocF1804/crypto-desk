"""Typed web command contract and safe result models."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .domain import TradeTicket, to_jsonable

COMMAND_KINDS = (
    "doctor",
    "sync",
    "screen",
    "analyze",
    "daily",
    "health",
    "tickets",
    "orders",
    "reflections",
    "preview",
    "execute",
    "vn-analyze",
    "vn-daily",
)
EXECUTION_KINDS = frozenset({"preview", "execute"})
STATE_CHANGING_KINDS = frozenset(
    {"sync", "analyze", "daily", "health", "execute", "vn-analyze", "vn-daily"}
)

MAX_ARGS_BYTES = 4096
MAX_RESULT_BYTES = 32768

FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "token",
        "telegram",
        "actor",
        "confirmation",
        "report_dir",
        "client_order_id",
        "payload",
        "raw",
        "secret",
    }
)

_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,10}$")

ExecutionAction = Literal["approve", "reject", "reconcile"]
ExecutionMode = Literal["TESTNET_ORDER", "DRY_RUN"]


class UnsafeResultError(RuntimeError):
    """A safe result contains forbidden data or exceeds its size cap."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def command_hash(
    command_id: str,
    kind: str,
    args: dict[str, Any],
) -> str:
    material = canonical_json({"args": args, "id": command_id, "kind": kind})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_FINGERPRINT_FIELDS = (
    "environment",
    "expires_at",
    "id",
    "intent",
    "limit_price",
    "notional_usdt",
    "quantity",
    "side",
    "status",
    "stop_price",
    "symbol",
    "target_price",
)


def ticket_fingerprint(ticket: TradeTicket) -> str:
    material = canonical_json({name: getattr(ticket, name) for name in _FINGERPRINT_FIELDS})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyArgs(_Args):
    pass


def _check_symbol(value: str) -> str:
    symbol = value.upper()
    if not _SYMBOL_PATTERN.match(symbol):
        raise ValueError("symbol is outside the configured allowlist")
    return symbol


class AnalyzeArgs(_Args):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return _check_symbol(value)


class ReflectionsArgs(_Args):
    symbol: str | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str | None) -> str | None:
        return None if value is None else _check_symbol(value)


def _check_ticket_id(value: str) -> str:
    if not _TICKET_ID_PATTERN.match(value):
        raise ValueError("invalid ticket id")
    return value


class PreviewArgs(_Args):
    action: ExecutionAction
    ticket_id: str

    @field_validator("ticket_id")
    @classmethod
    def _ticket(cls, value: str) -> str:
        return _check_ticket_id(value)


class ExecuteArgs(_Args):
    action: ExecutionAction
    ticket_id: str
    preview_command_id: str
    fingerprint: str

    @field_validator("ticket_id")
    @classmethod
    def _ticket(cls, value: str) -> str:
        return _check_ticket_id(value)

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint(cls, value: str) -> str:
        if not re.match(r"^[0-9a-f]{64}$", value):
            raise ValueError("invalid fingerprint")
        return value


_ARGS_MODELS: dict[str, type[_Args]] = {
    "doctor": EmptyArgs,
    "sync": EmptyArgs,
    "screen": EmptyArgs,
    "daily": EmptyArgs,
    "health": EmptyArgs,
    "tickets": EmptyArgs,
    "orders": EmptyArgs,
    "analyze": AnalyzeArgs,
    "reflections": ReflectionsArgs,
    "preview": PreviewArgs,
    "execute": ExecuteArgs,
    "vn-analyze": AnalyzeArgs,
    "vn-daily": EmptyArgs,
}


def parse_args(kind: str, args: dict[str, Any]) -> _Args:
    model = _ARGS_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unknown command kind: {kind}")
    if len(canonical_json(args).encode("utf-8")) > MAX_ARGS_BYTES:
        raise ValueError("arguments exceed the 4 KiB limit")
    try:
        return model(**args)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from None


class _SafeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SafeDoctorResult(_SafeResult):
    python: str
    package_versions: dict[str, str | None]
    provider: str
    binance_environment: str
    binance_keys_present: bool
    testnet_execution_enabled: bool
    execution_mode: ExecutionMode
    database_schema_version: int
    schedule: dict[str, str]


class SafeSyncResult(_SafeResult):
    environment: str
    nav_usdt: str
    free_usdt: str
    positions_count: int
    open_orders_count: int
    as_of: str


class SafeScreenItem(_SafeResult):
    symbol: str
    passes: bool
    score: str
    reasons: list[str]


class SafeScreenResult(_SafeResult):
    items: list[SafeScreenItem]


class SafeAnalyzeResult(_SafeResult):
    run_id: str
    symbol: str
    cutoff: str
    action: str
    conviction: str
    reason: str
    entry: str | None
    stop: str | None
    target: str | None
    current_price: str | None
    ticket_id: str | None


class SafeDailyResult(_SafeResult):
    status: str
    bucket: str
    run_ids: list[str]
    screen: list[SafeScreenItem]


class SafeHealthResult(_SafeResult):
    status: str
    bucket: str
    alerts: list[str]
    run_ids: list[str]
    reconciled_count: int


class SafeTicketRow(_SafeResult):
    id: str
    environment: str
    symbol: str
    status: str
    created_at: str
    expires_at: str


class SafeTicketsResult(_SafeResult):
    tickets: list[SafeTicketRow]


class SafeOrderRow(_SafeResult):
    ticket_id: str
    environment: str
    status: str
    updated_at: str


class SafeOrdersResult(_SafeResult):
    orders: list[SafeOrderRow]


class SafeReflectionRow(_SafeResult):
    run_id: str
    symbol: str
    created_at: str
    realized_return: str | None
    alpha: str | None


class SafeReflectionsResult(_SafeResult):
    reflections: list[SafeReflectionRow]


class SafePreviewResult(_SafeResult):
    action: ExecutionAction
    ticket_id: str
    environment: str
    execution_mode: ExecutionMode
    status: str
    symbol: str
    side: str
    intent: str
    quantity: str
    notional_usdt: str
    entry: str
    stop: str
    target: str
    created_at: str
    expires_at: str
    submission_status: str | None
    fingerprint: str


class SafeExecuteResult(_SafeResult):
    action: ExecutionAction
    ticket_id: str
    status: str


def ensure_safe_result(
    model: BaseModel,
) -> dict[str, Any]:
    dumped = model.model_dump(mode="json")
    _walk_forbidden(dumped)
    if len(canonical_json(dumped).encode("utf-8")) > MAX_RESULT_BYTES:
        raise UnsafeResultError("safe result exceeds the 32 KiB limit")
    return dumped


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in FORBIDDEN_RESULT_KEYS:
                raise UnsafeResultError(f"forbidden result key: {key}")
            _walk_forbidden(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_forbidden(item)
