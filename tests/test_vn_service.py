from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from crypto_desk.committee import CommitteeResult
from crypto_desk.config import VN_BENCHMARK_SYMBOL, Settings
from crypto_desk.domain import EvidenceItem, FuturesTradeSetup, ResearchDecision
from crypto_desk.store import Store
from crypto_desk.vn_data import VNEvidenceSnapshot
from crypto_desk.vn_prompts import (
    VN_OPTIONAL_KINDS,
    VN_ROLE_PROMPTS,
    VN_SPECIALIST_EVIDENCE,
    VN_SPECIALISTS,
)
from crypto_desk.vn_service import VNDeskService


def test_the_vn_manager_is_never_offered_reduce_or_exit():
    """Bất biến 8: không có nguồn vị thế cho cổ phiếu, nên REDUCE/EXIT luôn bị
    _validate_manager từ chối và biến một quyết định thật thành lỗi kỹ thuật."""
    manager = VN_ROLE_PROMPTS["manager"]

    assert "ACCUMULATE" in manager
    assert "HOLD" in manager
    assert "NO_TRADE" in manager
    assert "REDUCE" not in manager
    assert "EXIT" not in manager


def test_every_vn_specialist_has_a_prompt_and_an_evidence_mapping():
    assert len(VN_SPECIALISTS) == 5
    for role in VN_SPECIALISTS:
        assert role in VN_ROLE_PROMPTS
        assert role in VN_SPECIALIST_EVIDENCE
    for role in ("bull", "bear", "manager"):
        assert role in VN_ROLE_PROMPTS


def test_fundamentals_is_the_only_optional_kind():
    kinds = {kind for kind, _ in VN_SPECIALIST_EVIDENCE.values()}

    assert kinds == {"spot", "news", "flow", "fundamentals"}
    assert VN_OPTIONAL_KINDS == frozenset({"fundamentals"})


NOW = datetime(2026, 9, 20, 0, 15, tzinfo=UTC)


def make_vn_snapshot(symbol: str = "FPT") -> VNEvidenceSnapshot:
    items = tuple(
        EvidenceItem.create(
            kind=kind,
            provider="fixture",
            source=f"fixture://{kind}",
            fetched_at=NOW.isoformat(),
            as_of=NOW.isoformat(),
            delayed=False,
            stale=False,
            payload={"symbol": symbol, "value": value},
        )
        for kind, value in (
            ("spot", "100"),
            ("news", "current"),
            ("flow", "100"),
            ("reference", "100"),
        )
    )
    return VNEvidenceSnapshot(
        symbol=symbol,
        cutoff=NOW.isoformat(),
        items=items,
        mid=Decimal("100"),
        industry="Công nghệ Thông tin",
    )


def make_vn_decision(symbol: str = "FPT", action: str = "HOLD") -> ResearchDecision:
    return ResearchDecision(
        symbol=symbol,
        action=action,
        conviction=Decimal("5"),
        bull_case="Bull",
        bear_case="Bear",
        catalysts=(),
        invalidation="Invalidation",
        entry=None,
        stop=None,
        target=None,
        evidence_ids=("fixture-1",),
        reason="Quyết định của hội đồng.",
        decided=True,
    )


def test_a_missing_fundamentals_cache_still_produces_a_run_and_says_so(tmp_path):
    """Bất biến 10: vnstock vắng mặt không được làm chết run, nhưng phải nói ra."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "artifacts",
        vn_artifacts=tmp_path / "artifacts",
        fundamentals_dir=tmp_path / "no-such-dir",
    )
    store = Store(settings.database)

    class _Builder:
        def build(self, symbol, cutoff=None):
            return make_vn_snapshot(symbol)

    class _Committee:
        def run(self, snapshot, **kwargs):
            return CommitteeResult(
                decision=make_vn_decision(snapshot.symbol, "HOLD"),
                reports={},
                calls=(),
                skipped_specialists=("fundamentals",),
            )

    service = VNDeskService(settings, store, evidence_builder=_Builder(), committee=_Committee())
    try:
        run = service.analyze("FPT")
        report = (run.report_dir / "report.md").read_text(encoding="utf-8")
    finally:
        store.close()

    assert run.decision.action == "HOLD"
    assert "KHÔNG đọc được báo cáo tài chính" in report
    assert report.splitlines()[0].startswith(">")


def test_vn_decision_strips_futures_bias_and_setups_from_committee(tmp_path):
    """Kịch bản phái sinh không được rò vào quyết định cổ phiếu VN (HOSE)."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        vn_artifacts=tmp_path / "artifacts",
        fundamentals_dir=tmp_path / "no-such-dir",
    )
    store = Store(settings.database)

    leak_decision = make_vn_decision("FPT", "NO_TRADE")
    leak_decision = dataclasses.replace(
        leak_decision,
        futures_bias="BEARISH",
        futures_setups=(
            FuturesTradeSetup(
                direction="SHORT",
                entry=Decimal("71700"),
                stop=Decimal("74300"),
                target=Decimal("66250"),
                risk_reward_ratio=Decimal("2.1"),
                rationale="Phe bán áp đảo.",
            ),
        ),
    )

    class _Builder:
        def build(self, symbol, cutoff=None):
            return make_vn_snapshot(symbol)

    class _Committee:
        def run(self, snapshot, **kwargs):
            return CommitteeResult(
                decision=leak_decision,
                reports={},
                calls=(),
                skipped_specialists=(),
            )

    service = VNDeskService(settings, store, evidence_builder=_Builder(), committee=_Committee())
    try:
        run = service.analyze("FPT")
    finally:
        store.close()

    assert run.decision.futures_bias is None
    assert run.decision.futures_setups == ()
    saved_decision = json.loads((run.report_dir / "decision.json").read_text(encoding="utf-8"))
    assert saved_decision.get("futures_bias") is None
    assert saved_decision.get("futures_setups") == []


def test_committee_runs_with_missing_optional_fundamentals():
    """Bất biến 10: CryptoCommittee thật chạy bình thường khi thiếu fundamentals."""
    from crypto_desk.committee import AnalystReport, CryptoCommittee, VNManagerDecision
    from crypto_desk.vn_prompts import (
        VN_OPTIONAL_KINDS,
        VN_ROLE_PROMPTS,
        VN_SPECIALIST_EVIDENCE,
        VN_SPECIALISTS,
    )

    class _MockModel:
        def generate(self, **kwargs):
            evidence_ids = kwargs["payload"]["evidence_ids"]
            if kwargs.get("response_model") is AnalystReport:
                return {
                    "stance": "neutral",
                    "confidence": "5",
                    "observations": ["ok"],
                    "risks": ["ok"],
                    "evidence_ids": evidence_ids,
                }
            return {
                "action": "HOLD",
                "conviction": "5",
                "decision_reason": "Only 4/5 specialist reports are available; no defensible R:R.",
                "bull_case": "ok",
                "bear_case": "ok",
                "catalysts": [],
                "invalidation": "none",
                "entry": None,
                "stop": None,
                "target": None,
                "evidence_ids": evidence_ids,
            }

    committee = CryptoCommittee(
        _MockModel(),
        specialists=VN_SPECIALISTS,
        role_prompts=VN_ROLE_PROMPTS,
        specialist_evidence=VN_SPECIALIST_EVIDENCE,
        optional_kinds=VN_OPTIONAL_KINDS,
        mid_label="giá khớp SSI",
        manager_model=VNManagerDecision,
    )
    snapshot = make_vn_snapshot("FPT")  # 4 items bắt buộc, KHÔNG có fundamentals
    result = committee.run(snapshot)

    assert result.decision.action == "HOLD"
    assert result.decision.reason == "Only 4/5 specialist reports are available; no defensible R:R."
    assert "fundamentals" in result.skipped_specialists


def test_vn_reflections_are_benchmarked_against_vn30_not_btc(tmp_path):
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
    )
    store = Store(settings.database)
    seen: dict[str, str] = {}

    class _Builder:
        def reflection_closes(self, symbol, start, periods=20):
            seen.setdefault("benchmark_asked", symbol)
            return tuple(Decimal("100") for _ in range(20))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    payload = service.save_reflection(
        run_id="r1",
        symbol="FPT",
        entry=Decimal("100"),
        closes=tuple(Decimal("110") for _ in range(20)),
        benchmark_closes=tuple(Decimal("100") for _ in range(20)),
        benchmark_symbol=VN_BENCHMARK_SYMBOL,
    )
    store.close()

    assert VN_BENCHMARK_SYMBOL == "VN30"
    assert payload["benchmark_symbol"] == "VN30"


def test_vn_reflection_sweep_never_picks_up_crypto_runs(tmp_path):
    """Bảng research_runs dùng chung; quét không lọc sẽ vớ phải run crypto."""
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
        symbols=("BTCUSDT",),
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)
    swept: list[str] = []

    for run_id, symbol in (("r-btc", "BTCUSDT"), ("r-fpt", "FPT")):
        report_dir = tmp_path / run_id
        report_dir.mkdir()
        (report_dir / "evidence.json").write_text(
            json.dumps({"closeRaw": "100", "binance_mid": "100"}), encoding="utf-8"
        )
        store.save_run(
            run_id,
            (NOW - timedelta(days=40)).isoformat(),
            make_vn_decision(symbol, "HOLD"),
            report_dir,
        )

    class _Builder:
        def reflection_closes(self, symbol, start, periods=20):
            swept.append(symbol)
            return tuple(Decimal("100") for _ in range(20))

    service = VNDeskService(settings, store, evidence_builder=_Builder())
    try:
        saved = service.refresh_reflections(NOW)
    finally:
        store.close()

    assert saved == ["r-fpt"]
    assert "BTCUSDT" not in swept


def test_vn_technical_evidence_includes_close_raw():
    kind, fields = VN_SPECIALIST_EVIDENCE["technical"]
    assert kind == "spot"
    assert "close_raw" in fields
    assert "mid" in fields
    assert "daily_closes" in fields
    assert {"ref_price", "avg_price", "floor_price", "ceiling_price"} <= set(fields)


def test_vn_service_builds_and_passes_prior_thesis_to_committee(tmp_path):
    settings = Settings(
        database=tmp_path / "vn.sqlite3",
        artifacts=tmp_path / "a",
        vn_artifacts=tmp_path / "a",
        vn_symbols=("FPT",),
    )
    store = Store(settings.database)

    prior_cutoff = NOW - timedelta(days=5)
    prior_dir = tmp_path / "r-prior"
    prior_dir.mkdir()
    (prior_dir / "evidence.json").write_text(
        json.dumps({"mid": "95", "closeRaw": "95"}), encoding="utf-8"
    )
    prior_dec = make_vn_decision("FPT", "ACCUMULATE")
    prior_dec = dataclasses.replace(
        prior_dec,
        entry=Decimal("95"),
        stop=Decimal("90"),
        target=Decimal("110"),
    )
    store.save_run("r-prior", prior_cutoff.isoformat(), prior_dec, prior_dir)

    passed_prior_thesis: dict | None = None

    class _Builder:
        def build(self, symbol, cutoff=None):
            return make_vn_snapshot(symbol)

    class _Committee:
        def run(self, snapshot, **kwargs):
            nonlocal passed_prior_thesis
            passed_prior_thesis = kwargs.get("prior_thesis")
            return CommitteeResult(
                decision=make_vn_decision(snapshot.symbol, "HOLD"),
                reports={},
                calls=(),
                skipped_specialists=(),
            )

    service = VNDeskService(settings, store, evidence_builder=_Builder(), committee=_Committee())
    try:
        run = service.analyze("FPT", NOW)
    finally:
        store.close()

    assert passed_prior_thesis is not None
    assert passed_prior_thesis["run_id"] == "r-prior"
    assert passed_prior_thesis["action"] == "ACCUMULATE"
    assert passed_prior_thesis["prior_price"] == "95"
    assert passed_prior_thesis["current_price"] == "100"
    assert run.decision.prior_run_id == "r-prior"


def test_vn_committee_with_vn_manager_decision_model():
    from crypto_desk.committee import AnalystReport, CryptoCommittee, VNManagerDecision

    class _MockModel:
        def generate(self, **kwargs):
            evidence_ids = kwargs["payload"]["evidence_ids"]
            if kwargs.get("response_model") is AnalystReport:
                return {
                    "stance": "bullish",
                    "confidence": "7",
                    "observations": ["Strong growth"],
                    "risks": ["Competition"],
                    "evidence_ids": evidence_ids,
                }
            assert kwargs.get("response_model") is VNManagerDecision
            return {
                "action": "ACCUMULATE",
                "conviction": "8",
                "decision_reason": "Raw VND setup has gross R:R (115-100)/(100-95) = 3.0.",
                "bull_case": "Solid fundamentals and technical setup",
                "bear_case": "Market volatility",
                "catalysts": ["Earnings release"],
                "invalidation": "Breaks below support",
                "entry": "100",
                "stop": "95",
                "target": "115",
                "evidence_ids": evidence_ids,
                "thesis_continuity": "NEW",
            }

    committee = CryptoCommittee(
        _MockModel(),
        specialists=VN_SPECIALISTS,
        role_prompts=VN_ROLE_PROMPTS,
        specialist_evidence=VN_SPECIALIST_EVIDENCE,
        optional_kinds=VN_OPTIONAL_KINDS,
        mid_label="giá khớp SSI",
        manager_model=VNManagerDecision,
    )
    snapshot = make_vn_snapshot("FPT")
    result = committee.run(snapshot)

    assert result.decision.action == "ACCUMULATE"
    assert result.decision.conviction == Decimal("8")
    assert "R:R" in result.decision.reason
    assert result.decision.futures_bias is None
    assert result.decision.futures_setups == ()


def test_vn_prompts_bind_all_roles_to_snapshot_and_raw_price_scale():
    for role, prompt in VN_ROLE_PROMPTS.items():
        assert "only source of market facts" in prompt, role
        assert "daily_closes are split-adjusted" in prompt, role
        assert "raw matched price in VND" in prompt, role
        assert "If a field is missing" in prompt, role
    for role in ("bull", "bear"):
        assert "asymmetry" in VN_ROLE_PROMPTS[role]
        assert "invalidat" in VN_ROLE_PROMPTS[role]
    manager = VN_ROLE_PROMPTS["manager"]
    assert "(target - entry) / (entry - stop)" in manager
    assert "R:R >= 1.5" in manager
    assert "decision_reason" in manager


def test_vn_accumulate_requires_raw_entry_and_minimum_gross_reward_risk():
    from crypto_desk.committee import AnalystReport, CryptoCommittee, VNManagerDecision

    class _Model:
        def __init__(self, entry: str, stop: str, target: str):
            self.entry, self.stop, self.target = entry, stop, target
            self.manager_calls = 0

        def generate(self, **kwargs):
            ids = kwargs["payload"]["evidence_ids"]
            if kwargs["response_model"] is AnalystReport:
                return {
                    "stance": "neutral",
                    "confidence": "4",
                    "observations": ["Evidence is mixed"],
                    "risks": ["The raw target may be unsupported"],
                    "evidence_ids": ids,
                }
            self.manager_calls += 1
            return {
                "action": "ACCUMULATE",
                "conviction": "7",
                "decision_reason": "Proposed raw VND setup, subject to R:R gate.",
                "bull_case": "Potential upside",
                "bear_case": "Downside risk",
                "catalysts": [],
                "invalidation": "Raw stop breached",
                "entry": self.entry,
                "stop": self.stop,
                "target": self.target,
                "evidence_ids": ids,
            }

    def run(entry: str, stop: str, target: str):
        model = _Model(entry, stop, target)
        committee = CryptoCommittee(
            model,
            specialists=VN_SPECIALISTS,
            role_prompts=VN_ROLE_PROMPTS,
            specialist_evidence=VN_SPECIALIST_EVIDENCE,
            optional_kinds=VN_OPTIONAL_KINDS,
            manager_model=VNManagerDecision,
            mid_label="raw SSI mid",
        )
        return model, committee.run(make_vn_snapshot())

    model, result = run("100", "90", "115")  # gross R:R = 1.5, inclusive boundary
    assert result.decision.action == "ACCUMULATE"
    assert "Computed gross R:R: (115 - 100) / (100 - 90) = 1.50" in result.decision.reason
    assert model.manager_calls == 1

    model, result = run("100", "90", "114")  # gross R:R = 1.4
    assert result.decision.action == "NO_TRADE"
    assert "R:R" in result.decision.reason
    assert model.manager_calls == 2

    model, result = run("50", "45", "60")  # adjusted close used as entry vs raw mid 100
    assert result.decision.action == "NO_TRADE"
    assert "deviates" in result.decision.reason
    assert model.manager_calls == 2
