from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from .config import BENCHMARK_SYMBOL, REFLECTION_HORIZON_DAYS, Settings
from .data import EvidenceBuilder, EvidenceError, EvidenceSnapshot
from .domain import (
    PortfolioSnapshot,
    ResearchDecision,
    format_pct,
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
    current_price: Decimal | None
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
                snapshot = builder.build(symbol, effective_cutoff, live=cutoff is None)
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
        llm_payload: dict[str, Any] = {
            "provider": self.settings.models.provider,
            "quick_model": self.settings.models.quick,
            "deep_model": self.settings.models.deep,
            "quick_thinking": self.settings.models.quick_thinking,
            "deep_thinking": self.settings.models.deep_thinking,
            "calls": [],
        }
        evidence_payload: Any
        prior_thesis, prior_run_id = self._build_prior_thesis(symbol, None, effective_cutoff)
        try:
            snapshot = builder.build(symbol, effective_cutoff, live=cutoff is None)
        except EvidenceError as exc:
            decision = self._no_trade(symbol, str(exc), prior_run_id=prior_run_id)
            evidence_payload = {
                "symbol": symbol,
                "cutoff": iso(effective_cutoff),
                "error": str(exc),
            }
        except Exception as exc:
            reason = f"evidence_provider:{type(exc).__name__}"
            decision = self._no_trade(symbol, reason, prior_run_id=prior_run_id)
            evidence_payload = {
                "symbol": symbol,
                "cutoff": iso(effective_cutoff),
                "error": reason,
            }
        else:
            evidence_payload = snapshot
            prior_thesis, prior_run_id = self._build_prior_thesis(
                symbol, snapshot, effective_cutoff
            )
            committee = self._require_committee()
            reflections = tuple(
                render_reflection(item) for item in self.store.list_reflections(symbol)[:5]
            )
            try:
                try:
                    committee_result = committee.run(
                        snapshot,
                        reflections=reflections,
                        prior_thesis=prior_thesis,
                        position_quantity=self._position_quantity(symbol),
                    )
                except TypeError as exc:
                    if "prior_thesis" in str(exc):
                        committee_result = committee.run(
                            snapshot,
                            reflections=reflections,
                            position_quantity=self._position_quantity(symbol),
                        )
                    else:
                        raise
                decision = committee_result.decision
                if prior_run_id and decision.prior_run_id is None:
                    decision = replace(decision, prior_run_id=prior_run_id)
                reports = committee_result.reports
                llm_payload["calls"] = to_jsonable(getattr(committee_result, "calls", ()))
            except Exception as exc:
                decision = self._no_trade(
                    symbol,
                    f"committee:{type(exc).__name__}",
                    prior_run_id=prior_run_id,
                )

        run_id = str(uuid.uuid4())
        report_dir = self.settings.artifacts / effective_cutoff.date().isoformat() / run_id
        report_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(report_dir / "evidence.json", evidence_payload)
        if prior_thesis:
            self._write_json(report_dir / "prior_thesis.json", prior_thesis)
        self._write_json(
            report_dir / "analysts.json",
            {name: self._report_payload(report) for name, report in reports.items()},
        )
        self._write_json(report_dir / "llm.json", llm_payload)
        self._write_json(report_dir / "decision.json", decision)
        (report_dir / "report.md").write_text(
            self._markdown_report(
                decision=decision,
                cutoff=effective_cutoff,
                reports=reports,
                snapshot=snapshot,
                prior_thesis=prior_thesis,
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
            current_price=snapshot.binance_mid if snapshot else None,
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
        reflection_run_ids = self.refresh_reflections(cutoff)
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
            "reflection_run_ids": reflection_run_ids,
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
        decision_action: str | None = None,
    ) -> dict[str, Any]:
        if len(closes) < REFLECTION_HORIZON_DAYS:
            raise ValueError(
                f"Reflection requires {REFLECTION_HORIZON_DAYS} completed daily periods"
            )
        payload: dict[str, Any] = calculate_reflection(
            entry=entry,
            closes=closes,
            benchmark_closes=benchmark_closes,
        )
        if decision_action is not None:
            payload["decision_action"] = decision_action
        if not self.store.save_reflection(
            run_id,
            symbol,
            to_jsonable(payload),
        ):
            raise ValueError(f"Reflection already exists for {run_id}")
        return payload

    def refresh_reflections(self, cutoff: datetime) -> list[str]:
        builder = self._require_builder()
        completed_before = iso(self._aware(cutoff) - timedelta(days=REFLECTION_HORIZON_DAYS))
        saved: list[str] = []
        for run in self.store.unreflected_runs(completed_before):
            if not run["decision"].get("evidence_ids"):
                continue
            try:
                evidence = json.loads(
                    (Path(run["report_dir"]) / "evidence.json").read_text(encoding="utf-8")
                )
                entry = Decimal(str(evidence["binance_mid"]))
                run_cutoff = datetime.fromisoformat(run["cutoff"]).astimezone(UTC)
                start = (run_cutoff + timedelta(days=1)).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                closes = builder.reflection_closes(run["symbol"], start)
                benchmark = (
                    ()
                    if run["symbol"] == BENCHMARK_SYMBOL
                    else builder.reflection_closes(BENCHMARK_SYMBOL, start)
                )
                self.save_reflection(
                    run_id=run["id"],
                    symbol=run["symbol"],
                    entry=entry,
                    closes=closes,
                    benchmark_closes=benchmark,
                    decision_action=str(run["decision"]["action"]),
                )
            except (EvidenceError, OSError, ValueError, KeyError, TypeError):
                continue
            saved.append(run["id"])
        return saved

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
        # Only the configured allowlist is actively researched/protected. On
        # testnet the account holds hundreds of unmanaged external + unpriced
        # dust assets; emitting a per-asset alert for each floods every alert
        # sink (Telegram). Aggregate those into one count each instead.
        configured = set(self.settings.symbols)
        unpriced_count = 0
        external_count = 0
        for position in snapshot.positions:
            if position.get("unpriced"):
                unpriced_count += 1
                continue
            symbol = str(position.get("symbol", ""))
            value = Decimal(position.get("value_usdt", "0"))
            if not symbol or value <= 0:
                continue
            if symbol not in configured:
                external_count += 1
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
        if external_count:
            alerts.append(f"external_unmanaged:{external_count}")
        if unpriced_count:
            alerts.append(f"unpriced_assets:{unpriced_count}")
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
        if decision.entry is None or decision.stop is None:
            return None
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

    def _build_prior_thesis(
        self,
        symbol: str,
        snapshot: EvidenceSnapshot | None,
        cutoff: datetime,
    ) -> tuple[dict[str, Any] | None, str | None]:
        prior_run = self.store.latest_valid_run(symbol, before_cutoff=iso(cutoff))
        if not prior_run:
            return None, None

        prior_id = str(prior_run["id"])
        prior_decision = prior_run.get("decision", {})
        prior_cutoff_str = prior_run.get("cutoff")
        hours_ago = Decimal("0")
        if prior_cutoff_str:
            try:
                prior_dt = datetime.fromisoformat(prior_cutoff_str).astimezone(UTC)
                diff = (cutoff - prior_dt).total_seconds() / 3600
                hours_ago = Decimal(str(round(max(0.0, diff), 1)))
            except Exception:
                pass

        prior_price: Decimal | None = None
        report_dir_str = prior_run.get("report_dir")
        if report_dir_str:
            ev_path = Path(report_dir_str) / "evidence.json"
            if ev_path.exists():
                try:
                    ev_data = json.loads(ev_path.read_text(encoding="utf-8"))
                    if ev_data.get("binance_mid") is not None:
                        prior_price = Decimal(str(ev_data["binance_mid"]))
                except Exception:
                    pass
        if prior_price is None and prior_decision.get("entry") is not None:
            try:
                prior_price = Decimal(str(prior_decision["entry"]))
            except Exception:
                pass

        current_mid = snapshot.binance_mid if snapshot else None
        price_change_pct: Decimal | None = None
        if prior_price and current_mid and prior_price > Decimal("0"):
            price_change_pct = (
                (current_mid - prior_price) / prior_price * Decimal("100")
            ).quantize(Decimal("0.01"))

        prior_entry = (
            Decimal(str(prior_decision["entry"]))
            if prior_decision.get("entry") is not None
            else None
        )
        prior_stop = (
            Decimal(str(prior_decision["stop"])) if prior_decision.get("stop") is not None else None
        )
        prior_target = (
            Decimal(str(prior_decision["target"]))
            if prior_decision.get("target") is not None
            else None
        )

        prior_payload: dict[str, Any] = {
            "run_id": prior_id,
            "cutoff": prior_cutoff_str,
            "hours_ago": str(hours_ago),
            "action": str(prior_decision.get("action", "NO_TRADE")),
            "conviction": str(prior_decision.get("conviction", "0")),
            "prior_price": str(prior_price) if prior_price is not None else None,
            "current_price": str(current_mid) if current_mid is not None else None,
            "price_change_pct": str(price_change_pct) if price_change_pct is not None else None,
            "bull_case": str(prior_decision.get("bull_case", ""))[:500],
            "bear_case": str(prior_decision.get("bear_case", ""))[:500],
            "catalysts": [str(c)[:100] for c in prior_decision.get("catalysts", [])][:3],
            "invalidation": str(prior_decision.get("invalidation", ""))[:300],
            "entry": str(prior_entry) if prior_entry is not None else None,
            "stop": str(prior_stop) if prior_stop is not None else None,
            "target": str(prior_target) if prior_target is not None else None,
            "futures_bias": prior_decision.get("futures_bias"),
        }
        return prior_payload, prior_id

    def _markdown_report(
        self,
        *,
        decision: ResearchDecision,
        cutoff: datetime,
        reports: dict[str, Any],
        snapshot: EvidenceSnapshot | None,
        prior_thesis: dict[str, Any] | None = None,
    ) -> str:
        def price(value: Decimal | None) -> str:
            return str(value) if value is not None else "N/A"

        lines = [
            f"# Crypto Desk — {decision.symbol}",
            "",
            f"- Thời điểm: {iso(cutoff)}",
            f"- Môi trường: {self.settings.binance.environment}",
            f"- LLM: {self.settings.models.provider} "
            f"({self.settings.models.quick} / {self.settings.models.deep})",
            f"- Quyết định: **{decision.action}**",
            f"- Conviction: {decision.conviction}/10",
            f"- Entry / Stop / Target: {price(decision.entry)} / "
            f"{price(decision.stop)} / {price(decision.target)}",
            f"- Lý do: {decision.reason}",
        ]
        if decision.thesis_continuity and decision.thesis_continuity != "NEW":
            lines.append(f"- Kế thừa luận điểm (Continuity): **{decision.thesis_continuity}**")
        if decision.prior_run_id:
            lines.append(f"- Tham chiếu phân tích trước: `{decision.prior_run_id}`")

        lines.extend(
            [
                "",
                "## Luận điểm",
                "",
                f"- Bull: {decision.bull_case}",
                f"- Bear: {decision.bear_case}",
                f"- Invalidation: {decision.invalidation}",
                "",
            ]
        )

        if prior_thesis:
            lines.extend(
                [
                    "## Đối soát Luận điểm Trước (Thesis Tracking)",
                    "",
                    f"- **Trạng thái tiếp nối:** **{decision.thesis_continuity}**",
                    f"- **Lần phân tích trước:** `{prior_thesis.get('cutoff')}` ({prior_thesis.get('hours_ago')}h trước)",
                    f"- **Mã tham chiếu (Run ID):** `{prior_thesis.get('run_id')}`",
                    f"- **Quyết định trước:** `{prior_thesis.get('action')}` (Conviction: {prior_thesis.get('conviction')}/10)",
                ]
            )
            if prior_thesis.get("prior_price") and prior_thesis.get("current_price"):
                chg = prior_thesis.get("price_change_pct")
                chg_str = (
                    f" ({'+' if chg and not str(chg).startswith(('-', '+')) else ''}{chg}%)"
                    if chg is not None
                    else ""
                )
                lines.append(
                    f"- **Biến động giá:** Từ `{prior_thesis.get('prior_price')}` đến `{prior_thesis.get('current_price')}`{chg_str}"
                )
            if prior_thesis.get("invalidation"):
                lines.append(f"- **Ngưỡng vô hiệu trước:** {prior_thesis.get('invalidation')}")
            lines.append("")

        if decision.futures_bias or decision.futures_setups:
            lines.extend(
                [
                    "## Kịch bản giao dịch Phái sinh (Futures Setups — Tham khảo, Non-executing)",
                    "",
                    "> **LƯU Ý:** Các kịch bản dưới đây chỉ phục vụ nghiên cứu và lập kế hoạch giao dịch cá nhân. Hệ thống **tuyệt đối không gọi lệnh** (non-executing) ra sàn.",
                    "",
                    f"- **Thiên hướng Futures (Bias):** **{decision.futures_bias or 'NEUTRAL'}**",
                    "",
                ]
            )
            if snapshot is not None:
                lines.extend(
                    [
                        "### Chỉ số phái sinh chính",
                        "",
                        f"- Funding Rate: `{snapshot.funding_rate}` ({getattr(snapshot, 'funding_rate_trend', 'stable')})",
                        f"- Open Interest: `{snapshot.open_interest}`"
                        + (
                            f" (Biến động 1h: `{snapshot.oi_change_1h_pct:+.2f}%`)"
                            if getattr(snapshot, "oi_change_1h_pct", None) is not None
                            else ""
                        ),
                        f"- Tỷ lệ Long/Short toàn cầu (Đám đông): `{getattr(snapshot, 'long_short_ratio', None) or 'N/A'}`",
                        f"- Tỷ lệ Long/Short Top Trader (Cá mập): `{getattr(snapshot, 'top_trader_ratio', None) or 'N/A'}`",
                        f"- Tỷ lệ Taker Mua/Bán: `{getattr(snapshot, 'taker_buy_sell_ratio', None) or 'N/A'}`",
                        "",
                    ]
                )
            if decision.futures_setups:
                lines.extend(
                    [
                        "### Thiết lập Entry / SL / TP",
                        "",
                        "| Hướng | Entry (USDT) | Stop Loss (USDT) | Take Profit (USDT) | R:R | Luận điểm |",
                        "| :--- | :--- | :--- | :--- | :--- | :--- |",
                    ]
                )
                for setup in decision.futures_setups:
                    lines.append(
                        f"| **{setup.direction}** | `{setup.entry}` | `{setup.stop}` | `{setup.target}` | `{setup.risk_reward_ratio}` | {setup.rationale} |"
                    )
                lines.append("")

        lines.extend(
            [
                "## Báo cáo hội đồng",
                "",
            ]
        )
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
    def _no_trade(
        symbol: str,
        reason: str,
        prior_run_id: str | None = None,
    ) -> ResearchDecision:
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
            thesis_continuity="NEW",
            prior_run_id=prior_run_id,
        )


def render_reflection(item: dict[str, Any]) -> str:
    """Một dòng tiếng Việt cho prompt: quyết định nào, đo trong bao lâu, kết quả ra sao.

    Trước đây reflection vào prompt dưới dạng ``json.dumps(payload)`` — một túi
    số không nhãn. Model không biết cửa sổ đo dài bao nhiêu nên đọc một con số
    âm trên 20 ngày như bằng chứng luận điểm sai, kể cả khi luận điểm viết cho
    horizon dài hơn. Dòng này nói thẳng cửa sổ đo.

    Dòng alpha bị bỏ khi symbol chính là benchmark: alpha của nó luôn bằng 0
    theo cấu tạo, in ra sẽ đọc như "không tạo được lợi thế" thay vì "không áp
    dụng".
    """
    payload = item["payload"]
    symbol = item["symbol"]
    action = payload.get("decision_action") or "KHÔNG RÕ"
    parts = [
        f"{item['created_at'][:10]}",
        symbol,
        f"quyết định {action}",
        f"sau {REFLECTION_HORIZON_DAYS} ngày: lợi nhuận "
        f"{format_pct(payload['realized_return'])}",
    ]
    if symbol != BENCHMARK_SYMBOL:
        parts.append(f"alpha so với {BENCHMARK_SYMBOL} {format_pct(payload['alpha'])}")
    parts.append(f"sụt sâu nhất {format_pct(payload['maximum_adverse_excursion'])}")
    return " | ".join(parts)


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
        alpha = realized_return - benchmark_return
    else:
        benchmark_return = Decimal("0")
        alpha = Decimal("0")
    return {
        "realized_return": realized_return,
        "maximum_adverse_excursion": min(returns),
        "maximum_favorable_excursion": max(returns),
        "benchmark_return": benchmark_return,
        "alpha": alpha,
    }
