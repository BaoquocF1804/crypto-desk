from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from .config import Settings
from .data import EvidenceBuilder, EvidenceError, EvidenceSnapshot
from .domain import (
    PortfolioSnapshot,
    ResearchDecision,
    iso,
    to_jsonable,
    utcnow,
)
from .execution import ExecutionService
from .risk import build_ticket, size_buy, size_sell
from .screener import ScreenResult, screen
from .store import Store


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    run_id: str
    cutoff: str
    decision: ResearchDecision
    report_dir: Path
    ticket_id: str | None = None


class CryptoDeskService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        broker: Any | None = None,
        evidence_builder: EvidenceBuilder | Any | None = None,
        committee: Any | None = None,
        execution: ExecutionService | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self.store = store
        self.broker = broker
        self.evidence_builder = evidence_builder
        self.committee = committee
        self.execution = execution
        self.now = now

    def sync(self) -> PortfolioSnapshot:
        if self.broker is None:
            raise ValueError("Binance broker is required for sync")
        snapshot = self.broker.account_snapshot()
        self.store.save_snapshot(snapshot)
        return snapshot

    def screen(self, cutoff: datetime | None = None) -> list[ScreenResult]:
        builder = self._require_builder()
        effective_cutoff = self._aware(cutoff or self._now())
        results: list[ScreenResult] = []
        for symbol in self.settings.symbols:
            try:
                snapshot = builder.build(symbol, effective_cutoff)
                result = screen(
                    snapshot,
                    allowlist=self.settings.symbols,
                )
            except EvidenceError as exc:
                result = ScreenResult(
                    symbol=symbol,
                    passes=False,
                    score=Decimal("0"),
                    reasons=(f"evidence:{exc}",),
                    evidence_ids=(),
                )
            except Exception as exc:
                result = ScreenResult(
                    symbol=symbol,
                    passes=False,
                    score=Decimal("0"),
                    reasons=(f"evidence_provider:{type(exc).__name__}",),
                    evidence_ids=(),
                )
            results.append(result)
        return sorted(results, key=lambda item: item.score, reverse=True)

    def analyze(
        self,
        symbol: str,
        cutoff: datetime | None = None,
    ) -> AnalysisRun:
        symbol = symbol.upper()
        if symbol not in self.settings.symbols:
            raise ValueError("Symbol is outside the configured allowlist")
        builder = self._require_builder()
        effective_cutoff = self._aware(cutoff or self._now())
        snapshot: EvidenceSnapshot | None = None
        reports: dict[str, Any] = {}
        evidence_payload: Any
        try:
            snapshot = builder.build(symbol, effective_cutoff)
        except EvidenceError as exc:
            decision = self._no_trade(symbol, str(exc))
            evidence_payload = {
                "symbol": symbol,
                "cutoff": iso(effective_cutoff),
                "error": str(exc),
            }
        except Exception as exc:
            reason = f"evidence_provider:{type(exc).__name__}"
            decision = self._no_trade(symbol, reason)
            evidence_payload = {
                "symbol": symbol,
                "cutoff": iso(effective_cutoff),
                "error": reason,
            }
        else:
            evidence_payload = snapshot
            committee = self._require_committee()
            reflections = tuple(
                json.dumps(item["payload"], ensure_ascii=False)
                for item in self.store.list_reflections(symbol)[:5]
            )
            try:
                committee_result = committee.run(
                    snapshot,
                    reflections=reflections,
                    position_quantity=self._position_quantity(symbol),
                )
                decision = committee_result.decision
                reports = committee_result.reports
            except Exception as exc:
                decision = self._no_trade(
                    symbol,
                    f"committee:{type(exc).__name__}",
                )

        run_id = str(uuid.uuid4())
        report_dir = self.settings.artifacts / effective_cutoff.date().isoformat() / run_id
        report_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(report_dir / "evidence.json", evidence_payload)
        self._write_json(
            report_dir / "analysts.json",
            {name: self._report_payload(report) for name, report in reports.items()},
        )
        self._write_json(report_dir / "decision.json", decision)
        (report_dir / "report.md").write_text(
            self._markdown_report(
                decision=decision,
                cutoff=effective_cutoff,
                reports=reports,
                snapshot=snapshot,
            ),
            encoding="utf-8",
        )
        self.store.save_run(
            run_id,
            iso(effective_cutoff),
            decision,
            report_dir,
        )
        ticket_id = self._create_ticket(decision, snapshot, effective_cutoff)
        return AnalysisRun(
            run_id=run_id,
            cutoff=iso(effective_cutoff),
            decision=decision,
            report_dir=report_dir,
            ticket_id=ticket_id,
        )

    def daily(
        self,
        *,
        due: bool = False,
        catch_up: bool = False,
    ) -> dict[str, Any]:
        now = self._now()
        target_date = now.date()
        if now.time() < time(0, 15):
            target_date -= timedelta(days=1)
            if due and not catch_up:
                return {"status": "NOT_DUE", "bucket": str(target_date)}
        if catch_up:
            for days_ago in range(31):
                candidate = target_date - timedelta(days=days_ago)
                if not self.store.scheduled_done(
                    "daily",
                    candidate.isoformat(),
                ):
                    target_date = candidate
                    break
        bucket = target_date.isoformat()
        if self.store.scheduled_done("daily", bucket):
            return {"status": "ALREADY_DONE", "bucket": bucket}

        cutoff = datetime.combine(target_date, time(0, 15), tzinfo=UTC)
        screen_results = self.screen(cutoff)
        runs: list[str] = []
        for result in screen_results:
            if result.passes:
                runs.append(self.analyze(result.symbol, cutoff).run_id)
        self.store.mark_scheduled("daily", bucket)
        return {
            "status": "COMPLETED",
            "bucket": bucket,
            "screen": [to_jsonable(result) for result in screen_results],
            "run_ids": runs,
        }

    def health(self, *, due: bool = False) -> dict[str, Any]:
        now = self._now()
        minute = (now.minute // 15) * 15
        bucket_time = now.replace(minute=minute, second=0, microsecond=0)
        bucket = bucket_time.strftime("%Y-%m-%dT%H:%MZ")
        if self.store.scheduled_done("health", bucket):
            return {"status": "ALREADY_DONE", "bucket": bucket}

        try:
            snapshot = self.sync()
        except Exception as exc:
            alert = f"broker_sync:{type(exc).__name__}"
            self.store.mark_scheduled("health", bucket)
            return {
                "status": "COMPLETED",
                "bucket": bucket,
                "alerts": [alert],
                "run_ids": [],
                "reconciled": [],
                "snapshot": None,
            }
        alerts, symbols = self._health_alerts(snapshot, now)
        reconciled: list[dict[str, Any]] = []
        if self.execution is not None:
            for submission in self.store.list_submissions(
                (
                    "SUBMITTING",
                    "SUBMISSION_UNKNOWN",
                    "RECONCILE_REQUIRED",
                    "SUBMITTED",
                    "PROTECTION_CANCELED",
                    "EXIT_SUBMITTING",
                )
            ):
                try:
                    result = self.execution.reconcile(submission["ticket_id"])
                    reconciled.append(to_jsonable(result))
                except Exception as exc:
                    alerts.append(f"reconcile:{submission['ticket_id']}:{type(exc).__name__}")

        runs = [
            self.analyze(symbol, now).run_id
            for symbol in sorted(symbols)
            if symbol in self.settings.symbols
        ]
        self.store.mark_scheduled("health", bucket)
        return {
            "status": "COMPLETED",
            "bucket": bucket,
            "alerts": alerts,
            "run_ids": runs,
            "reconciled": reconciled,
            "snapshot": to_jsonable(snapshot),
        }

    def save_reflection(
        self,
        *,
        run_id: str,
        symbol: str,
        entry: Decimal,
        closes: tuple[Decimal, ...],
        benchmark_closes: tuple[Decimal, ...],
    ) -> dict[str, Decimal]:
        if len(closes) < 21:
            raise ValueError("Reflection requires 20 completed daily periods")
        payload = calculate_reflection(
            entry=entry,
            closes=closes,
            benchmark_closes=benchmark_closes,
        )
        if not self.store.save_reflection(
            run_id,
            symbol,
            to_jsonable(payload),
        ):
            raise ValueError(f"Reflection already exists for {run_id}")
        return payload

    def _health_alerts(
        self,
        snapshot: PortfolioSnapshot,
        now: datetime,
    ) -> tuple[list[str], set[str]]:
        alerts: list[str] = []
        symbols: set[str] = set()
        if snapshot.nav_usdt <= 0:
            return ["invalid_nav"], symbols
        gross = sum(
            (Decimal(position.get("value_usdt", "0")) for position in snapshot.positions),
            Decimal("0"),
        )
        if gross > self.settings.risk.max_gross * snapshot.nav_usdt:
            alerts.append("max_gross")
        if snapshot.free_usdt < self.settings.risk.min_usdt_reserve * snapshot.nav_usdt:
            alerts.append("usdt_reserve")

        protected_symbols = {
            str(order.get("symbol"))
            for order in snapshot.open_orders
            if str(order.get("side", "")).upper() == "SELL"
        }
        stop_prices: dict[str, list[Decimal]] = {}
        for order in snapshot.open_orders:
            if str(order.get("side", "")).upper() != "SELL":
                continue
            raw_stop = order.get("stopPrice") or order.get("stop_price")
            if raw_stop is None or Decimal(str(raw_stop)) <= 0:
                continue
            stop_prices.setdefault(str(order.get("symbol")), []).append(Decimal(str(raw_stop)))
        for position in snapshot.positions:
            if position.get("unpriced"):
                alerts.append(f"unpriced_asset:{position.get('asset')}")
                continue
            symbol = str(position.get("symbol", ""))
            value = Decimal(position.get("value_usdt", "0"))
            if not symbol or value <= 0:
                continue
            if value > self.settings.risk.max_symbol * snapshot.nav_usdt:
                alerts.append(f"max_symbol:{symbol}")
                symbols.add(symbol)
            if symbol not in protected_symbols:
                alerts.append(f"missing_protection:{symbol}")
                symbols.add(symbol)
            elif self.broker is not None and stop_prices.get(symbol):
                try:
                    quote = self.broker.latest_quote(symbol)
                    if quote.mid <= max(stop_prices[symbol]):
                        alerts.append(f"stop_breach:{symbol}")
                        symbols.add(symbol)
                except Exception as exc:
                    alerts.append(f"quote:{symbol}:{type(exc).__name__}")
                    symbols.add(symbol)
            latest = self.store.latest_run(symbol)
            if latest is None:
                alerts.append(f"missing_thesis:{symbol}")
                symbols.add(symbol)
            else:
                cutoff = datetime.fromisoformat(latest["cutoff"]).astimezone(UTC)
                if now - cutoff > timedelta(days=5):
                    alerts.append(f"stale_thesis:{symbol}")
                    symbols.add(symbol)
                if self._run_evidence_stale(latest, now):
                    alerts.append(f"stale_evidence:{symbol}")
                    symbols.add(symbol)
        return alerts, symbols

    @staticmethod
    def _run_evidence_stale(
        run: dict[str, Any],
        now: datetime,
    ) -> bool:
        path = Path(run["report_dir"]) / "evidence.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            if payload.get("error") or not items:
                return True
            for item in items:
                if item.get("stale"):
                    return True
                as_of = datetime.fromisoformat(item["as_of"]).astimezone(UTC)
                if now - as_of > timedelta(hours=26):
                    return True
        except (OSError, ValueError, KeyError, TypeError):
            return True
        return False

    def _position_quantity(self, symbol: str) -> Decimal:
        snapshot = self.store.latest_snapshot(self.settings.binance.environment)
        if snapshot is None:
            return Decimal("0")
        return sum(
            (
                Decimal(position.get("total", position.get("quantity", "0")))
                for position in snapshot.positions
                if position.get("symbol") == symbol
            ),
            Decimal("0"),
        )

    def _create_ticket(
        self,
        decision: ResearchDecision,
        evidence: EvidenceSnapshot | None,
        cutoff: datetime,
    ) -> str | None:
        if decision.action not in {"ACCUMULATE", "REDUCE", "EXIT"} or evidence is None:
            return None
        now = self._now()
        if cutoff > now or now - cutoff > timedelta(minutes=5):
            return None
        portfolio = self.store.latest_snapshot(self.settings.binance.environment)
        if portfolio is None or portfolio.environment != self.settings.binance.environment:
            return None
        if decision.action == "ACCUMULATE" and any(
            position.get("unpriced") for position in portfolio.positions
        ):
            return None
        portfolio_as_of = datetime.fromisoformat(portfolio.as_of).astimezone(UTC)
        if portfolio_as_of > now or now - portfolio_as_of > timedelta(minutes=5):
            return None
        assert decision.entry is not None
        assert decision.stop is not None
        current_gross = sum(
            (Decimal(position.get("value_usdt", "0")) for position in portfolio.positions),
            Decimal("0"),
        )
        current_symbol = sum(
            (
                Decimal(position.get("value_usdt", "0"))
                for position in portfolio.positions
                if position.get("symbol") == decision.symbol
            ),
            Decimal("0"),
        )
        current_quantity = self._position_quantity(decision.symbol)
        try:
            risk_snapshot: dict[str, Any] = {
                "portfolio_as_of": portfolio.as_of,
            }
            if decision.action == "ACCUMULATE":
                sizing = size_buy(
                    environment=self.settings.binance.environment,
                    nav_usdt=portfolio.nav_usdt,
                    free_usdt=portfolio.free_usdt,
                    entry=decision.entry,
                    stop=decision.stop,
                    current_symbol_value=current_symbol,
                    current_gross_value=current_gross,
                    mainnet_order_cap_usdt=self.settings.risk.mainnet_initial_order_cap_usdt,
                    completed_mainnet_chains=self.store.completed_mainnet_chains(),
                    risk=self.settings.risk,
                    rules=evidence.rules,
                )
            else:
                protection_ids = {
                    str(order["orderListId"])
                    for order in portfolio.open_orders
                    if order.get("symbol") == decision.symbol
                    and str(order.get("side", "")).upper() == "SELL"
                    and int(order.get("orderListId", -1)) >= 0
                }
                if len(protection_ids) != 1:
                    return None
                risk_snapshot["protection_list_client_order_id"] = protection_ids.pop()
                sizing = size_sell(
                    action=decision.action,
                    free_base=current_quantity,
                    price=decision.entry,
                    rules=evidence.rules,
                )
            ticket = build_ticket(
                environment=self.settings.binance.environment,
                decision=decision,
                quantity=sizing.quantity,
                current_position_quantity=current_quantity,
                rules=evidence.rules,
                risk_snapshot={
                    **risk_snapshot,
                    "limiting_rule": sizing.limiting_rule,
                    "rooms": sizing.rooms,
                },
                ttl_minutes=self.settings.risk.ticket_ttl_minutes,
            )
        except ValueError:
            return None
        self.store.save_ticket(ticket)
        return ticket.id

    def _require_builder(self) -> Any:
        if self.evidence_builder is None:
            raise ValueError("Evidence builder is not configured")
        return self.evidence_builder

    def _require_committee(self) -> Any:
        if self.committee is None:
            raise ValueError("Research committee is not configured")
        return self.committee

    def _now(self) -> datetime:
        return self._aware(self.now())

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(
            json.dumps(
                to_jsonable(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _report_payload(report: Any) -> Any:
        if isinstance(report, BaseModel):
            return report.model_dump(mode="json")
        return to_jsonable(report)

    def _markdown_report(
        self,
        *,
        decision: ResearchDecision,
        cutoff: datetime,
        reports: dict[str, Any],
        snapshot: EvidenceSnapshot | None,
    ) -> str:
        def price(value: Decimal | None) -> str:
            return str(value) if value is not None else "N/A"

        lines = [
            f"# Crypto Desk — {decision.symbol}",
            "",
            f"- Thời điểm: {iso(cutoff)}",
            f"- Môi trường: {self.settings.binance.environment}",
            f"- Quyết định: **{decision.action}**",
            f"- Conviction: {decision.conviction}/10",
            f"- Entry / Stop / Target: {price(decision.entry)} / "
            f"{price(decision.stop)} / {price(decision.target)}",
            f"- Lý do: {decision.reason}",
            "",
            "## Luận điểm",
            "",
            f"- Bull: {decision.bull_case}",
            f"- Bear: {decision.bear_case}",
            f"- Invalidation: {decision.invalidation}",
            "",
            "## Báo cáo hội đồng",
            "",
        ]
        for name, report in reports.items():
            lines.extend(
                [
                    f"### {name}",
                    "",
                    "```json",
                    json.dumps(
                        self._report_payload(report),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "## Evidence",
                "",
                ", ".join(decision.evidence_ids) or "Không có evidence hợp lệ.",
                "",
            ]
        )
        if snapshot is not None:
            lines.append(
                f"Giá Binance/CoinGecko: {snapshot.binance_mid} / {snapshot.reference_usdt}."
            )
        if decision.action == "NO_TRADE":
            lines.extend(
                [
                    "",
                    f"> **NO_TRADE:** {decision.reason}",
                ]
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _no_trade(symbol: str, reason: str) -> ResearchDecision:
        return ResearchDecision(
            symbol=symbol,
            action="NO_TRADE",
            conviction=Decimal("0"),
            bull_case="Không có evidence hợp lệ để lập luận.",
            bear_case="Dữ liệu không đạt freshness/consistency contract.",
            catalysts=(),
            invalidation="Chạy lại khi evidence hợp lệ.",
            entry=None,
            stop=None,
            target=None,
            evidence_ids=(),
            reason=reason,
        )


def calculate_reflection(
    *,
    entry: Decimal,
    closes: tuple[Decimal, ...],
    benchmark_closes: tuple[Decimal, ...],
) -> dict[str, Decimal]:
    if entry <= 0 or not closes:
        raise ValueError("Reflection requires positive entry and closes")
    returns = tuple(close / entry - Decimal("1") for close in closes)
    realized_return = closes[-1] / entry - Decimal("1")
    if benchmark_closes:
        if benchmark_closes[0] <= 0:
            raise ValueError("Benchmark start must be positive")
        benchmark_return = benchmark_closes[-1] / benchmark_closes[0] - Decimal("1")
    else:
        benchmark_return = Decimal("0")
    return {
        "realized_return": realized_return,
        "maximum_adverse_excursion": min(returns),
        "maximum_favorable_excursion": max(returns),
        "benchmark_return": benchmark_return,
        "alpha": realized_return - benchmark_return,
    }
