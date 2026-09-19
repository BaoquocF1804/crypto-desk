from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml

from .domain import Environment


TESTNET_URL = "https://testnet.binance.vision"
MAINNET_URL = "https://api.binance.com"
V1_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "SUIUSDT"})
MAINNET_GRADUATION_CHAINS = 20
HARD_MAINNET_CAP_USDT = Decimal("25")
MAX_TICKET_TTL_MINUTES = 30
DEFAULT_COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
    "SUIUSDT": "sui",
}
GEMINI_ALLOWED_MODELS = frozenset(
    {
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    }
)
ModelProvider = Literal["gemini", "openai", "vertexai"]
ThinkingLevel = Literal["low", "high"]


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
    provider: ModelProvider = "gemini"
    quick: str = "gemini-3.6-flash"
    deep: str = "gemini-3.6-flash"
    quick_thinking: ThinkingLevel = "low"
    deep_thinking: ThinkingLevel = "high"
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
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "SUIUSDT")
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
    if settings.models.provider not in {"gemini", "openai", "vertexai"}:
        raise ValueError("models.provider must be gemini, openai, or vertexai")
    if settings.models.quick_thinking not in {"low", "high"}:
        raise ValueError("models.quick_thinking must be low or high")
    if settings.models.deep_thinking not in {"low", "high"}:
        raise ValueError("models.deep_thinking must be low or high")
    selected_models = (settings.models.quick, settings.models.deep)
    if settings.models.provider in {"gemini", "vertexai"}:
        if any(model not in GEMINI_ALLOWED_MODELS for model in selected_models):
            raise ValueError("Gemini models must be in the allowlist")
    elif any(model.startswith("gemini-") for model in selected_models):
        raise ValueError("Gemini models require models.provider=gemini or vertexai")
    if settings.binance.environment not in {"testnet", "mainnet"}:
        raise ValueError("binance.environment must be testnet or mainnet")
    if settings.schedule.daily_utc != "00:15" or settings.schedule.health_minutes != 15:
        raise ValueError("V1 schedule is fixed at 00:15 UTC and every 15 minutes")


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    symbols = tuple(str(item).upper() for item in raw.get("symbols", Settings().symbols))
    models_raw = raw.get("models", {})
    model_defaults = ModelSettings()
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
            provider=str(models_raw.get("provider", model_defaults.provider)).lower(),
            quick=str(models_raw.get("quick", model_defaults.quick)),
            deep=str(models_raw.get("deep", model_defaults.deep)),
            quick_thinking=str(
                models_raw.get("quick_thinking", model_defaults.quick_thinking)
            ).lower(),
            deep_thinking=str(
                models_raw.get("deep_thinking", model_defaults.deep_thinking)
            ).lower(),
            debate_rounds=int(models_raw.get("debate_rounds", model_defaults.debate_rounds)),
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
