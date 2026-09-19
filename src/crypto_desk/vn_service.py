"""Service điều phối quy trình phân tích và reflection cho cổ phiếu VN."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Callable
import uuid

from pydantic import BaseModel

from .config import REFLECTION_HORIZON_DAYS, VN_BENCHMARK_SYMBOL, Settings
from .data import EvidenceError, _aware
from .domain import ResearchDecision, is_decided, iso, to_jsonable, utcnow
from .service import AnalysisRun, calculate_reflection, render_reflection
from .store import Store
from .vn_data import SSIClient, VNEvidenceBuilder, VNEvidenceSnapshot
from .vn_fundamentals import load_fundamentals


class VNDeskService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        evidence_builder: Any | None = None,
        committee: Any | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self.store = store
        self.evidence_builder = evidence_builder
        self.committee = committee
        self.now = now

    def _require_builder(self) -> Any:
        if self.evidence_builder is None:
            return VNEvidenceBuilder(SSIClient(self.settings.vn_news_feeds, now=self.now))
        return self.evidence_builder

    def _require_committee(self) -> Any:
        if self.committee is None:
            raise ValueError("Research committee is not configured")
        return self.committee

    def _now(self) -> datetime:
        return _aware(self.now())

    def analyze(
        self,
        symbol: str,
        cutoff: datetime | None = None,
    ) -> AnalysisRun:
        symbol = symbol.upper()
        if symbol not in self.settings.vn_symbols:
            raise ValueError("Symbol is outside the configured VN allowlist")
        builder = self._require_builder()
        effective_cutoff = _aware(cutoff or self._now())
        snapshot: VNEvidenceSnapshot | None = None
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
        skipped_specialists: tuple[str, ...] = ()
        try:
            snapshot = builder.build(symbol, effective_cutoff)
            # Tải báo cáo tài chính nếu có
            fund_item = load_fundamentals(
                symbol, snapshot.industry, self.settings.fundamentals_dir
            )
            if fund_item is not None:
                snapshot = dataclasses.replace(
                    snapshot,
                    items=(*snapshot.items, fund_item),
                )
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
                render_reflection(item) for item in self.store.list_reflections(symbol)[:5]
            )
            try:
                committee_result = committee.run(
                    snapshot,
                    reflections=reflections,
                )
                decision = committee_result.decision
                reports = committee_result.reports
                skipped_specialists = getattr(committee_result, "skipped_specialists", ())
                llm_payload["calls"] = to_jsonable(getattr(committee_result, "calls", ()))
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
        self._write_json(report_dir / "llm.json", llm_payload)
        self._write_json(report_dir / "decision.json", decision)
        (report_dir / "report.md").write_text(
            self._markdown_report(
                decision=decision,
                cutoff=effective_cutoff,
                reports=reports,
                snapshot=snapshot,
                skipped_specialists=skipped_specialists,
            ),
            encoding="utf-8",
        )
        self.store.save_run(
            run_id,
            iso(effective_cutoff),
            decision,
            report_dir,
        )
        return AnalysisRun(
            run_id=run_id,
            cutoff=iso(effective_cutoff),
            decision=decision,
            current_price=snapshot.mid if snapshot else None,
            report_dir=report_dir,
        )

    def save_reflection(
        self,
        run_id: str,
        symbol: str,
        entry: Decimal,
        closes: tuple[Decimal, ...],
        benchmark_closes: tuple[Decimal, ...],
        decision_action: str | None = None,
        decision_cutoff: str | None = None,
        benchmark_symbol: str | None = None,
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
        payload["horizon_days"] = REFLECTION_HORIZON_DAYS
        if decision_action is not None:
            payload["decision_action"] = decision_action
        if decision_cutoff is not None:
            payload["decision_cutoff"] = decision_cutoff
        if benchmark_symbol is not None:
            payload["benchmark_symbol"] = benchmark_symbol
        if not self.store.save_reflection(
            run_id,
            symbol,
            to_jsonable(payload),
        ):
            raise ValueError(f"Reflection already exists for {run_id}")
        return payload

    def refresh_reflections(self, cutoff: datetime) -> list[str]:
        builder = self._require_builder()
        completed_before = iso(_aware(cutoff) - timedelta(days=REFLECTION_HORIZON_DAYS))
        saved: list[str] = []
        for run in self.store.unreflected_runs(completed_before, tuple(self.settings.vn_symbols)):
            if not is_decided(run["decision"]):
                continue
            if not run["decision"].get("evidence_ids"):
                continue
            try:
                evidence = json.loads(
                    (Path(run["report_dir"]) / "evidence.json").read_text(encoding="utf-8")
                )
                entry_val = (
                    evidence.get("closeRaw")
                    or evidence.get("close_raw")
                    or evidence.get("mid")
                    or evidence.get("binance_mid")
                )
                if entry_val is None:
                    continue
                entry = Decimal(str(entry_val))
                run_cutoff = datetime.fromisoformat(run["cutoff"]).astimezone(UTC)
                start = (run_cutoff + timedelta(days=1)).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                closes = builder.reflection_closes(
                    run["symbol"], start, periods=REFLECTION_HORIZON_DAYS
                )
                benchmark = (
                    ()
                    if run["symbol"] == VN_BENCHMARK_SYMBOL
                    else builder.reflection_closes(
                        VN_BENCHMARK_SYMBOL, start, periods=REFLECTION_HORIZON_DAYS
                    )
                )
                self.save_reflection(
                    run_id=run["id"],
                    symbol=run["symbol"],
                    entry=entry,
                    closes=closes,
                    benchmark_closes=benchmark,
                    decision_action=str(run["decision"]["action"]),
                    decision_cutoff=str(run["cutoff"]),
                    benchmark_symbol=VN_BENCHMARK_SYMBOL,
                )
            except (EvidenceError, OSError, ValueError, KeyError, TypeError):
                continue
            saved.append(run["id"])
        return saved

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
                    "vn_daily",
                    candidate.isoformat(),
                ):
                    target_date = candidate
                    break
        bucket = target_date.isoformat()
        if self.store.scheduled_done("vn_daily", bucket):
            return {"status": "ALREADY_DONE", "bucket": bucket}

        cutoff = datetime.combine(target_date, time(0, 15), tzinfo=UTC)
        reflection_run_ids = self.refresh_reflections(cutoff)
        runs: list[str] = []
        for symbol in self.settings.vn_symbols:
            runs.append(self.analyze(symbol, cutoff).run_id)
        self.store.mark_scheduled("vn_daily", bucket)
        return {
            "status": "COMPLETED",
            "bucket": bucket,
            "run_ids": runs,
            "reflection_run_ids": reflection_run_ids,
        }

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

    @staticmethod
    def _no_trade(
        symbol: str,
        reason: str,
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
            decided=False,
        )

    def _markdown_report(
        self,
        *,
        decision: ResearchDecision,
        cutoff: datetime,
        reports: dict[str, Any],
        snapshot: VNEvidenceSnapshot | None,
        skipped_specialists: tuple[str, ...] = (),
    ) -> str:
        lines: list[str] = []
        if skipped_specialists:
            lines.append(
                f"> Phân tích này KHÔNG đọc được báo cáo tài chính (thiếu: {', '.join(skipped_specialists)})."
            )
        lines.append(
            "*Lưu ý kiểm chéo: Hai nguồn dữ liệu đều từ SSI (thống kê và bảng giá), "
            "yếu hơn kiểm chéo đa nhà cung cấp.*"
        )
        lines.append("")
        lines.append(f"# Báo cáo phân tích hội đồng: {decision.symbol}")
        lines.append("")
        lines.append(f"- Thời điểm: `{iso(cutoff)}`")
        lines.append(f"- Quyết định: **{decision.action}** (Độ tin cậy: `{decision.conviction}/10`)")
        if decision.entry is not None:
            lines.append(
                f"- Mức giá tham khảo: Vào `{decision.entry:,}` | Dừng lỗ `{decision.stop:,}` | Chốt lời `{decision.target:,}`"
            )
        lines.append(f"- Lý do: {decision.reason}")
        lines.append("")
        lines.append("## Luận điểm tăng (Bull Case)")
        lines.append(decision.bull_case)
        lines.append("")
        lines.append("## Luận điểm giảm (Bear Case)")
        lines.append(decision.bear_case)
        lines.append("")
        if decision.catalysts:
            lines.append("## Chất xúc tác (Catalysts)")
            for cat in decision.catalysts:
                lines.append(f"- {cat}")
            lines.append("")
        if decision.invalidation:
            lines.append("## Điều kiện vô hiệu (Invalidation)")
            lines.append(decision.invalidation)
            lines.append("")
        lines.append("## Báo cáo chuyên gia")
        lines.append("")
        for name, report in reports.items():
            lines.append(f"### {name}")
            lines.append("")
            lines.append("```json")
            lines.append(
                json.dumps(
                    self._report_payload(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            lines.append("```")
            lines.append("")
        lines.append("## Evidence")
        lines.append("")
        lines.append(", ".join(decision.evidence_ids) or "Không có evidence hợp lệ.")
        lines.append("")
        if snapshot is not None:
            lines.append(f"Giá khớp SSI: {snapshot.mid}.")
            lines.append(f"Ngành: {snapshot.industry}.")
        if decision.action == "NO_TRADE":
            lines.append("")
            lines.append(f"> **NO_TRADE:** {decision.reason}")
        return "\n".join(lines) + "\n"
