"""Map typed dashboard commands to services and curated safe result models."""

from __future__ import annotations

import importlib.metadata
import os
import platform
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .commands import (
    AnalyzeArgs,
    ExecuteArgs,
    PreviewArgs,
    ReflectionsArgs,
    SafeAnalyzeResult,
    SafeDailyResult,
    SafeDoctorResult,
    SafeExecuteResult,
    SafeHealthResult,
    SafeOrderRow,
    SafeOrdersResult,
    SafePreviewResult,
    SafeReflectionRow,
    SafeReflectionsResult,
    SafeScreenItem,
    SafeScreenResult,
    SafeSyncResult,
    SafeTicketRow,
    SafeTicketsResult,
    parse_args,
    ticket_fingerprint,
)
from .config import Settings
from .domain import utcnow
from .store import Store

_DOCTOR_PACKAGES = (
    "crypto-desk",
    "binance-sdk-spot",
    "google-genai",
    "openai",
    "httpx",
    "pydantic",
)


class DispatchError(RuntimeError):
    """Dispatch failure carrying a stable, safe error code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def execution_mode() -> Literal["TESTNET_ORDER", "DRY_RUN"]:
    return "TESTNET_ORDER" if os.getenv("TESTNET_EXECUTION_ENABLED") == "1" else "DRY_RUN"


class CommandDispatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        service_factory: Callable[..., Any] | None = None,
        execution_factory: Callable[[], Any] | None = None,
        store_factory: Callable[[], Store] | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self._service_factory = service_factory
        self._execution_factory = execution_factory
        self._store_factory = store_factory
        self.now = now

    def _service(self, **kwargs: Any) -> Any:
        if self._service_factory is not None:
            return self._service_factory(**kwargs)
        from .cli import _service

        return _service(self.settings, **kwargs)

    def _execution(self) -> Any:
        if self._execution_factory is not None:
            return self._execution_factory()
        from .cli import _execution_service

        return _execution_service(self.settings)

    def _store(self) -> Store:
        if self._store_factory is not None:
            return self._store_factory()
        return Store(self.settings.database)

    def dispatch(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        operator_email: str,
        environment: str = "testnet",
    ) -> BaseModel:
        if environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        try:
            parsed = parse_args(kind, args)
        except ValueError as exc:
            raise DispatchError("INVALID_COMMAND", str(exc)) from None
        handler = getattr(self, f"_dispatch_{kind}", None)
        if handler is None:
            raise DispatchError("INVALID_COMMAND")
        return handler(parsed, operator_email)

    def _dispatch_doctor(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeDoctorResult:
        del args, operator_email
        prefix = self.settings.binance.environment.upper()
        versions: dict[str, str | None] = {}
        for package in _DOCTOR_PACKAGES:
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        return SafeDoctorResult(
            python=platform.python_version(),
            package_versions=versions,
            provider=self.settings.models.provider,
            binance_environment=self.settings.binance.environment,
            binance_keys_present=bool(
                os.getenv(f"BINANCE_{prefix}_API_KEY") and os.getenv(f"BINANCE_{prefix}_API_SECRET")
            ),
            testnet_execution_enabled=(os.getenv("TESTNET_EXECUTION_ENABLED") == "1"),
            execution_mode=execution_mode(),
            database_schema_version=self._store().schema_version(),
            schedule={
                "daily_utc": self.settings.schedule.daily_utc,
                "health_minutes": str(self.settings.schedule.health_minutes),
            },
        )

    def _dispatch_sync(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeSyncResult:
        del args, operator_email
        snapshot = self._service(broker=True, committee=False).sync()
        return SafeSyncResult(
            environment=snapshot.environment,
            nav_usdt=str(snapshot.nav_usdt),
            free_usdt=str(snapshot.free_usdt),
            positions_count=len(snapshot.positions),
            open_orders_count=len(snapshot.open_orders),
            as_of=snapshot.as_of,
        )

    @staticmethod
    def _screen_item(item: Any) -> SafeScreenItem:
        if isinstance(item, dict):
            return SafeScreenItem(
                symbol=item["symbol"],
                passes=bool(item["passes"]),
                score=str(item["score"]),
                reasons=[str(reason) for reason in item.get("reasons", [])],
            )
        return SafeScreenItem(
            symbol=item.symbol,
            passes=item.passes,
            score=str(item.score),
            reasons=[str(reason) for reason in item.reasons],
        )

    def _dispatch_screen(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeScreenResult:
        del args, operator_email
        results = self._service(committee=False).screen()
        return SafeScreenResult(items=[self._screen_item(item) for item in results])

    def _dispatch_analyze(
        self,
        args: AnalyzeArgs,
        operator_email: str,
    ) -> SafeAnalyzeResult:
        del operator_email
        if args.symbol not in self.settings.symbols:
            raise DispatchError(
                "VALIDATION_FAILED",
                "Symbol is outside the configured allowlist",
            )
        run = self._service().analyze(args.symbol)
        decision = run.decision
        return SafeAnalyzeResult(
            run_id=run.run_id,
            symbol=decision.symbol,
            cutoff=run.cutoff,
            action=decision.action,
            conviction=str(decision.conviction),
            reason=decision.reason,
            entry=(None if decision.entry is None else str(decision.entry)),
            stop=(None if decision.stop is None else str(decision.stop)),
            target=(None if decision.target is None else str(decision.target)),
            current_price=(None if run.current_price is None else str(run.current_price)),
            ticket_id=run.ticket_id,
        )

    def _dispatch_daily(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeDailyResult:
        del args, operator_email
        result = self._service().daily(due=False, catch_up=False)
        return SafeDailyResult(
            status=result["status"],
            bucket=str(result["bucket"]),
            run_ids=[str(run_id) for run_id in result.get("run_ids", [])],
            screen=[self._screen_item(item) for item in result.get("screen", [])],
        )

    def _dispatch_health(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeHealthResult:
        del args, operator_email
        result = self._service(
            broker=True,
            execution=True,
        ).health(due=False)
        return SafeHealthResult(
            status=result["status"],
            bucket=str(result["bucket"]),
            alerts=[str(alert) for alert in result.get("alerts", [])],
            run_ids=[str(run_id) for run_id in result.get("run_ids", [])],
            reconciled_count=len(result.get("reconciled", [])),
        )

    def _dispatch_tickets(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeTicketsResult:
        del args, operator_email
        return SafeTicketsResult(
            tickets=[
                SafeTicketRow(
                    id=row["id"],
                    environment=row["environment"],
                    symbol=row["symbol"],
                    status=row["status"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                )
                for row in self._store().list_tickets()
            ]
        )

    def _dispatch_orders(
        self,
        args: Any,
        operator_email: str,
    ) -> SafeOrdersResult:
        del args, operator_email
        return SafeOrdersResult(
            orders=[
                SafeOrderRow(
                    ticket_id=row["ticket_id"],
                    environment=row["environment"],
                    status=row["status"],
                    updated_at=row["updated_at"],
                )
                for row in self._store().list_submissions()
            ]
        )

    def _dispatch_reflections(
        self,
        args: ReflectionsArgs,
        operator_email: str,
    ) -> SafeReflectionsResult:
        del operator_email
        reflections = []
        for row in self._store().list_reflections(args.symbol):
            payload = row.get("payload") or {}
            reflections.append(
                SafeReflectionRow(
                    run_id=row["run_id"],
                    symbol=row["symbol"],
                    created_at=row["created_at"],
                    realized_return=_optional_str(payload.get("realized_return")),
                    alpha=_optional_str(payload.get("alpha")),
                )
            )
        return SafeReflectionsResult(reflections=reflections)

    def _require_testnet(
        self,
        ticket_environment: str | None = None,
    ) -> None:
        if self.settings.binance.environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        if os.getenv("BINANCE_ENV") != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        if ticket_environment is not None and ticket_environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")

    @staticmethod
    def _load_ticket(store: Store, ticket_id: str) -> Any:
        try:
            return store.ticket(ticket_id)
        except ValueError:
            raise DispatchError("TICKET_NOT_FOUND") from None

    def _dispatch_preview(
        self,
        args: PreviewArgs,
        operator_email: str,
    ) -> SafePreviewResult:
        del operator_email
        self._require_testnet()
        store = self._store()
        ticket = self._load_ticket(store, args.ticket_id)
        self._require_testnet(ticket.environment)
        submission = store.submission(ticket.id)
        return SafePreviewResult(
            action=args.action,
            ticket_id=ticket.id,
            environment=ticket.environment,
            execution_mode=execution_mode(),
            status=ticket.status,
            symbol=ticket.symbol,
            side=ticket.side,
            intent=ticket.intent,
            quantity=str(ticket.quantity),
            notional_usdt=str(ticket.notional_usdt),
            entry=str(ticket.limit_price),
            stop=str(ticket.stop_price),
            target=str(ticket.target_price),
            created_at=ticket.created_at,
            expires_at=ticket.expires_at,
            submission_status=(None if submission is None else submission["status"]),
            fingerprint=ticket_fingerprint(ticket),
        )

    def _dispatch_execute(
        self,
        args: ExecuteArgs,
        operator_email: str,
    ) -> SafeExecuteResult:
        self._require_testnet()
        store = self._store()
        ticket = self._load_ticket(store, args.ticket_id)
        self._require_testnet(ticket.environment)
        if ticket_fingerprint(ticket) != args.fingerprint:
            raise DispatchError("TICKET_CHANGED")
        execution = self._execution()
        try:
            if args.action == "approve":
                result = execution.approve(
                    ticket.id,
                    actor=operator_email,
                    channel="local",
                )
            elif args.action == "reject":
                result = execution.reject(
                    ticket.id,
                    actor=operator_email,
                    channel="local",
                )
            else:
                result = execution.reconcile(ticket.id)
        except ValueError as exc:
            raise DispatchError(
                "VALIDATION_FAILED",
                str(exc),
            ) from None
        return SafeExecuteResult(
            action=args.action,
            ticket_id=ticket.id,
            status=result.status,
        )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
