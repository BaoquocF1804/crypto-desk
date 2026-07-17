from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from binance_sdk_spot.rest_api.models.ticker_book_ticker_response import (
    TickerBookTickerResponse,
)

from crypto_desk.broker import BinanceSpotBroker
from crypto_desk.domain import TradeTicket


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeRest:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_account(self, **kwargs):
        self.calls.append(("get_account", kwargs))
        return fixture("binance_account.json")

    def get_open_orders(self, **kwargs):
        self.calls.append(("get_open_orders", kwargs))
        return []

    def ticker_book_ticker(self, **kwargs):
        self.calls.append(("ticker_book_ticker", kwargs))
        quotes = {
            "BTCUSDT": {"bidPrice": "99990", "askPrice": "100010"},
            "BNBUSDT": {"bidPrice": "499", "askPrice": "501"},
        }
        return quotes[kwargs["symbol"]]

    def exchange_info(self, **kwargs):
        self.calls.append(("exchange_info", kwargs))
        return fixture("binance_exchange_info.json")

    def order_list_otoco(self, **kwargs):
        self.calls.append(("order_list_otoco", kwargs))
        return fixture("binance_otoco_response.json")

    def delete_order_list(self, **kwargs):
        self.calls.append(("delete_order_list", kwargs))
        return {"listStatusType": "ALL_DONE", **kwargs}

    def new_order(self, **kwargs):
        self.calls.append(("new_order", kwargs))
        return {"orderId": 2001, "status": "FILLED", **kwargs}

    def order_list_oco(self, **kwargs):
        self.calls.append(("order_list_oco", kwargs))
        return {"orderListId": 2002, "listStatusType": "EXEC_STARTED", **kwargs}

    def get_order_list(self, **kwargs):
        self.calls.append(("get_order_list", kwargs))
        return fixture("binance_order_list_status.json")


class FakeSdk:
    def __init__(self):
        self.rest_api = FakeRest()


@pytest.fixture
def sdk() -> FakeSdk:
    return FakeSdk()


@pytest.fixture
def testnet_env(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "test-secret")


@pytest.fixture
def ticket() -> TradeTicket:
    return TradeTicket(
        id="c04716a4-9b5f-44e9-9281-97970dd00f86",
        environment="testnet",
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000.00"),
        stop_price=Decimal("95000.00"),
        target_price=Decimal("110000.00"),
        notional_usdt=Decimal("25.0000000"),
        risk_snapshot={"limiting_rule": "mainnet_cap"},
        created_at="2026-07-17T00:15:00+00:00",
        expires_at="2026-07-17T00:45:00+00:00",
    )


def test_testnet_and_mainnet_use_fixed_urls(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "test-secret")
    assert (
        BinanceSpotBroker("testnet", client=FakeSdk()).base_url == "https://testnet.binance.vision"
    )

    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "live-key")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET", "live-secret")
    assert BinanceSpotBroker("mainnet", client=FakeSdk()).base_url == "https://api.binance.com"


def test_missing_environment_specific_key_is_rejected(monkeypatch):
    monkeypatch.delenv("BINANCE_MAINNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_MAINNET_API_SECRET", raising=False)

    with pytest.raises(ValueError, match="MAINNET"):
        BinanceSpotBroker("mainnet", client=FakeSdk())


def test_credentials_are_not_retained_or_rendered(testnet_env, sdk):
    broker = BinanceSpotBroker("testnet", client=sdk)

    assert not hasattr(broker, "api_key")
    assert not hasattr(broker, "api_secret")
    assert "test-key" not in repr(broker)
    assert "test-secret" not in repr(broker)


def test_account_snapshot_values_free_and_locked_balances_in_usdt(
    testnet_env,
    sdk,
):
    snapshot = BinanceSpotBroker("testnet", client=sdk).account_snapshot()

    assert snapshot.environment == "testnet"
    assert snapshot.free_usdt == Decimal("100.00000000")
    assert snapshot.nav_usdt == Decimal("270.00000000")
    btc = next(position for position in snapshot.positions if position["asset"] == "BTC")
    assert btc["free"] == "0.00100000"
    assert btc["locked"] == "0.00020000"
    assert btc["value_usdt"] == "120.00000000"


def test_symbol_rules_and_latest_quote_are_decimal_safe(testnet_env, sdk):
    broker = BinanceSpotBroker("testnet", client=sdk)

    rules = broker.symbol_rules("BTCUSDT")
    quote = broker.latest_quote("BTCUSDT")

    assert rules.step_size == Decimal("0.00001")
    assert quote.mid == Decimal("100000")
    assert quote.bid == Decimal("99990")
    assert quote.ask == Decimal("100010")


def test_official_sdk_oneof_response_is_unwrapped(testnet_env, sdk):
    sdk.rest_api.ticker_book_ticker = lambda **kwargs: TickerBookTickerResponse.from_dict(
        {
            "symbol": kwargs["symbol"],
            "bidPrice": "99990",
            "bidQty": "1",
            "askPrice": "100010",
            "askQty": "1",
        }
    )

    quote = BinanceSpotBroker("testnet", client=sdk).latest_quote("BTCUSDT")

    assert quote.mid == Decimal("100000")


def test_entry_otoco_is_fok_and_sends_decimal_strings(
    testnet_env,
    sdk,
    ticket,
):
    broker = BinanceSpotBroker("testnet", client=sdk)

    result = broker.place_entry_otoco(ticket)
    _, params = sdk.rest_api.calls[-1]

    assert result["orderListId"] == 123
    assert params["workingSide"] == "BUY"
    assert params["workingType"] == "LIMIT"
    assert params["workingTimeInForce"] == "FOK"
    assert params["pendingSide"] == "SELL"
    assert params["pendingAboveType"] == "LIMIT_MAKER"
    assert params["pendingBelowType"] == "STOP_LOSS"
    for key in (
        "workingPrice",
        "workingQuantity",
        "pendingQuantity",
        "pendingAbovePrice",
        "pendingBelowStopPrice",
    ):
        assert isinstance(params[key], str)
    assert params["workingQuantity"] == "0.00025"
    ids = {
        params["listClientOrderId"],
        params["workingClientOrderId"],
        params["pendingAboveClientOrderId"],
        params["pendingBelowClientOrderId"],
    }
    assert len(ids) == 4
    assert all(len(value) <= 36 for value in ids)


def test_exit_protection_cancel_and_reconcile_use_narrow_spot_calls(
    testnet_env,
    sdk,
    ticket,
):
    sell_ticket = replace(ticket, intent="CLOSE", side="SELL")
    broker = BinanceSpotBroker("testnet", client=sdk)

    exit_order = broker.place_exit_fok(sell_ticket)
    protection = broker.place_protection_oco(ticket, Decimal("0.00010"))
    cancelled = broker.cancel_order_list("BTCUSDT", "cdt-existing")
    chain = broker.order_chain("cdt-existing")

    assert exit_order["status"] == "FILLED"
    assert protection["orderListId"] == 2002
    assert cancelled["listStatusType"] == "ALL_DONE"
    assert chain["orderListId"] == 123
    calls = {name: params for name, params in sdk.rest_api.calls}
    assert calls["new_order"]["timeInForce"] == "FOK"
    assert calls["new_order"]["quantity"] == "0.00025"
    assert calls["order_list_oco"]["quantity"] == "0.00010"
    assert calls["delete_order_list"]["listClientOrderId"] == "cdt-existing"
    assert calls["get_order_list"]["origClientOrderId"] == "cdt-existing"
