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
    assert settings.models.provider == "gemini"
    assert settings.models.quick == "gemini-3.6-flash"
    assert settings.models.deep == "gemini-3.6-flash"


def test_config_accepts_explicit_openai_provider(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
models:
  provider: openai
  quick: gpt-5.4-mini
  deep: gpt-5.5
  quick_thinking: low
  deep_thinking: high
""",
    )

    settings = load_settings(config)

    assert settings.models.provider == "openai"
    assert settings.models.deep == "gpt-5.5"


def test_config_accepts_vertexai_provider_and_gemini_2_5_flash(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
models:
  provider: vertexai
  quick: gemini-2.5-flash
  deep: gemini-2.5-flash
  quick_thinking: low
  deep_thinking: high
""",
    )

    settings = load_settings(config)

    assert settings.models.provider == "vertexai"
    assert settings.models.quick == "gemini-2.5-flash"
    assert settings.models.deep == "gemini-2.5-flash"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("models:\n  provider: unknown\n", "models.provider"),
        (
            "models:\n  provider: gemini\n  quick: gemini-flash-latest\n",
            "allowlist",
        ),
        (
            "models:\n  provider: openai\n  quick: gemini-3.6-flash\n",
            "require models.provider=gemini",
        ),
        ("models:\n  quick_thinking: medium\n", "quick_thinking"),
    ],
)
def test_config_rejects_invalid_provider_models_and_thinking(
    tmp_path: Path,
    body: str,
    message: str,
):
    config = write_config(tmp_path / "config.yaml", body)

    with pytest.raises(ValueError, match=message):
        load_settings(config)


def test_config_accepts_gemini_3_flash_preview(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        "models:\n"
        "  provider: gemini\n"
        "  quick: gemini-3-flash-preview\n"
        "  deep: gemini-3-flash-preview\n",
    )

    settings = load_settings(config)

    assert settings.models.quick == "gemini-3-flash-preview"
    assert settings.models.deep == "gemini-3-flash-preview"


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


def test_config_rejects_ticket_ttl_above_30_minutes(tmp_path: Path):
    config = write_config(
        tmp_path / "config.yaml",
        """
symbols: [BTCUSDT]
risk:
  ticket_ttl_minutes: 45
""",
    )

    with pytest.raises(ValueError, match="ticket_ttl_minutes"):
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
