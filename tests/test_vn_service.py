from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from crypto_desk.committee import CommitteeResult
from crypto_desk.config import VN_BENCHMARK_SYMBOL, Settings
from crypto_desk.domain import EvidenceItem, ResearchDecision
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

    service = VNDeskService(
        settings, store, evidence_builder=_Builder(), committee=_Committee()
    )
    try:
        run = service.analyze("FPT")
        report = (run.report_dir / "report.md").read_text(encoding="utf-8")
    finally:
        store.close()

    assert run.decision.action == "HOLD"
    assert "KHÔNG đọc được báo cáo tài chính" in report
    assert report.splitlines()[0].startswith(">")


def test_committee_runs_with_missing_optional_fundamentals():
    """Bất biến 10: CryptoCommittee thật chạy bình thường khi thiếu fundamentals."""
    from crypto_desk.committee import AnalystReport, CryptoCommittee
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
                "bull_case": "ok",
                "bear_case": "ok",
                "catalysts": [],
                "invalidation": "none",
                "entry": None,
                "stop": None,
                "target": None,
                "evidence_ids": evidence_ids,
                "futures_bias": "NEUTRAL",
                "futures_setups": [],
            }

    committee = CryptoCommittee(
        _MockModel(),
        specialists=VN_SPECIALISTS,
        role_prompts=VN_ROLE_PROMPTS,
        specialist_evidence=VN_SPECIALIST_EVIDENCE,
        optional_kinds=VN_OPTIONAL_KINDS,
        mid_label="giá khớp SSI",
    )
    snapshot = make_vn_snapshot("FPT")  # 4 items bắt buộc, KHÔNG có fundamentals
    result = committee.run(snapshot)

    assert result.decision.action == "HOLD"
    assert "fundamentals" in result.skipped_specialists


def test_vn_reflections_are_benchmarked_against_vn30_not_btc(tmp_path):
    settings = Settings(database=tmp_path / "vn.sqlite3", artifacts=tmp_path / "a")
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

