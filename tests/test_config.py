from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.config import MAINNET_URL, TESTNET_URL, load_settings
from crypto_desk.domain import SymbolRules, TradeTicket, to_jsonable


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_execution_urls_are_owned_by_code():
    assert TESTNET_URL == "https://testnet.binance.vision"
    assert MAINNET_URL == "https://api.binance.com"


@pytest.mark.parametrize("symbol", ["ETHBTC", "DOGEUSDT"])
def test_config_rejects_symbol_outside_v1_allowlist(tmp_path: Path, symbol: str):
    config = write_config(tmp_path / "config.yaml", f"symbols: [BTCUSDT, {symbol}]\n")

    with pytest.raises(ValueError, match="allowlist"):
        load_settings(config)


def test_config_resolves_paths_and_decimal_risk_values(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
database: state/crypto.sqlite3
artifacts: output
symbols: [BTCUSDT, ETHUSDT]
risk:
  per_trade: 0.005
  max_symbol: 0.20
  max_gross: 0.80
  min_usdt_reserve: 0.20
  mainnet_initial_order_cap_usdt: "25"
""",
    )

    settings = load_settings(config)

    assert settings.database == tmp_path / "state/crypto.sqlite3"
    assert settings.artifacts == tmp_path / "output"
    assert settings.symbols == ("BTCUSDT", "ETHUSDT")
    assert settings.risk.per_trade == Decimal("0.005")
    assert settings.risk.mainnet_initial_order_cap_usdt == Decimal("25")


def test_config_rejects_mainnet_cap_below_five_usdt(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
symbols: [BTCUSDT]
risk:
  mainnet_initial_order_cap_usdt: "4.99"
""",
    )

    with pytest.raises(ValueError, match="5 USDT"):
        load_settings(config)


def test_decimal_serializes_as_string_without_losing_scale():
    assert to_jsonable({"qty": Decimal("0.00123000")}) == {"qty": "0.00123000"}


def test_trade_ticket_keeps_decimal_values():
    ticket = TradeTicket.create(
        environment="testnet",
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000"),
        stop_price=Decimal("90000"),
        target_price=Decimal("120000"),
        notional_usdt=Decimal("25"),
        risk_snapshot={},
        ttl_minutes=30,
    )

    assert ticket.quantity == Decimal("0.00025")
    assert ticket.status == "PENDING"
    assert ticket.id
    assert ticket.expires_at > ticket.created_at


def test_symbol_rules_require_usdt_quote():
    with pytest.raises(ValueError, match="USDT"):
        SymbolRules(
            symbol="ETHBTC",
            base_asset="ETH",
            quote_asset="BTC",
            tick_size=Decimal("0.000001"),
            step_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("0.0001"),
        )
