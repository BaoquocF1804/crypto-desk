from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from binance_common.configuration import ConfigurationRestAPI
from binance_sdk_spot import Spot
from pydantic import BaseModel

from .config import MAINNET_URL, TESTNET_URL, V1_SYMBOLS
from .domain import (
    Environment,
    PortfolioSnapshot,
    SymbolRules,
    TradeTicket,
    iso,
)


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpotQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    mid: Decimal


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


class _OfficialRestApi:
    def __init__(self, rest_api: Any):
        self._rest_api = rest_api

    def __getattr__(self, name: str):
        target = getattr(self._rest_api, name)

        def call(**wire_params: Any):
            python_params = {_camel_to_snake(key): value for key, value in wire_params.items()}
            return target(**python_params)

        return call


class _OfficialSpotClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str,
    ):
        configuration = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=base_url,
            retries=0,
        )
        self.rest_api = _OfficialRestApi(Spot(config_rest_api=configuration).rest_api)


class BinanceSpotBroker:
    def __init__(
        self,
        environment: Environment,
        *,
        client: Any | None = None,
    ):
        if environment not in {"testnet", "mainnet"}:
            raise ValueError("Binance environment must be testnet or mainnet")
        prefix = environment.upper()
        api_key = os.getenv(f"BINANCE_{prefix}_API_KEY")
        api_secret = os.getenv(f"BINANCE_{prefix}_API_SECRET")
        if not api_key or not api_secret:
            raise ValueError(f"Missing BINANCE_{prefix}_API_KEY or BINANCE_{prefix}_API_SECRET")
        self.environment = environment
        self.base_url = TESTNET_URL if environment == "testnet" else MAINNET_URL
        self._client = client or _OfficialSpotClient(
            api_key=api_key,
            api_secret=api_secret,
            base_url=self.base_url,
        )

    def __repr__(self) -> str:
        return f"BinanceSpotBroker(environment={self.environment!r})"

    def account_snapshot(self) -> PortfolioSnapshot:
        account = _unwrap(self._client.rest_api.get_account(omitZeroBalances=True))
        if not isinstance(account, dict):
            raise BrokerError("Binance account response is invalid")
        balances = account.get("balances", [])
        nav = Decimal("0")
        free_usdt = Decimal("0")
        positions: list[dict[str, str]] = []
        for balance in balances:
            asset = str(balance["asset"])
            free = Decimal(str(balance["free"]))
            locked = Decimal(str(balance["locked"]))
            total = free + locked
            if total == 0:
                continue
            if asset == "USDT":
                mid = Decimal("1")
                free_usdt = free
            else:
                quote = self.latest_quote(f"{asset}USDT")
                mid = quote.mid
            value = total * mid
            nav += value
            if asset != "USDT":
                positions.append(
                    {
                        "asset": asset,
                        "symbol": f"{asset}USDT",
                        "free": str(free),
                        "locked": str(locked),
                        "total": str(total),
                        "mid_usdt": str(mid),
                        "value_usdt": str(value),
                    }
                )

        open_orders = _unwrap(self._client.rest_api.get_open_orders())
        if not isinstance(open_orders, list):
            raise BrokerError("Binance open-orders response is invalid")
        update_time = account.get("updateTime")
        as_of = (
            iso(datetime.fromtimestamp(int(update_time) / 1000, tz=UTC))
            if update_time is not None
            else iso()
        )
        return PortfolioSnapshot(
            environment=self.environment,
            nav_usdt=nav,
            free_usdt=free_usdt,
            positions=tuple(positions),
            open_orders=tuple(
                item if isinstance(item, dict) else _unwrap(item) for item in open_orders
            ),
            as_of=as_of,
        )

    def symbol_rules(self, symbol: str) -> SymbolRules:
        self._validate_symbol(symbol)
        payload = _unwrap(self._client.rest_api.exchange_info(symbol=symbol))
        symbols = [item for item in payload.get("symbols", []) if item.get("symbol") == symbol]
        if len(symbols) != 1:
            raise BrokerError(f"Missing or ambiguous rules for {symbol}")
        item = symbols[0]
        if (
            item.get("status") != "TRADING"
            or item.get("quoteAsset") != "USDT"
            or not item.get("isSpotTradingAllowed")
            or not item.get("ocoAllowed")
            or not item.get("otoAllowed")
        ):
            raise BrokerError(f"{symbol} lacks required Spot/OTO/OCO support")
        filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
        try:
            notional = filters.get("NOTIONAL") or filters["MIN_NOTIONAL"]
            return SymbolRules(
                symbol=symbol,
                base_asset=str(item["baseAsset"]),
                quote_asset=str(item["quoteAsset"]),
                tick_size=Decimal(str(filters["PRICE_FILTER"]["tickSize"])),
                step_size=Decimal(str(filters["LOT_SIZE"]["stepSize"])),
                min_qty=Decimal(str(filters["LOT_SIZE"]["minQty"])),
                min_notional=Decimal(str(notional["minNotional"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerError(f"Missing Binance filters for {symbol}") from exc

    def latest_quote(self, symbol: str) -> SpotQuote:
        self._validate_symbol(symbol)
        payload = _unwrap(self._client.rest_api.ticker_book_ticker(symbol=symbol))
        bid = Decimal(str(payload["bidPrice"]))
        ask = Decimal(str(payload["askPrice"]))
        if bid <= 0 or ask <= bid:
            raise BrokerError(f"Invalid book ticker for {symbol}")
        return SpotQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / Decimal("2"),
        )

    def place_entry_otoco(self, ticket: TradeTicket) -> dict[str, Any]:
        self._validate_ticket(ticket, side="BUY")
        ids = self._client_ids(ticket.id)
        response = self._client.rest_api.order_list_otoco(
            symbol=ticket.symbol,
            workingType="LIMIT",
            workingSide="BUY",
            workingPrice=str(ticket.limit_price),
            workingQuantity=str(ticket.quantity),
            pendingSide="SELL",
            pendingQuantity=str(ticket.quantity),
            pendingAboveType="LIMIT_MAKER",
            pendingAbovePrice=str(ticket.target_price),
            pendingBelowType="STOP_LOSS",
            pendingBelowStopPrice=str(ticket.stop_price),
            workingTimeInForce="FOK",
            listClientOrderId=ids["list"],
            workingClientOrderId=ids["working"],
            pendingAboveClientOrderId=ids["target"],
            pendingBelowClientOrderId=ids["stop"],
        )
        return _expect_dict(response, "OTOCO")

    def cancel_order_list(
        self,
        symbol: str,
        list_client_order_id: str,
    ) -> dict[str, Any]:
        self._validate_symbol(symbol)
        return _expect_dict(
            self._client.rest_api.delete_order_list(
                symbol=symbol,
                listClientOrderId=list_client_order_id,
            ),
            "cancel order list",
        )

    def place_exit_fok(self, ticket: TradeTicket) -> dict[str, Any]:
        self._validate_ticket(ticket, side="SELL")
        ids = self._client_ids(ticket.id)
        return _expect_dict(
            self._client.rest_api.new_order(
                symbol=ticket.symbol,
                side="SELL",
                type="LIMIT",
                timeInForce="FOK",
                quantity=str(ticket.quantity),
                price=str(ticket.limit_price),
                newClientOrderId=ids["exit"],
            ),
            "exit order",
        )

    def place_protection_oco(
        self,
        ticket: TradeTicket,
        quantity: Decimal,
    ) -> dict[str, Any]:
        self._validate_environment(ticket)
        if quantity <= 0:
            raise ValueError("Protection quantity must be positive")
        ids = self._client_ids(ticket.id)
        return _expect_dict(
            self._client.rest_api.order_list_oco(
                symbol=ticket.symbol,
                side="SELL",
                quantity=str(quantity),
                aboveType="LIMIT_MAKER",
                abovePrice=str(ticket.target_price),
                belowType="STOP_LOSS",
                belowStopPrice=str(ticket.stop_price),
                listClientOrderId=ids["protection"],
                aboveClientOrderId=ids["protection_target"],
                belowClientOrderId=ids["protection_stop"],
            ),
            "protection OCO",
        )

    def order_chain(self, list_client_order_id: str) -> dict[str, Any]:
        return _expect_dict(
            self._client.rest_api.get_order_list(origClientOrderId=list_client_order_id),
            "order chain",
        )

    def client_order_id(self, ticket_id: str) -> str:
        return self._client_ids(ticket_id)["list"]

    def _client_ids(self, ticket_id: str) -> dict[str, str]:
        environment_code = "t" if self.environment == "testnet" else "m"
        digest = hashlib.sha256(ticket_id.encode()).hexdigest()[:20]
        base = f"cd{environment_code}-{digest}"
        return {
            "list": base,
            "working": f"{base}-w",
            "target": f"{base}-t",
            "stop": f"{base}-s",
            "exit": f"{base}-e",
            "protection": f"{base}-p",
            "protection_target": f"{base}-pt",
            "protection_stop": f"{base}-ps",
        }

    def _validate_ticket(
        self,
        ticket: TradeTicket,
        *,
        side: str,
    ) -> None:
        self._validate_environment(ticket)
        self._validate_symbol(ticket.symbol)
        if ticket.side != side:
            raise ValueError(f"Expected a {side} ticket")
        if (
            ticket.quantity <= 0
            or min(
                ticket.limit_price,
                ticket.stop_price,
                ticket.target_price,
            )
            <= 0
        ):
            raise ValueError("Ticket quantity and prices must be positive")

    def _validate_environment(self, ticket: TradeTicket) -> None:
        if ticket.environment != self.environment:
            raise ValueError("Ticket environment does not match broker")

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if symbol not in V1_SYMBOLS:
            raise ValueError("Symbol is outside the V1 Spot allowlist")


def _unwrap(response: Any) -> Any:
    if hasattr(response, "data") and callable(response.data):
        response = response.data()
    if isinstance(response, BaseModel):
        actual_instance = getattr(response, "actual_instance", None)
        if actual_instance is not None:
            return _unwrap(actual_instance)
        return {
            key: _unwrap(value)
            for key, value in response.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ).items()
        }
    if isinstance(response, dict):
        return {str(key): _unwrap(value) for key, value in response.items()}
    if isinstance(response, (list, tuple)):
        return [_unwrap(value) for value in response]
    return response


def _expect_dict(response: Any, label: str) -> dict[str, Any]:
    payload = _unwrap(response)
    if not isinstance(payload, dict):
        raise BrokerError(f"Invalid Binance {label} response")
    return payload
