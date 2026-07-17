from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .data import EvidenceSnapshot
from .domain import Action, ResearchDecision, to_jsonable


QUICK_MODEL = "gpt-5.4-mini"
DEEP_MODEL = "gpt-5.5"
SPECIALISTS = ("technical", "liquidity", "news", "derivatives")
NUMBER_PATTERN = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?(?![\w])")

ROLE_PROMPTS = {
    "technical": (
        "Bạn là technical analyst cho đầu tư crypto Spot trung hạn. Chỉ dùng daily/4h "
        "trend, volatility, momentum và mức invalidation trong evidence được cung cấp."
    ),
    "liquidity": (
        "Bạn là liquidity analyst cho Binance Spot. Chỉ đánh giá spread, depth, "
        "24h quote volume và quy mô có thể khớp từ evidence được cung cấp."
    ),
    "news": (
        "Bạn là news analyst. Chỉ dùng headline, URL, publication time và content "
        "hash đã cung cấp; không suy đoán nội dung bài báo."
    ),
    "derivatives": (
        "Bạn là derivatives-signal analyst. Funding và open interest chỉ là evidence "
        "định vị; tuyệt đối không đề xuất giao dịch Futures."
    ),
    "bull": (
        "Bạn là bull researcher. Thách thức bear case và chỉ lập luận bằng evidence "
        "ID hợp lệ trong snapshot."
    ),
    "bear": (
        "Bạn là bear researcher. Thách thức bull case và chỉ lập luận bằng evidence "
        "ID hợp lệ trong snapshot."
    ),
    "manager": (
        "Bạn là research manager của crypto Spot desk long-only. Chọn đúng một action "
        "HOLD, ACCUMULATE, REDUCE, EXIT hoặc NO_TRADE; không suy đoán dữ liệu."
    ),
}


class AnalystReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stance: Literal["bullish", "neutral", "bearish"]
    confidence: Annotated[Decimal, Field(ge=0, le=10)]
    observations: list[str] = Field(min_length=1, max_length=8)
    risks: list[str] = Field(max_length=8)
    evidence_ids: list[str] = Field(min_length=1)


class ManagerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    conviction: Annotated[Decimal, Field(ge=0, le=10)]
    bull_case: str
    bear_case: str
    catalysts: list[str]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    evidence_ids: list[str] = Field(min_length=1)


class StructuredClient(Protocol):
    def generate(
        self,
        *,
        stage: str,
        model: str,
        response_model: type[BaseModel],
        system_prompt: str,
        payload: dict[str, Any],
    ) -> BaseModel | dict[str, Any]: ...


class OpenAIStructuredClient:
    def __init__(self, client: Any):
        self.client = client

    def generate(
        self,
        *,
        stage: str,
        model: str,
        response_model: type[BaseModel],
        system_prompt: str,
        payload: dict[str, Any],
    ) -> BaseModel:
        del stage
        response = self.client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        to_jsonable(payload),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            text_format=response_model,
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI returned no parsed structured output")
        return response.output_parsed


@dataclass(frozen=True, slots=True)
class CommitteeResult:
    decision: ResearchDecision
    reports: dict[str, AnalystReport]


class CommitteeOutputError(ValueError):
    pass


class CryptoCommittee:
    def __init__(
        self,
        llm: StructuredClient,
        *,
        quick_model: str = QUICK_MODEL,
        deep_model: str = DEEP_MODEL,
        debate_rounds: int = 2,
    ):
        if debate_rounds != 2:
            raise ValueError("Crypto Desk V1 requires exactly two debate rounds")
        self.llm = llm
        self.quick_model = quick_model
        self.deep_model = deep_model
        self.debate_rounds = debate_rounds

    def run(
        self,
        snapshot: EvidenceSnapshot,
        reflections: tuple[str, ...] = (),
        *,
        position_quantity: Decimal = Decimal("0"),
    ) -> CommitteeResult:
        reports: dict[str, AnalystReport] = {}
        required_kinds = {"spot", "news", "derivatives", "reference"}
        if {item.kind for item in snapshot.items} != required_kinds or any(
            item.stale for item in snapshot.items
        ):
            return CommitteeResult(
                decision=self._no_trade(
                    snapshot,
                    "evidence snapshot is stale or incomplete",
                ),
                reports=reports,
            )
        payload = self._base_payload(snapshot, reflections, position_quantity)
        numeric_evidence = self._numeric_evidence(snapshot)
        try:
            for role in SPECIALISTS:
                reports[role] = self._call(
                    stage=role,
                    model=self.quick_model,
                    response_model=AnalystReport,
                    system_prompt=ROLE_PROMPTS[role],
                    payload={**payload, "reports": self._reports_payload(reports)},
                    valid_evidence_ids=snapshot.evidence_ids,
                    numeric_evidence=numeric_evidence,
                )

            for round_number in range(1, self.debate_rounds + 1):
                for side in ("bull", "bear"):
                    stage = f"{side}_round_{round_number}"
                    reports[stage] = self._call(
                        stage=stage,
                        model=self.deep_model,
                        response_model=AnalystReport,
                        system_prompt=ROLE_PROMPTS[side],
                        payload={**payload, "reports": self._reports_payload(reports)},
                        valid_evidence_ids=snapshot.evidence_ids,
                        numeric_evidence=numeric_evidence,
                    )

            manager = self._call(
                stage="manager",
                model=self.deep_model,
                response_model=ManagerDecision,
                system_prompt=ROLE_PROMPTS["manager"],
                payload={**payload, "reports": self._reports_payload(reports)},
                valid_evidence_ids=snapshot.evidence_ids,
                numeric_evidence=numeric_evidence,
                position_quantity=position_quantity,
            )
        except CommitteeOutputError as exc:
            return CommitteeResult(
                decision=self._no_trade(snapshot, str(exc)),
                reports=reports,
            )

        return CommitteeResult(
            decision=ResearchDecision(
                symbol=snapshot.symbol,
                action=manager.action,
                conviction=manager.conviction,
                bull_case=manager.bull_case,
                bear_case=manager.bear_case,
                catalysts=tuple(manager.catalysts),
                invalidation=manager.invalidation,
                entry=manager.entry,
                stop=manager.stop,
                target=manager.target,
                evidence_ids=tuple(manager.evidence_ids),
                reason="committee decision",
            ),
            reports=reports,
        )

    def _call(
        self,
        *,
        stage: str,
        model: str,
        response_model: type[AnalystReport] | type[ManagerDecision],
        system_prompt: str,
        payload: dict[str, Any],
        valid_evidence_ids: tuple[str, ...],
        numeric_evidence: frozenset[Decimal],
        position_quantity: Decimal = Decimal("0"),
    ) -> AnalystReport | ManagerDecision:
        last_error = "invalid structured output"
        for _attempt in range(2):
            try:
                raw = self.llm.generate(
                    stage=stage,
                    model=model,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    payload=payload,
                )
                parsed = (
                    raw if isinstance(raw, response_model) else response_model.model_validate(raw)
                )
                self._validate_evidence_ids(
                    parsed.evidence_ids,
                    valid_evidence_ids,
                )
                self._validate_numeric_claims(parsed, numeric_evidence)
                if isinstance(parsed, ManagerDecision):
                    self._validate_manager(parsed, position_quantity)
                return parsed
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = str(exc)
        raise CommitteeOutputError(f"{stage} structured output rejected: {last_error}")

    @staticmethod
    def _validate_evidence_ids(
        supplied: list[str],
        valid: tuple[str, ...],
    ) -> None:
        unknown = set(supplied) - set(valid)
        if unknown:
            raise ValueError("unknown evidence IDs")

    @staticmethod
    def _validate_manager(
        decision: ManagerDecision,
        position_quantity: Decimal,
    ) -> None:
        if decision.action == "ACCUMULATE":
            if None in (decision.entry, decision.stop, decision.target):
                raise ValueError("ACCUMULATE requires entry, stop and target")
            assert decision.entry is not None
            assert decision.stop is not None
            assert decision.target is not None
            if not decision.stop < decision.entry < decision.target:
                raise ValueError("ACCUMULATE requires stop < entry < target")
        if decision.action in {"REDUCE", "EXIT"} and position_quantity <= 0:
            raise ValueError(f"{decision.action} requires an existing position")

    @staticmethod
    def _validate_numeric_claims(
        parsed: AnalystReport | ManagerDecision,
        numeric_evidence: frozenset[Decimal],
    ) -> None:
        if isinstance(parsed, AnalystReport):
            texts = [*parsed.observations, *parsed.risks]
        else:
            texts = [
                parsed.bull_case,
                parsed.bear_case,
                *parsed.catalysts,
                parsed.invalidation,
            ]
        claims = {
            Decimal(match.replace(",", "."))
            for text in texts
            for match in NUMBER_PATTERN.findall(text)
        }
        unknown = claims - numeric_evidence
        if unknown:
            raise ValueError("numeric claim is absent from evidence")

    @staticmethod
    def _numeric_evidence(
        snapshot: EvidenceSnapshot,
    ) -> frozenset[Decimal]:
        encoded = json.dumps(to_jsonable(snapshot), ensure_ascii=False)
        values = {Decimal(match.replace(",", ".")) for match in NUMBER_PATTERN.findall(encoded)}
        values.update(Decimal(value) for value in ("1", "4", "20", "24", "60", "120", "180"))
        return frozenset(values)

    @staticmethod
    def _base_payload(
        snapshot: EvidenceSnapshot,
        reflections: tuple[str, ...],
        position_quantity: Decimal,
    ) -> dict[str, Any]:
        bounded_reflections = [str(value)[:2000] for value in reflections[-5:]]
        return {
            "snapshot": to_jsonable(snapshot),
            "evidence_ids": list(snapshot.evidence_ids),
            "reflections": bounded_reflections,
            "position_quantity": str(position_quantity),
        }

    @staticmethod
    def _reports_payload(
        reports: dict[str, AnalystReport],
    ) -> dict[str, Any]:
        return {name: report.model_dump(mode="json") for name, report in reports.items()}

    @staticmethod
    def _no_trade(
        snapshot: EvidenceSnapshot,
        reason: str,
    ) -> ResearchDecision:
        return ResearchDecision(
            symbol=snapshot.symbol,
            action="NO_TRADE",
            conviction=Decimal("0"),
            bull_case="Không đủ bằng chứng hợp lệ để giao dịch.",
            bear_case="Dữ liệu hoặc structured output không đạt contract.",
            catalysts=(),
            invalidation="Chạy lại sau khi evidence và schema hợp lệ.",
            entry=None,
            stop=None,
            target=None,
            evidence_ids=snapshot.evidence_ids,
            reason=reason,
        )
