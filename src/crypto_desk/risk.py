from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from .config import RiskSettings
from .domain import (
    Environment,
    ResearchDecision,
    SymbolRules,
    TradeTicket,
    to_jsonable,
)

INITIAL_MAINNET_CAP_USDT = Decimal("25")


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: Decimal
    notional_usdt: Decimal
    limiting_rule: str
    rooms: dict[str, Decimal]


def round_down(value: Decimal, increment: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("value must be non-negative")
    if increment <= 0:
        raise ValueError("increment must be positive")
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def validate_prices(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    rules: SymbolRules,
) -> None:
    if min(entry, stop, target) <= 0:
        raise ValueError("entry, stop and target must be positive")
    if not stop < entry < target:
        raise ValueError("prices must satisfy stop < entry < target")
    for label, value in (("entry", entry), ("stop", stop), ("target", target)):
        if round_down(value, rules.tick_size) != value:
            raise ValueError(f"{label} is not aligned to Binance tick size")


def size_buy(
    *,
    environment: Environment,
    nav_usdt: Decimal,
    free_usdt: Decimal,
    entry: Decimal,
    stop: Decimal,
    current_symbol_value: Decimal,
    current_gross_value: Decimal,
    mainnet_order_cap_usdt: Decimal,
    completed_mainnet_chains: int,
    risk: RiskSettings,
    rules: SymbolRules,
) -> SizingResult:
    if environment not in {"testnet", "mainnet"}:
        raise ValueError("environment must be testnet or mainnet")
    if nav_usdt <= 0:
        raise ValueError("NAV must be positive")
    if min(free_usdt, current_symbol_value, current_gross_value) < 0:
        raise ValueError("portfolio values must be non-negative")
    if entry <= 0 or stop <= 0 or stop >= entry:
        raise ValueError("stop must be positive and below entry")
    if round_down(entry, rules.tick_size) != entry:
        raise ValueError("entry is not aligned to Binance tick size")
    if round_down(stop, rules.tick_size) != stop:
        raise ValueError("stop is not aligned to Binance tick size")
    if completed_mainnet_chains < 0:
        raise ValueError("completed_mainnet_chains must be non-negative")

    risk_notional = risk.per_trade * nav_usdt * entry / (entry - stop)
    rooms = {
        "per_trade": risk_notional,
        "max_symbol": risk.max_symbol * nav_usdt - current_symbol_value,
        "max_gross": risk.max_gross * nav_usdt - current_gross_value,
        "usdt_reserve": free_usdt - risk.min_usdt_reserve * nav_usdt,
    }
    if environment == "mainnet" and completed_mainnet_chains < 20:
        rooms["mainnet_cap"] = min(
            mainnet_order_cap_usdt,
            INITIAL_MAINNET_CAP_USDT,
        )

    limiting_rule = min(rooms, key=rooms.__getitem__)
    available_notional = rooms[limiting_rule]
    if available_notional <= 0:
        raise ValueError(f"No remaining risk room for {limiting_rule}")
    if available_notional < rules.min_notional:
        raise ValueError("Order is below Binance minNotional")

    quantity = round_down(available_notional / entry, rules.step_size)
    notional = quantity * entry
    if quantity < rules.min_qty:
        raise ValueError("Order is below Binance minQty")
    if notional < rules.min_notional:
        raise ValueError("Order is below Binance minNotional after rounding")

    _validate_post_buy(
        nav_usdt=nav_usdt,
        free_usdt=free_usdt,
        current_symbol_value=current_symbol_value,
        current_gross_value=current_gross_value,
        notional=notional,
        risk=risk,
    )
    if (
        environment == "mainnet"
        and completed_mainnet_chains < 20
        and notional > min(mainnet_order_cap_usdt, INITIAL_MAINNET_CAP_USDT)
    ):
        raise ValueError("Order exceeds active Mainnet cap")

    return SizingResult(
        quantity=quantity,
        notional_usdt=notional,
        limiting_rule=limiting_rule,
        rooms=rooms,
    )


def _validate_post_buy(
    *,
    nav_usdt: Decimal,
    free_usdt: Decimal,
    current_symbol_value: Decimal,
    current_gross_value: Decimal,
    notional: Decimal,
    risk: RiskSettings,
) -> None:
    if current_symbol_value + notional > risk.max_symbol * nav_usdt:
        raise ValueError("Post-trade symbol exposure exceeds max_symbol")
    if current_gross_value + notional > risk.max_gross * nav_usdt:
        raise ValueError("Post-trade gross exposure exceeds max_gross")
    if free_usdt - notional < risk.min_usdt_reserve * nav_usdt:
        raise ValueError("Post-trade USDT reserve is below minimum")


def size_sell(
    *,
    action: Literal["REDUCE", "EXIT"] | str,
    free_base: Decimal,
    price: Decimal,
    rules: SymbolRules,
) -> SizingResult:
    if action not in {"REDUCE", "EXIT"}:
        raise ValueError("Sell action must be REDUCE or EXIT")
    if free_base <= 0:
        raise ValueError("Free base balance must be positive")
    if price <= 0:
        raise ValueError("Sell price must be positive")
    if round_down(price, rules.tick_size) != price:
        raise ValueError("Sell price is not aligned to Binance tick size")

    requested = free_base if action == "EXIT" else free_base / Decimal("2")
    quantity = round_down(requested, rules.step_size)
    notional = quantity * price
    if quantity < rules.min_qty:
        raise ValueError("Sell is below Binance minQty")
    if notional < rules.min_notional:
        raise ValueError("Sell is below Binance minNotional")
    if quantity > free_base:
        raise ValueError("Sell quantity exceeds free base balance")
    return SizingResult(
        quantity=quantity,
        notional_usdt=notional,
        limiting_rule="free_base" if action == "EXIT" else "reduce_50_percent",
        rooms={"free_base": free_base * price},
    )


def build_ticket(
    *,
    environment: Environment,
    decision: ResearchDecision,
    quantity: Decimal,
    current_position_quantity: Decimal,
    rules: SymbolRules,
    risk_snapshot: dict,
    ttl_minutes: int,
) -> TradeTicket:
    if decision.symbol != rules.symbol:
        raise ValueError("Decision symbol does not match Binance rules")
    if decision.action not in {"ACCUMULATE", "REDUCE", "EXIT"}:
        raise ValueError("Only actionable decisions can create tickets")
    if None in (decision.entry, decision.stop, decision.target):
        raise ValueError("Ticket requires entry, stop and target")
    assert decision.entry is not None
    assert decision.stop is not None
    assert decision.target is not None
    validate_prices(decision.entry, decision.stop, decision.target, rules)
    if quantity < rules.min_qty or round_down(quantity, rules.step_size) != quantity:
        raise ValueError("Ticket quantity violates Binance lot size")
    notional = quantity * decision.entry
    if notional < rules.min_notional:
        raise ValueError("Ticket is below Binance minNotional")

    if decision.action == "ACCUMULATE":
        side = "BUY"
        intent = "OPEN" if current_position_quantity <= 0 else "ADD"
    else:
        side = "SELL"
        intent = "REDUCE" if decision.action == "REDUCE" else "CLOSE"
        if current_position_quantity <= 0 or quantity > current_position_quantity:
            raise ValueError("Sell ticket exceeds current free position")

    return TradeTicket.create(
        ttl_minutes=ttl_minutes,
        environment=environment,
        symbol=decision.symbol,
        intent=intent,
        side=side,
        quantity=quantity,
        limit_price=decision.entry,
        stop_price=decision.stop,
        target_price=decision.target,
        notional_usdt=notional,
        risk_snapshot=to_jsonable(risk_snapshot),
    )
