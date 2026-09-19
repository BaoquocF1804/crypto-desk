from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, NamedTuple
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_serializer

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
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

HealthState = Literal["online", "degraded", "offline"]
AttemptState = Literal["valid", "blocked"]
AssetState = Literal["cash", "managed", "external", "unpriced"]


class LatestAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    cutoff: str
    state: AttemptState
    reason: str


class DashboardFuturesSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["LONG", "SHORT"]
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_reward_ratio: Decimal
    rationale: str


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
    futures_bias: str | None = None
    futures_setups: list[DashboardFuturesSetup] = []


class DerivativesIndicators(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funding_rate: Decimal | None = None
    funding_rate_trend: str | None = None
    open_interest: Decimal | None = None
    oi_change_1h_pct: Decimal | None = None
    long_short_ratio: Decimal | None = None
    top_trader_ratio: Decimal | None = None
    taker_buy_sell_ratio: Decimal | None = None


class DashboardMemberVote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    label: str
    stance: str
    confidence: Decimal
    summary: str


class DashboardCommitteeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: str  # "LONG" | "SHORT" | "NEUTRAL"
    long_pct: int
    short_pct: int
    neutral_pct: int
    summary: str
    member_votes: list[DashboardMemberVote] = []


class SymbolSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    mark_usdt: Decimal | None
    change_24h_pct: Decimal | None
    position_share_pct: Decimal | None
    latest_attempt: LatestAttempt | None
    latest_valid_decision: LatestValidDecision | None
    sparkline_closes: list[Decimal] = Field(default_factory=list)
    derivatives_indicators: DerivativesIndicators | None = None
    committee_evaluation: DashboardCommitteeEvaluation | None = None


class ConfiguredPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    asset: str
    quantity: Decimal
    mark_usdt: Decimal
    value_usdt: Decimal
    share_pct: Decimal


class CurrentAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: str
    quantity: Decimal
    mark: Decimal | None
    value: Decimal | None
    state: AssetState

    @field_serializer("quantity", "mark", "value", when_used="json")
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None


class PortfolioSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str | None
    nav_usdt: Decimal
    free_usdt: Decimal
    gross_exposure_usdt: Decimal
    deployed_pct: Decimal
    open_orders_count: int
    assets: list[CurrentAsset]
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
) -> tuple[
    list[CurrentAsset],
    list[ConfiguredPosition],
    int,
    Decimal,
    int,
    dict[str, _PricedShare],
]:
    assets: list[CurrentAsset] = []
    configured_positions: list[ConfiguredPosition] = []
    external_assets_count = 0
    external_value_usdt = Decimal("0")
    unpriced_assets_count = 0
    priced_by_symbol: dict[str, _PricedShare] = {}

    for position in positions:
        if not _is_priced_position(position):
            unpriced_assets_count += 1
            assets.append(
                CurrentAsset(
                    asset=position["asset"],
                    quantity=Decimal(position["total"]),
                    mark=None,
                    value=None,
                    state="unpriced",
                )
            )
            continue
        value_usdt = Decimal(position["value_usdt"])
        mark_usdt = Decimal(position["mid_usdt"])
        share_pct = value_usdt / nav_usdt * 100 if nav_usdt > 0 else Decimal("0")
        symbol = position["symbol"]
        managed = symbol in settings.symbols
        assets.append(
            CurrentAsset(
                asset=position["asset"],
                quantity=Decimal(position["total"]),
                mark=mark_usdt,
                value=value_usdt,
                state="managed" if managed else "external",
            )
        )
        if managed:
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

    state_order = {"cash": 0, "managed": 1, "external": 2, "unpriced": 3}
    assets.sort(
        key=lambda item: (
            state_order[item.state],
            -(item.value or Decimal("0")),
            item.asset,
        )
    )
    return (
        assets,
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
    all_symbols = list(settings.symbols) + [
        s for s in getattr(settings, "vn_symbols", ()) if s not in settings.symbols
    ]
    for symbol in all_symbols:
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
        change_24h_pct: Decimal | None = None
        if latest_valid_row is not None:
            decision = latest_valid_row["decision"]
            raw_setups = decision.get("futures_setups") or []
            futures_setups = [
                DashboardFuturesSetup(
                    direction=str(s["direction"]),
                    entry=Decimal(str(s["entry"])),
                    stop=Decimal(str(s["stop"])),
                    target=Decimal(str(s["target"])),
                    risk_reward_ratio=Decimal(str(s.get("risk_reward_ratio", "1.5"))),
                    rationale=str(s.get("rationale", "")),
                )
                for s in raw_setups
            ]
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
                futures_bias=decision.get("futures_bias"),
                futures_setups=futures_setups,
            )
            evidence = _evidence_payload(latest_valid_row)
            change_24h_pct = _evidence_change_24h_pct(evidence)
            mark_usdt = priced.mark_usdt if priced else _evidence_mark_usdt(evidence)
            sparkline_closes = _evidence_sparkline_closes(evidence, limit=30)
            derivatives_indicators = _evidence_derivatives_indicators(evidence)
        else:
            change_24h_pct = None
            mark_usdt = priced.mark_usdt if priced else None
            sparkline_closes = []
            derivatives_indicators = None

        results.append(
            SymbolSection(
                symbol=symbol,
                mark_usdt=mark_usdt,
                change_24h_pct=change_24h_pct,
                position_share_pct=priced.share_pct if priced else None,
                latest_attempt=latest_attempt,
                latest_valid_decision=latest_valid_decision,
                sparkline_closes=sparkline_closes,
                derivatives_indicators=derivatives_indicators,
                committee_evaluation=_build_committee_evaluation(latest_valid_row),
            )
        )
    return results


def _evidence_payload(run: dict[str, object] | None) -> dict[str, object] | None:
    if not run:
        return None
    try:
        return json.loads((Path(str(run["report_dir"])) / "evidence.json").read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _evidence_mark_usdt(evidence: dict[str, object] | None) -> Decimal | None:
    if not evidence:
        return None
    try:
        items = evidence.get("items") or []
        spot = next(item for item in items if item.get("kind") == "spot")
        return Decimal(str(spot["payload"]["mid"]))
    except (ValueError, KeyError, TypeError, StopIteration):
        return None


def _evidence_change_24h_pct(evidence: dict[str, object] | None) -> Decimal | None:
    if not evidence:
        return None
    try:
        items = evidence.get("items") or []
        spot = next(item for item in items if item.get("kind") == "spot")
        payload = spot.get("payload") or {}
        if "change_24h_pct" in payload and payload["change_24h_pct"] is not None:
            return Decimal(str(payload["change_24h_pct"]))
        if "ref_price" in payload and "mid" in payload:
            ref = Decimal(str(payload["ref_price"]))
            mid = Decimal(str(payload["mid"]))
            if ref > 0:
                return ((mid - ref) / ref * Decimal("100")).quantize(Decimal("0.01"))
        return None
    except (ValueError, KeyError, TypeError, StopIteration):
        return None


def _evidence_sparkline_closes(
    evidence: dict[str, object] | None, limit: int = 30
) -> list[Decimal]:
    if not evidence:
        return []
    try:
        closes = evidence.get("four_hour_closes") or evidence.get("daily_closes")
        if not closes:
            items = evidence.get("items") or []
            spot = next(item for item in items if item.get("kind") == "spot")
            closes = spot.get("payload", {}).get("daily_closes", [])
        selected = closes[-limit:] if len(closes) > limit else closes
        return [Decimal(str(c)) for c in selected]
    except (ValueError, TypeError, StopIteration):
        return []


def _evidence_derivatives_indicators(
    evidence: dict[str, object] | None,
) -> DerivativesIndicators | None:
    if not evidence:
        return None
    try:

        def _dec(k: str) -> Decimal | None:
            v = evidence.get(k)
            return Decimal(str(v)) if v is not None else None

        trend = evidence.get("funding_rate_trend")
        return DerivativesIndicators(
            funding_rate=_dec("funding_rate"),
            funding_rate_trend=str(trend) if trend is not None else None,
            open_interest=_dec("open_interest"),
            oi_change_1h_pct=_dec("oi_change_1h_pct"),
            long_short_ratio=_dec("long_short_ratio"),
            top_trader_ratio=_dec("top_trader_ratio"),
            taker_buy_sell_ratio=_dec("taker_buy_sell_ratio"),
        )
    except (ValueError, TypeError):
        return None


ROLES_METADATA = {
    "technical": "Chuyên viên Kỹ thuật (4H / Daily)",
    "derivatives": "Chuyên viên Phái sinh (Binance Futures)",
    "news": "Chuyên viên Tin tức & Vĩ mô",
    "liquidity": "Chuyên viên Thanh khoản & Sổ lệnh",
    "flow": "Chuyên viên Dòng tiền & Khối ngoại",
    "fundamentals": "Chuyên viên Phân tích Cơ bản",
    "bull_round_2": "Tranh biện Bull (Phe Mua)",
    "bear_round_2": "Tranh biện Bear (Phe Bán)",
}


def _build_committee_evaluation(
    run: dict[str, object] | None,
) -> DashboardCommitteeEvaluation | None:
    if not run:
        return None

    report_dir = Path(str(run.get("report_dir", "")))
    analysts_file = report_dir / "analysts.json"

    if analysts_file.exists():
        try:
            analysts = json.loads(analysts_file.read_text())
            bull_w = Decimal("0")
            bear_w = Decimal("0")
            neut_w = Decimal("0")
            votes: list[DashboardMemberVote] = []

            for rk, rlabel in ROLES_METADATA.items():
                rep = analysts.get(rk)
                if isinstance(rep, dict):
                    st = str(rep.get("stance", "neutral")).lower()
                    cf = Decimal(str(rep.get("confidence", "5")))
                    obs_items = [str(o).strip() for o in rep.get("observations", []) if str(o).strip()]
                    risk_items = [str(r).strip() for r in rep.get("risks", []) if str(r).strip()]
                    parts: list[str] = []
                    if obs_items:
                        parts.extend([f"• {o}" for o in obs_items[:2]])
                    if risk_items:
                        parts.append(f"Rủi ro: {risk_items[0]}")
                    vote_summary = "\n".join(parts) if parts else str(rep.get("summary", "")).strip()
                    if len(vote_summary) > 450:
                        vote_summary = vote_summary[:440] + "..."

                    votes.append(
                        DashboardMemberVote(
                            role=rk,
                            label=rlabel,
                            stance=st,
                            confidence=cf,
                            summary=vote_summary,
                        )
                    )
                    if st == "bullish":
                        bull_w += cf
                    elif st == "bearish":
                        bear_w += cf
                    else:
                        neut_w += cf

            total_dir = bull_w + bear_w
            long_pct = int(round((bull_w / total_dir) * 100)) if total_dir > 0 else 50
            short_pct = 100 - long_pct

            total_all = bull_w + bear_w + neut_w
            neutral_pct = int(round((neut_w / total_all) * 100)) if total_all > 0 else 0

            if long_pct >= 60:
                rec = "LONG"
                summary = f"Hội đồng đồng thuận nghiêng về vị thế LONG ({long_pct}%). Lực mua chủ động và tín hiệu kỹ thuật/phái sinh chiếm ưu thế."
            elif short_pct >= 60:
                rec = "SHORT"
                summary = f"Hội đồng đồng thuận nghiêng về vị thế SHORT ({short_pct}%). Lực bán chủ động, cản kỹ thuật hoặc tin tức vĩ mô chiếm ưu thế."
            else:
                rec = "NEUTRAL"
                summary = f"Hội đồng đánh giá thị trường CÂN BẰNG (Long {long_pct}% / Short {short_pct}%). Tín hiệu giữa các chuyên viên đang phân kỳ, khuyến nghị thận trọng."

            return DashboardCommitteeEvaluation(
                recommendation=rec,
                long_pct=long_pct,
                short_pct=short_pct,
                neutral_pct=neutral_pct,
                summary=summary,
                member_votes=votes,
            )
        except Exception:
            pass

    decision = run.get("decision")
    if isinstance(decision, dict):
        bias = str(decision.get("futures_bias", "NEUTRAL")).upper()
        if bias == "BULLISH":
            rec = "LONG"
            long_pct = 70
            short_pct = 30
            summary = "Hội đồng đánh giá thiên hướng TĂNG (Long 70% / Short 30%)."
        elif bias == "BEARISH":
            rec = "SHORT"
            long_pct = 30
            short_pct = 70
            summary = "Hội đồng đánh giá thiên hướng GIẢM (Long 30% / Short 70%)."
        else:
            rec = "NEUTRAL"
            long_pct = 50
            short_pct = 50
            summary = "Hội đồng đánh giá thị trường TRUNG LẬP / CÂN BẰNG (Long 50% / Short 50%)."

        return DashboardCommitteeEvaluation(
            recommendation=rec,
            long_pct=long_pct,
            short_pct=short_pct,
            neutral_pct=0,
            summary=summary,
            member_votes=[],
        )

    return None


def _build_health(
    store: Store,
    now: datetime,
    symbols: list[SymbolSection],
    settings: Settings | None = None,
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

    crypto_symbols = (
        set(settings.symbols)
        if settings
        else {s.symbol for s in symbols if s.symbol.endswith("USDT")}
    )
    alerts = [
        f"research_freshness:{item.symbol}"
        for item in symbols
        if item.symbol in crypto_symbols
        and (
            item.latest_attempt is None
            or item.latest_attempt.state == "blocked"
            or item.latest_valid_decision is None
        )
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
        assets,
        configured_positions,
        external_assets_count,
        external_value_usdt,
        unpriced_assets_count,
        priced_by_symbol,
    ) = _split_positions(raw_positions, settings, nav_usdt)
    if free_usdt > 0:
        assets.insert(
            0,
            CurrentAsset(
                asset=settings.base_currency,
                quantity=free_usdt,
                mark=Decimal("1"),
                value=free_usdt,
                state="cash",
            ),
        )

    portfolio = PortfolioSection(
        as_of=as_of,
        nav_usdt=nav_usdt,
        free_usdt=free_usdt,
        gross_exposure_usdt=gross_exposure_usdt,
        deployed_pct=deployed_pct,
        open_orders_count=open_orders_count,
        assets=assets,
        configured_positions=configured_positions,
        external_assets_count=external_assets_count,
        external_value_usdt=external_value_usdt,
        unpriced_assets_count=unpriced_assets_count,
    )

    symbols = _build_symbols(settings, store, priced_by_symbol)
    health = _build_health(store, current, symbols, settings=settings)
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


def is_loopback_url(url: str) -> bool:
    return urlparse(url).hostname in _LOOPBACK_HOSTS


def publish_dashboard(
    snapshot: DashboardSnapshot,
    *,
    url: str,
    ingest_token: str,
    sites_bypass_token: str | None,
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
    }
    if sites_bypass_token:
        headers["OAI-Sites-Authorization"] = f"Bearer {sites_bypass_token}"
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
        )
        if not value
    ]
    if url and not is_loopback_url(url) and not sites_bypass_token:
        missing.append(SITES_BYPASS_TOKEN_ENV)
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


def publish_dashboard_if_configured(
    settings: Settings,
    *,
    store_factory: Callable[..., Store] = Store,
) -> str | None:
    """Best-effort publish after a state-changing command."""
    if not os.getenv(DASHBOARD_INGEST_URL_ENV):
        return None
    store = None
    try:
        store = store_factory(settings.database)
        snapshot = build_dashboard_snapshot(settings, store)
        publish_dashboard_from_env(snapshot, strict=False)
        return None
    except Exception as exc:  # noqa: BLE001 - intentionally best-effort
        return f"Warning: dashboard publish failed ({type(exc).__name__})"
    finally:
        if store is not None:
            store.close()
