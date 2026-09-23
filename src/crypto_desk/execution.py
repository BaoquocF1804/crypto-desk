from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from .broker import BinanceSpotBroker, BrokerError
from .config import (
    HARD_MAINNET_CAP_USDT,
    MAINNET_GRADUATION_CHAINS,
    MAX_TICKET_TTL_MINUTES,
    Settings,
)
from .domain import PortfolioSnapshot, SymbolRules, TradeTicket, utcnow
from .risk import round_down, size_buy, size_sell, validate_prices
from .store import Store


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ticket_id: str
    status: str
    payload: dict[str, Any]


CODE_BUCKET_SECONDS = 300


def _code_for_bucket(secret: str, ticket_id: str, bucket: int) -> str:
    digest = hmac.new(
        secret.encode(),
        f"{ticket_id}:{bucket}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def confirmation_code(
    secret: str,
    ticket_id: str,
    now: datetime,
) -> str:
    if not secret:
        raise ValueError("LIVE_CONFIRMATION_SECRET is required")
    bucket = int(now.astimezone(UTC).timestamp()) // CODE_BUCKET_SECONDS
    return _code_for_bucket(secret, ticket_id, bucket)


def verify_confirmation_code(
    secret: str,
    ticket_id: str,
    supplied: str,
    now: datetime,
) -> bool:
    bucket = int(now.astimezone(UTC).timestamp()) // CODE_BUCKET_SECONDS
    current = hmac.compare_digest(_code_for_bucket(secret, ticket_id, bucket), supplied)
    previous = hmac.compare_digest(_code_for_bucket(secret, ticket_id, bucket - 1), supplied)
    return current | previous


def telegram_approval_proof(
    secret: str,
    ticket_id: str,
    actor: str,
    code: str,
) -> str:
    if not secret:
        raise ValueError("Trusted Telegram ingress secret is required")
    return hmac.new(
        secret.encode(),
        f"{ticket_id}:{actor}:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()


_UNSET_GATE: Any = object()


class ExecutionService:
    def __init__(
        self,
        store: Store,
        broker: BinanceSpotBroker,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utcnow,
        live_enabled: bool | None = None,
        testnet_enabled: bool | None = None,
        confirmation_secret: str | None = None,
        environment_gate: Any = _UNSET_GATE,
        telegram_approval_secret: str | None = None,
    ):
        self.store = store
        self.broker = broker
        self.settings = settings
        self.now = now
        self.live_enabled = (
            os.getenv("LIVE_EXECUTION_ENABLED") == "1" if live_enabled is None else live_enabled
        )
        self.testnet_enabled = (
            os.getenv("TESTNET_EXECUTION_ENABLED") == "1"
            if testnet_enabled is None
            else testnet_enabled
        )
        self._confirmation_secret = (
            confirmation_secret
            if confirmation_secret is not None
            else os.getenv("LIVE_CONFIRMATION_SECRET")
        )
        self.environment_gate = (
            os.getenv("BINANCE_ENV") if environment_gate is _UNSET_GATE else environment_gate
        )
        self._telegram_approval_secret = (
            telegram_approval_secret
            if telegram_approval_secret is not None
            else os.getenv("HERMES_TELEGRAM_INGRESS_SECRET")
        )

    def approve(
        self,
        ticket_id: str,
        *,
        actor: str,
        channel: str,
        code: str | None = None,
        telegram_proof: str | None = None,
    ) -> ExecutionResult:
        ticket, resuming = self._approvable_ticket(ticket_id)
        self._validate_actor(actor, channel, ticket)
        selected_environment = self.settings.binance.environment
        if ticket.environment != selected_environment:
            raise ValueError("Ticket environment does not match BINANCE_ENV")
        if self.broker.environment != selected_environment:
            raise ValueError("Broker environment does not match BINANCE_ENV")

        if ticket.environment == "mainnet":
            if self.environment_gate != "mainnet":
                raise ValueError("BINANCE_ENV must independently select mainnet")
            if not self.live_enabled:
                raise ValueError("Mainnet execution is disabled")
            if channel != "telegram":
                raise ValueError("Mainnet approval requires Telegram")
            if not self._confirmation_secret:
                raise ValueError("Mainnet confirmation secret is missing")
            if code is None or not verify_confirmation_code(
                self._confirmation_secret,
                ticket.id,
                code,
                self._now(),
            ):
                raise ValueError("Mainnet confirmation code is invalid")
            if (
                not self._telegram_approval_secret
                or telegram_proof is None
                or not hmac.compare_digest(
                    telegram_approval_proof(
                        self._telegram_approval_secret,
                        ticket.id,
                        actor,
                        code,
                    ),
                    telegram_proof,
                )
            ):
                raise ValueError("Mainnet approval lacks trusted Telegram proof")
        elif not self.testnet_enabled:
            self.store.record_approval(
                ticket.id,
                actor=actor,
                channel=channel,
                status="APPROVED_DRY_RUN",
            )
            return ExecutionResult(
                ticket_id=ticket.id,
                status="APPROVED_DRY_RUN",
                payload={"status": "APPROVED_DRY_RUN"},
            )

        client_order_id = self.broker.client_order_id(ticket.id, ticket.side)
        if self.store.submission(ticket.id) is not None:
            raise ValueError(f"Ticket {ticket.id} is already submitted")
        if (
            self.store.submission_by_client_order_id(
                ticket.environment,
                client_order_id,
            )
            is not None
        ):
            raise ValueError(f"Client order {client_order_id} is already submitted")

        account, rules = self._pre_submit(ticket)
        if not resuming:
            self.store.record_approval(
                ticket.id,
                actor=actor,
                channel=channel,
                status="APPROVED",
            )
        self.store.save_submission(
            ticket.id,
            ticket.environment,
            client_order_id,
            {"status": "SUBMITTING", "_reconciled": False},
        )
        try:
            if ticket.side == "BUY":
                payload = self.broker.place_entry_otoco(ticket)
            else:
                payload = self._submit_sell(ticket, account, rules)
            if not isinstance(payload, dict) or not payload:
                raise BrokerError("Ambiguous empty Binance submission response")
        except (TimeoutError, ConnectionError, BrokerError):
            self.store.record_order_event(
                ticket.id,
                "SUBMISSION_UNKNOWN",
                {"status": "SUBMISSION_UNKNOWN"},
            )
            return self._reconcile_submission(ticket.id, client_order_id)

        status = _normalized_status(payload, default="SUBMITTED")
        if ticket.side == "SELL":
            status, payload = self._finalize_sell(ticket, rules, status, payload)
        recorded_payload = {**payload, "_reconciled": False}
        self.store.record_order_event(ticket.id, status, recorded_payload)
        return ExecutionResult(ticket.id, status, recorded_payload)

    def reject(
        self,
        ticket_id: str,
        *,
        actor: str,
        channel: str,
    ) -> ExecutionResult:
        ticket = self._pending_ticket(ticket_id)
        self._validate_actor(actor, channel, ticket)
        self.store.record_approval(
            ticket.id,
            actor=actor,
            channel=channel,
            status="REJECTED",
        )
        return ExecutionResult(
            ticket_id=ticket.id,
            status="REJECTED",
            payload={"status": "REJECTED"},
        )

    def reconcile(self, ticket_id: str) -> ExecutionResult:
        submission = self.store.submission(ticket_id)
        if submission is None:
            raise ValueError(f"Ticket {ticket_id} has no submission")
        return self._reconcile_submission(
            ticket_id,
            submission["client_order_id"],
        )

    def _approvable_ticket(self, ticket_id: str) -> tuple[TradeTicket, bool]:
        ticket = self.store.ticket(ticket_id)
        resuming = ticket.status == "APPROVED" and self.store.submission(ticket_id) is None
        if ticket.status != "PENDING" and not resuming:
            raise ValueError(f"Ticket {ticket_id} is not PENDING")
        if resuming and self.store.approval(ticket_id)["decision"] != "APPROVE":
            raise ValueError(f"Ticket {ticket_id} approval is not APPROVE")
        now = self._now()
        created = datetime.fromisoformat(ticket.created_at).astimezone(UTC)
        expires = datetime.fromisoformat(ticket.expires_at).astimezone(UTC)
        if created > now:
            raise ValueError("Ticket creation time is in the future")
        if now >= expires or now >= created + timedelta(minutes=MAX_TICKET_TTL_MINUTES):
            raise ValueError(f"Ticket {ticket_id} is expired")
        return ticket, resuming

    def _pending_ticket(self, ticket_id: str) -> TradeTicket:
        ticket, resuming = self._approvable_ticket(ticket_id)
        if resuming:
            raise ValueError(f"Ticket {ticket_id} is not PENDING")
        return ticket

    def _validate_actor(
        self,
        actor: str,
        channel: str,
        ticket: TradeTicket,
    ) -> None:
        if channel == "telegram" and actor not in self.settings.telegram_allowlist:
            raise ValueError("Telegram actor is outside the allowlist")
        if ticket.environment == "mainnet" and channel != "telegram":
            raise ValueError("Mainnet approval requires Telegram")
        if channel not in {"telegram", "local"}:
            raise ValueError("Approval channel must be telegram or local")

    def _pre_submit(
        self,
        ticket: TradeTicket,
    ) -> tuple[PortfolioSnapshot, SymbolRules]:
        if ticket.symbol not in self.settings.symbols:
            raise ValueError("Ticket symbol is outside the configured allowlist")
        rules = self.broker.symbol_rules(ticket.symbol)
        validate_prices(
            ticket.limit_price,
            ticket.stop_price,
            ticket.target_price,
            rules,
        )
        if round_down(ticket.quantity, rules.step_size) != ticket.quantity:
            raise ValueError("Ticket quantity no longer matches symbol filters")
        if ticket.quantity < rules.min_qty:
            raise ValueError("Ticket is below current Binance minQty")
        if ticket.quantity * ticket.limit_price < rules.min_notional:
            raise ValueError("Ticket is below current Binance minNotional")
        quote = self.broker.latest_quote(ticket.symbol)
        deviation = abs(quote.mid - ticket.limit_price) / ticket.limit_price
        if deviation > self.settings.risk.max_quote_deviation:
            raise ValueError(f"Current price deviation {deviation:.4%} exceeds 0.5%")

        account = self.broker.account_snapshot()
        if account.environment != ticket.environment:
            raise ValueError("Account snapshot environment mismatch")
        if ticket.side == "BUY" and any(position.get("unpriced") for position in account.positions):
            raise ValueError("Account contains unpriced Spot balances")
        current_gross = sum(
            (Decimal(position.get("value_usdt", "0")) for position in account.positions),
            Decimal("0"),
        )
        current_symbol = sum(
            (
                Decimal(position.get("value_usdt", "0"))
                for position in account.positions
                if position.get("symbol") == ticket.symbol
            ),
            Decimal("0"),
        )

        if ticket.side == "BUY":
            maximum = size_buy(
                environment=ticket.environment,
                nav_usdt=account.nav_usdt,
                free_usdt=account.free_usdt,
                entry=ticket.limit_price,
                stop=ticket.stop_price,
                current_symbol_value=current_symbol,
                current_gross_value=current_gross,
                mainnet_order_cap_usdt=(self.settings.risk.mainnet_initial_order_cap_usdt),
                completed_mainnet_chains=(self.store.completed_mainnet_chains()),
                risk=self.settings.risk,
                rules=rules,
            )
            if ticket.quantity > maximum.quantity:
                raise ValueError("Ticket quantity exceeds current risk room")
        else:
            total_base = self._total_base(account, rules.base_asset)
            action = "REDUCE" if ticket.intent == "REDUCE" else "EXIT"
            maximum = size_sell(
                action=action,
                free_base=total_base,
                price=ticket.limit_price,
                rules=rules,
            )
            if action == "EXIT" and ticket.quantity != maximum.quantity:
                raise ValueError("EXIT must use the entire free base balance")
            if action == "REDUCE" and ticket.quantity > maximum.quantity:
                raise ValueError("REDUCE exceeds 50% of free base balance")

        current_notional = ticket.quantity * quote.mid
        if (
            ticket.environment == "mainnet"
            and self.store.completed_mainnet_chains() < MAINNET_GRADUATION_CHAINS
            and current_notional > HARD_MAINNET_CAP_USDT
        ):
            raise ValueError(f"Mainnet ticket exceeds the active {HARD_MAINNET_CAP_USDT} USDT cap")
        return account, rules

    def _submit_sell(
        self,
        ticket: TradeTicket,
        account: PortfolioSnapshot,
        rules: SymbolRules,
    ) -> dict[str, Any]:
        protection_id = ticket.risk_snapshot.get("protection_list_client_order_id")
        if not protection_id:
            raise BrokerError("Sell ticket has no reconciled protection list")
        cancelled = self.broker.cancel_order_list(
            ticket.symbol,
            str(protection_id),
        )
        cancel_status = _normalized_status(cancelled, default="UNKNOWN")
        if cancel_status not in {"CANCELED", "ALL_DONE"}:
            raise BrokerError("Protective order cancellation is ambiguous")
        self.store.record_order_event(
            ticket.id,
            "PROTECTION_CANCELED",
            {"status": "PROTECTION_CANCELED"},
        )

        try:
            refreshed_before_exit = self.broker.account_snapshot()
        except Exception as exc:
            original_total = self._total_base(account, rules.base_asset)
            try:
                self.broker.place_protection_oco(ticket, original_total)
            except Exception:
                pass
            raise BrokerError("Post-cancel account refresh failed") from exc
        original_free = self._free_base(refreshed_before_exit, rules.base_asset)
        if original_free < ticket.quantity:
            try:
                self.broker.place_protection_oco(ticket, original_free)
            except Exception:
                pass
            raise BrokerError("Canceled protection did not release enough balance")
        self.store.record_order_event(
            ticket.id,
            "EXIT_SUBMITTING",
            {"status": "EXIT_SUBMITTING"},
        )
        return self.broker.place_exit_fok(ticket)

    def _reconcile_submission(
        self,
        ticket_id: str,
        client_order_id: str,
    ) -> ExecutionResult:
        try:
            ticket = self.store.ticket(ticket_id)
            payload = self.broker.submission_status(ticket, client_order_id)
            if not isinstance(payload, dict) or not payload:
                raise BrokerError("Empty order-chain response")
        except (TimeoutError, ConnectionError, BrokerError):
            submission = self.store.submission(ticket_id)
            ticket = self.store.ticket(ticket_id)
            if (
                ticket.side == "SELL"
                and submission is not None
                and submission["status"] == "PROTECTION_CANCELED"
                and self._restore_sell_protection(ticket)
            ):
                payload = {
                    "status": "FAILED_SAFE",
                    "client_order_id": client_order_id,
                    "reason": "crash_recovery_restored_protection",
                }
                self.store.record_order_event(ticket_id, "FAILED_SAFE", payload)
                return ExecutionResult(ticket_id, "FAILED_SAFE", payload)
            payload = {
                "status": "RECONCILE_REQUIRED",
                "client_order_id": client_order_id,
            }
            self.store.record_order_event(
                ticket_id,
                "RECONCILE_REQUIRED",
                payload,
            )
            return ExecutionResult(
                ticket_id,
                "RECONCILE_REQUIRED",
                payload,
            )

        status = _normalized_status(payload, default="SUBMITTED")
        ticket = self.store.ticket(ticket_id)
        if ticket.side == "SELL":
            try:
                rules = self.broker.symbol_rules(ticket.symbol)
                status, payload = self._finalize_sell(ticket, rules, status, payload)
            except Exception:
                status = "RECONCILE_REQUIRED"
                payload = {
                    **payload,
                    "status": status,
                    "reason": "sell_protection_recovery_failed",
                }
        recorded_payload = {**payload, "_reconciled": True}
        self.store.record_order_event(ticket_id, status, recorded_payload)
        return ExecutionResult(ticket_id, status, recorded_payload)

    def _restore_sell_protection(self, ticket: TradeTicket) -> bool:
        try:
            rules = self.broker.symbol_rules(ticket.symbol)
            account = self.broker.account_snapshot()
            remaining = round_down(
                self._free_base(account, rules.base_asset),
                rules.step_size,
            )
            if remaining < rules.min_qty or remaining * ticket.limit_price < rules.min_notional:
                return False
            protection = self.broker.place_protection_oco(ticket, remaining)
            status = str(
                protection.get("listStatusType") or protection.get("listOrderStatus") or ""
            ).upper()
            return status in {"EXEC_STARTED", "EXECUTING"}
        except Exception:
            return False

    def _finalize_sell(
        self,
        ticket: TradeTicket,
        rules: SymbolRules,
        status: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if status not in {"FILLED", "EXPIRED", "CANCELED", "REJECTED"}:
            return (
                "RECONCILE_REQUIRED",
                {**payload, "status": "RECONCILE_REQUIRED"},
            )
        account = self.broker.account_snapshot()
        remaining = round_down(
            self._free_base(account, rules.base_asset),
            rules.step_size,
        )
        requires_protection = status != "FILLED" or ticket.intent == "REDUCE"
        if ticket.intent == "CLOSE" and status == "FILLED" and remaining >= rules.min_qty:
            requires_protection = True
            status = "RECONCILE_REQUIRED"
        if requires_protection and remaining >= rules.min_qty:
            protection = self.broker.place_protection_oco(ticket, remaining)
            protection_status = str(
                protection.get("listStatusType") or protection.get("listOrderStatus") or ""
            ).upper()
            if protection_status not in {"EXEC_STARTED", "EXECUTING"}:
                return (
                    "RECONCILE_REQUIRED",
                    {
                        **payload,
                        "status": "RECONCILE_REQUIRED",
                        "reason": "protection_status_ambiguous",
                    },
                )
        return status, {**payload, "status": status}

    @staticmethod
    def _free_base(
        account: PortfolioSnapshot,
        base_asset: str,
    ) -> Decimal:
        return sum(
            (
                Decimal(position.get("free", "0"))
                for position in account.positions
                if position.get("asset") == base_asset
            ),
            Decimal("0"),
        )

    @staticmethod
    def _total_base(
        account: PortfolioSnapshot,
        base_asset: str,
    ) -> Decimal:
        return sum(
            (
                Decimal(position.get("total", "0"))
                for position in account.positions
                if position.get("asset") == base_asset
            ),
            Decimal("0"),
        )

    def _now(self) -> datetime:
        value = self.now()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalized_status(payload: dict[str, Any], *, default: str) -> str:
    raw = payload.get("status") or payload.get("listOrderStatus") or payload.get("listStatusType")
    if raw is None:
        return default
    status = str(raw).upper()
    if status in {
        "FILLED",
        "EXPIRED",
        "CANCELED",
        "CANCELLED",
        "REJECTED",
        "FAILED_SAFE",
        "RECONCILE_REQUIRED",
        "ALL_DONE",
    }:
        return "CANCELED" if status == "CANCELLED" else status
    return default
