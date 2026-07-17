from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .domain import Environment


TESTNET_URL = "https://testnet.binance.vision"
MAINNET_URL = "https://api.binance.com"
V1_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"})
MAINNET_GRADUATION_CHAINS = 20
HARD_MAINNET_CAP_USDT = Decimal("25")
MAX_TICKET_TTL_MINUTES = 30
DEFAULT_COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class RiskSettings:
    per_trade: Decimal = Decimal("0.005")
    max_symbol: Decimal = Decimal("0.20")
    max_gross: Decimal = Decimal("0.80")
    min_usdt_reserve: Decimal = Decimal("0.20")
    max_quote_deviation: Decimal = Decimal("0.005")
    mainnet_initial_order_cap_usdt: Decimal = Decimal("25")
    ticket_ttl_minutes: int = 30


@dataclass(frozen=True, slots=True)
class ModelSettings:
    quick: str = "gpt-5.4-mini"
    deep: str = "gpt-5.5"
    debate_rounds: int = 2


@dataclass(frozen=True, slots=True)
class BinanceSettings:
    environment: Environment = "testnet"


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    daily_utc: str = "00:15"
    health_minutes: int = 15


@dataclass(frozen=True, slots=True)
class Settings:
    base_currency: str = "USDT"
    database: Path = Path("data/crypto_desk.sqlite3")
    artifacts: Path = Path("artifacts/crypto")
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
    coingecko_ids: dict[str, str] = field(default_factory=lambda: DEFAULT_COINGECKO_IDS.copy())
    news_feeds: tuple[str, ...] = ()
    models: ModelSettings = field(default_factory=ModelSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    binance: BinanceSettings = field(default_factory=BinanceSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    telegram_allowlist: tuple[str, ...] = ()


def _local_path(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


def _load_risk(raw: dict[str, Any]) -> RiskSettings:
    defaults = RiskSettings()
    return RiskSettings(
        per_trade=_decimal(raw.get("per_trade", defaults.per_trade)),
        max_symbol=_decimal(raw.get("max_symbol", defaults.max_symbol)),
        max_gross=_decimal(raw.get("max_gross", defaults.max_gross)),
        min_usdt_reserve=_decimal(raw.get("min_usdt_reserve", defaults.min_usdt_reserve)),
        max_quote_deviation=_decimal(raw.get("max_quote_deviation", defaults.max_quote_deviation)),
        mainnet_initial_order_cap_usdt=_decimal(
            raw.get(
                "mainnet_initial_order_cap_usdt",
                defaults.mainnet_initial_order_cap_usdt,
            )
        ),
        ticket_ttl_minutes=int(raw.get("ticket_ttl_minutes", defaults.ticket_ttl_minutes)),
    )


def _validate(settings: Settings) -> None:
    if settings.base_currency != "USDT":
        raise ValueError("base_currency must be USDT")
    if not settings.symbols or len(settings.symbols) != len(set(settings.symbols)):
        raise ValueError("symbols must be a non-empty unique allowlist")
    if any(symbol not in V1_SYMBOLS for symbol in settings.symbols):
        raise ValueError("symbol is outside the V1 USDT allowlist")
    if any(not symbol.endswith("USDT") for symbol in settings.symbols):
        raise ValueError("V1 allowlist supports USDT pairs only")
    if any(symbol not in settings.coingecko_ids for symbol in settings.symbols):
        raise ValueError("every symbol requires a CoinGecko id")
    for name in (
        "per_trade",
        "max_symbol",
        "max_gross",
        "min_usdt_reserve",
        "max_quote_deviation",
    ):
        value = getattr(settings.risk, name)
        if not Decimal("0") < value <= Decimal("1"):
            raise ValueError(f"risk.{name} must be between 0 and 1")
    if settings.risk.mainnet_initial_order_cap_usdt < Decimal("5"):
        raise ValueError("Mainnet initial order cap must be at least 5 USDT")
    if not 0 < settings.risk.ticket_ttl_minutes <= MAX_TICKET_TTL_MINUTES:
        raise ValueError(f"ticket_ttl_minutes must be between 1 and {MAX_TICKET_TTL_MINUTES}")
    if settings.models.debate_rounds != 2:
        raise ValueError("V1 requires exactly two debate rounds")
    if settings.binance.environment not in {"testnet", "mainnet"}:
        raise ValueError("binance.environment must be testnet or mainnet")
    if settings.schedule.daily_utc != "00:15" or settings.schedule.health_minutes != 15:
        raise ValueError("V1 schedule is fixed at 00:15 UTC and every 15 minutes")


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    symbols = tuple(str(item).upper() for item in raw.get("symbols", Settings().symbols))
    models_raw = raw.get("models", {})
    binance_raw = raw.get("binance", {})
    schedule_raw = raw.get("schedule", {})
    settings = Settings(
        base_currency=str(raw.get("base_currency", "USDT")).upper(),
        database=_local_path(base, raw.get("database", "data/crypto_desk.sqlite3")),
        artifacts=_local_path(base, raw.get("artifacts", "artifacts/crypto")),
        symbols=symbols,
        coingecko_ids={
            str(key).upper(): str(value)
            for key, value in raw.get("coingecko_ids", DEFAULT_COINGECKO_IDS).items()
        },
        news_feeds=tuple(str(value) for value in raw.get("news_feeds", [])),
        models=ModelSettings(
            quick=str(models_raw.get("quick", "gpt-5.4-mini")),
            deep=str(models_raw.get("deep", "gpt-5.5")),
            debate_rounds=int(models_raw.get("debate_rounds", 2)),
        ),
        risk=_load_risk(raw.get("risk", {})),
        binance=BinanceSettings(environment=str(binance_raw.get("environment", "testnet")).lower()),
        schedule=ScheduleSettings(
            daily_utc=str(schedule_raw.get("daily_utc", "00:15")),
            health_minutes=int(schedule_raw.get("health_minutes", 15)),
        ),
        telegram_allowlist=tuple(str(value) for value in raw.get("telegram_allowlist", [])),
    )
    _validate(settings)
    return settings
