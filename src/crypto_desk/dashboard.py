from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Callable, Literal, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .domain import Action, Environment, iso, utcnow
from .store import TERMINAL_CHAIN_STATES, Store


HEALTH_STALE_SECONDS = 20 * 60
HEALTH_OFFLINE_SECONDS = 60 * 60
ACTIONABLE_TICKET_STATUSES = frozenset({"PENDING"})

DASHBOARD_INGEST_URL_ENV = "CRYPTO_DESK_DASHBOARD_INGEST_URL"
DASHBOARD_INGEST_TOKEN_ENV = "CRYPTO_DESK_DASHBOARD_INGEST_TOKEN"
SITES_BYPASS_TOKEN_ENV = "CRYPTO_DESK_SITES_BYPASS_TOKEN"
_MAX_PUBLISH_PAYLOAD_BYTES = 64 * 1024

HealthState = Literal["online", "degraded", "offline"]
AttemptState = Literal["valid", "blocked"]


class LatestAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    cutoff: str
    state: AttemptState
    reason: str


class LatestValidDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    cutoff: str
    action: Action
    conviction: Decimal
    bull_case: str
    bear_case: str
    catalysts: list[str]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None


class SymbolSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    mark_usdt: Decimal | None
    change_24h_pct: Decimal | None
    position_share_pct: Decimal | None
    latest_attempt: LatestAttempt | None
    latest_valid_decision: LatestValidDecision | None


class ConfiguredPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    asset: str
    quantity: Decimal
    mark_usdt: Decimal
    value_usdt: Decimal
    share_pct: Decimal


class PortfolioSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str | None
    nav_usdt: Decimal
    free_usdt: Decimal
    gross_exposure_usdt: Decimal
    deployed_pct: Decimal
    open_orders_count: int
    configured_positions: list[ConfiguredPosition]
    external_assets_count: int
    external_value_usdt: Decimal
    unpriced_assets_count: int


class HealthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_state: HealthState
    research_state: HealthState
    last_health_at: str | None
    stale_after_seconds: int
    alerts: list[str]


class RecentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["order"]
    at: str
    summary: str


class OperationsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickets_total: int
    tickets_actionable: int
    orders_total: int
    orders_open: int
    recent_events: list[RecentEvent]


class DashboardSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    generated_at: str
    environment: Environment
    health: HealthStatus
    portfolio: PortfolioSection
    symbols: list[SymbolSection]
    operations: OperationsSection


class _PricedShare(NamedTuple):
    mark_usdt: Decimal
    share_pct: Decimal


def _is_priced_position(position: dict[str, str]) -> bool:
    return position.get("unpriced") is not True and Decimal(position["total"]) > 0


def _split_positions(
    positions: list[dict[str, str]],
    settings: Settings,
    nav_usdt: Decimal,
) -> tuple[list[ConfiguredPosition], int, Decimal, int, dict[str, _PricedShare]]:
    configured_positions: list[ConfiguredPosition] = []
    external_assets_count = 0
    external_value_usdt = Decimal("0")
    unpriced_assets_count = 0
    priced_by_symbol: dict[str, _PricedShare] = {}

    for position in positions:
        if not _is_priced_position(position):
            unpriced_assets_count += 1
            continue
        value_usdt = Decimal(position["value_usdt"])
        mark_usdt = Decimal(position["mid_usdt"])
        share_pct = value_usdt / nav_usdt * 100 if nav_usdt > 0 else Decimal("0")
        symbol = position["symbol"]
        if symbol in settings.symbols:
            configured_positions.append(
                ConfiguredPosition(
                    symbol=symbol,
                    asset=position["asset"],
                    quantity=Decimal(position["total"]),
                    mark_usdt=mark_usdt,
                    value_usdt=value_usdt,
                    share_pct=share_pct,
                )
            )
            priced_by_symbol[symbol] = _PricedShare(mark_usdt=mark_usdt, share_pct=share_pct)
        else:
            external_assets_count += 1
            external_value_usdt += value_usdt

    return (
        configured_positions,
        external_assets_count,
        external_value_usdt,
        unpriced_assets_count,
        priced_by_symbol,
    )


def _build_symbols(
    settings: Settings,
    store: Store,
    priced_by_symbol: dict[str, _PricedShare],
) -> list[SymbolSection]:
    results: list[SymbolSection] = []
    for symbol in settings.symbols:
        priced = priced_by_symbol.get(symbol)

        latest_attempt_row = store.latest_run(symbol)
        latest_attempt: LatestAttempt | None = None
        if latest_attempt_row is not None:
            decision = latest_attempt_row["decision"]
            state: AttemptState = "valid" if decision.get("evidence_ids") else "blocked"
            latest_attempt = LatestAttempt(
                run_id=latest_attempt_row["id"],
                cutoff=latest_attempt_row["cutoff"],
                state=state,
                reason=decision["reason"],
            )

        latest_valid_row = store.latest_valid_run(symbol)
        latest_valid_decision: LatestValidDecision | None = None
        if latest_valid_row is not None:
            decision = latest_valid_row["decision"]
            latest_valid_decision = LatestValidDecision(
                run_id=latest_valid_row["id"],
                cutoff=latest_valid_row["cutoff"],
                action=decision["action"],
                conviction=Decimal(decision["conviction"]),
                bull_case=decision["bull_case"],
                bear_case=decision["bear_case"],
                catalysts=list(decision["catalysts"]),
                invalidation=decision["invalidation"],
                entry=Decimal(decision["entry"]) if decision["entry"] is not None else None,
                stop=Decimal(decision["stop"]) if decision["stop"] is not None else None,
                target=Decimal(decision["target"]) if decision["target"] is not None else None,
            )

        results.append(
            SymbolSection(
                symbol=symbol,
                mark_usdt=priced.mark_usdt if priced else None,
                change_24h_pct=None,
                position_share_pct=priced.share_pct if priced else None,
                latest_attempt=latest_attempt,
                latest_valid_decision=latest_valid_decision,
            )
        )
    return results


def _build_health(
    store: Store,
    now: datetime,
    symbols: list[SymbolSection],
) -> HealthStatus:
    scheduled = store.latest_scheduled_run("health")
    last_health_at = scheduled["completed_at"] if scheduled else None

    if last_health_at is None:
        runner_state: HealthState = "offline"
    else:
        age_seconds = (now - datetime.fromisoformat(last_health_at)).total_seconds()
        if age_seconds <= HEALTH_STALE_SECONDS:
            runner_state = "online"
        elif age_seconds <= HEALTH_OFFLINE_SECONDS:
            runner_state = "degraded"
        else:
            runner_state = "offline"

    alerts = [
        f"research_freshness:{item.symbol}"
        for item in symbols
        if item.latest_attempt is None
        or item.latest_attempt.state == "blocked"
        or item.latest_valid_decision is None
    ]
    research_state: HealthState = "degraded" if alerts else "online"

    return HealthStatus(
        runner_state=runner_state,
        research_state=research_state,
        last_health_at=last_health_at,
        stale_after_seconds=HEALTH_STALE_SECONDS,
        alerts=alerts,
    )


def _build_operations(store: Store, environment: Environment) -> OperationsSection:
    tickets = [row for row in store.list_tickets() if row["environment"] == environment]
    tickets_actionable = sum(1 for row in tickets if row["status"] in ACTIONABLE_TICKET_STATUSES)
    submissions = [row for row in store.list_submissions() if row["environment"] == environment]
    orders_open = sum(1 for row in submissions if row["status"] not in TERMINAL_CHAIN_STATES)
    recent_events = [
        RecentEvent(
            kind="order",
            at=event["event_time"],
            summary=f"Order {event['status'].lower()}",
        )
        for event in store.recent_order_events(limit=5, environment=environment)
    ]
    return OperationsSection(
        tickets_total=len(tickets),
        tickets_actionable=tickets_actionable,
        orders_total=len(submissions),
        orders_open=orders_open,
        recent_events=recent_events,
    )


def build_dashboard_snapshot(
    settings: Settings,
    store: Store,
    *,
    now: Callable[[], datetime] = utcnow,
) -> DashboardSnapshot:
    current = now()
    environment = settings.binance.environment
    portfolio_snapshot = store.latest_snapshot(environment)

    if portfolio_snapshot is None:
        as_of = None
        nav_usdt = Decimal("0")
        free_usdt = Decimal("0")
        open_orders_count = 0
        raw_positions: list[dict[str, str]] = []
    else:
        as_of = portfolio_snapshot.as_of
        nav_usdt = portfolio_snapshot.nav_usdt
        free_usdt = portfolio_snapshot.free_usdt
        open_orders_count = len(portfolio_snapshot.open_orders)
        raw_positions = list(portfolio_snapshot.positions)

    gross_exposure_usdt = max(nav_usdt - free_usdt, Decimal("0"))
    deployed_pct = gross_exposure_usdt / nav_usdt * 100 if nav_usdt > 0 else Decimal("0")

    (
        configured_positions,
        external_assets_count,
        external_value_usdt,
        unpriced_assets_count,
        priced_by_symbol,
    ) = _split_positions(raw_positions, settings, nav_usdt)

    portfolio = PortfolioSection(
        as_of=as_of,
        nav_usdt=nav_usdt,
        free_usdt=free_usdt,
        gross_exposure_usdt=gross_exposure_usdt,
        deployed_pct=deployed_pct,
        open_orders_count=open_orders_count,
        configured_positions=configured_positions,
        external_assets_count=external_assets_count,
        external_value_usdt=external_value_usdt,
        unpriced_assets_count=unpriced_assets_count,
    )

    symbols = _build_symbols(settings, store, priced_by_symbol)
    health = _build_health(store, current, symbols)
    operations = _build_operations(store, environment)

    return DashboardSnapshot(
        generated_at=iso(current),
        environment=environment,
        health=health,
        portfolio=portfolio,
        symbols=symbols,
        operations=operations,
    )


class DashboardPublishError(RuntimeError):
    """Raised when a dashboard snapshot could not be published.

    The message is always safe to display/log: it never contains header
    values, tokens, or the remote response body.
    """


def publish_dashboard(
    snapshot: DashboardSnapshot,
    *,
    url: str,
    ingest_token: str,
    sites_bypass_token: str,
    client: httpx.Client | None = None,
) -> None:
    body = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > _MAX_PUBLISH_PAYLOAD_BYTES:
        raise DashboardPublishError("dashboard payload exceeds the 64 KiB publish limit")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ingest_token}",
        "OAI-Sites-Authorization": f"Bearer {sites_bypass_token}",
    }
    owns_client = client is None
    active = client if client is not None else httpx.Client(timeout=10)
    try:
        response = active.post(
            url,
            content=body,
            headers=headers,
            timeout=10,
            follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        category = "authentication failed" if status in (401, 403) else "request failed"
        raise DashboardPublishError(f"dashboard ingest {category} (status {status})") from None
    except httpx.HTTPError as exc:
        raise DashboardPublishError(
            f"dashboard ingest request failed ({type(exc).__name__})"
        ) from None
    finally:
        if owns_client:
            active.close()


def publish_dashboard_from_env(snapshot: DashboardSnapshot, *, strict: bool) -> None:
    url = os.environ.get(DASHBOARD_INGEST_URL_ENV)
    ingest_token = os.environ.get(DASHBOARD_INGEST_TOKEN_ENV)
    sites_bypass_token = os.environ.get(SITES_BYPASS_TOKEN_ENV)
    missing = [
        name
        for name, value in (
            (DASHBOARD_INGEST_URL_ENV, url),
            (DASHBOARD_INGEST_TOKEN_ENV, ingest_token),
            (SITES_BYPASS_TOKEN_ENV, sites_bypass_token),
        )
        if not value
    ]
    if missing:
        if strict:
            raise DashboardPublishError(
                f"missing required env vars for dashboard publish: {', '.join(missing)}"
            )
        return

    publish_dashboard(
        snapshot,
        url=url,
        ingest_token=ingest_token,
        sites_bypass_token=sites_bypass_token,
    )
