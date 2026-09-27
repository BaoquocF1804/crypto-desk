"""Quét toàn thị trường bằng model rẻ để tìm đồng đáng đưa vào watchlist nghiên cứu.

Code tính mọi con số; Flash chỉ xếp hạng cả bảng trong một lần gọi. Kết quả chỉ phục
vụ nghiên cứu: không đồng nào đi qua đây được tạo ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .committee import BASE_OUTPUT_CONTRACT, ProviderError, StructuredClient
from .config import REFLECTION_HORIZON_DAYS, WATCHLIST_MAX_ACTIVE
from .data import EvidenceError, _aware, _pct_change, _percentile_rank, _utc_from_ms, price_structure
from .domain import to_jsonable, utcnow
from .indicators import atr, ema

UNIVERSE_SIZE = 30
MIN_SCAN_QUOTE_VOLUME = Decimal("20000000")
# Stablecoins, fiat and gold tokens: no 20-day crypto thesis to find there.
NON_CRYPTO_BASES = frozenset(
    {
        "USDC",
        "FDUSD",
        "TUSD",
        "USDP",
        "DAI",
        "BUSD",
        "USDE",
        "USD1",
        "BFUSD",
        "XUSD",
        "RLUSD",
        "EUR",
        "EURI",
        "AEUR",
        "PAXG",
        "XAUT",
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    base_asset: str
    coingecko_id: str
    quote_volume: Decimal


def discover_universe(
    tickers: list[dict[str, Any]],
    spot_info: dict[str, Any],
    futures_info: dict[str, Any],
    markets: list[dict[str, Any]],
    *,
    exclude: frozenset[str],
) -> tuple[Candidate, ...]:
    """Cặp USDT đủ điều kiện để committee phân tích được, xếp theo volume 24h."""
    perpetuals = {
        item["symbol"]
        for item in futures_info.get("symbols", [])
        if item.get("contractType") == "PERPETUAL" and item.get("status") == "TRADING"
    }
    coingecko: dict[str, str] = {}
    for market in markets:
        # coins/markets is sorted by market cap, so the first coin per ticker is the largest.
        coingecko.setdefault(str(market.get("symbol", "")).upper(), str(market["id"]))
    spot = {item["symbol"]: item for item in spot_info.get("symbols", [])}
    candidates: list[Candidate] = []
    for ticker in tickers:
        symbol = str(ticker.get("symbol", ""))
        meta = spot.get(symbol)
        if meta is None or symbol in exclude:
            continue
        base = str(meta.get("baseAsset", ""))
        if (
            meta.get("quoteAsset") != "USDT"
            or meta.get("status") != "TRADING"
            or not meta.get("isSpotTradingAllowed")
            or not meta.get("ocoAllowed")
            or not meta.get("otoAllowed")
            or base in NON_CRYPTO_BASES
            or symbol not in perpetuals
            or base not in coingecko
        ):
            continue
        try:
            volume = Decimal(str(ticker["quoteVolume"]))
        except (KeyError, InvalidOperation):
            continue
        if volume >= MIN_SCAN_QUOTE_VOLUME:
            candidates.append(Candidate(symbol, base, coingecko[base], volume))
    candidates.sort(key=lambda candidate: (-candidate.quote_volume, candidate.symbol))
    return tuple(candidates[:UNIVERSE_SIZE])


CENT = Decimal("0.01")
# Price levels stay unrounded here; only the ratios the model reads are quantized.
LEVEL_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class ScanFeatures:
    symbol: str
    quote_volume_musd: Decimal
    change_20d_pct: Decimal | None
    ema20_gap_pct: Decimal | None
    ema20_above_ema50: bool | None
    atr1d_pct: Decimal | None
    range_position_20d: Decimal | None
    range_position_55d: Decimal | None
    support_distance_atr: Decimal | None
    resistance_distance_atr: Decimal | None
    long_short_pctile_20d: Decimal | None
    taker_buy_sell_24h: Decimal | None
    oi_change_24h_pct: Decimal | None
    funding_rate: Decimal | None


FEATURE_FIELDS = tuple(field.name for field in fields(ScanFeatures) if field.name != "symbol")


def _gap_pct(value: Decimal, base: Decimal | None) -> Decimal | None:
    return None if not base else ((value / base - 1) * 100).quantize(CENT)


def _range_position(
    close: Decimal, highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], window: int
) -> Decimal | None:
    if len(highs) < window:
        return None
    high, low = max(highs[-window:]), min(lows[-window:])
    if high == low:
        return Decimal("50")
    return ((close - low) / (high - low) * 100).quantize(Decimal("1"))


def compute_features(
    candidate: Candidate,
    daily_rows: list[list[Any]],
    oi_rows: list[dict[str, Any]],
    ls_rows: list[dict[str, Any]],
    taker_rows: list[dict[str, Any]],
    funding_rate: Decimal | None,
) -> ScanFeatures:
    highs = tuple(Decimal(str(row[2])) for row in daily_rows)
    lows = tuple(Decimal(str(row[3])) for row in daily_rows)
    closes = tuple(Decimal(str(row[4])) for row in daily_rows)
    if len(closes) < 21 or not all(value > 0 for value in (*highs, *lows, *closes)):
        raise EvidenceError(f"{candidate.symbol} lacks 21 valid closed daily candles")
    close = closes[-1]
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    atr14 = atr(highs, lows, closes, 14)
    dates = tuple(_utc_from_ms(row[0]).date().isoformat() for row in daily_rows)
    structure = price_structure(highs, lows, dates, close, LEVEL_QUANTUM)
    supports, resistances = structure["swing_supports"], structure["swing_resistances"]
    support = supports[0]["price"] if supports else structure["low_20d"]
    resistance = resistances[0]["price"] if resistances else None
    sold = sum((Decimal(str(row["sellVol"])) for row in taker_rows), Decimal(0))
    bought = sum((Decimal(str(row["buyVol"])) for row in taker_rows), Decimal(0))
    open_interest = [Decimal(str(row["sumOpenInterest"])) for row in oi_rows]
    oi_change = (
        _pct_change(open_interest[0], open_interest[-1]) if len(open_interest) >= 25 else None
    )
    return ScanFeatures(
        symbol=candidate.symbol,
        quote_volume_musd=(candidate.quote_volume / 1_000_000).quantize(CENT),
        change_20d_pct=_gap_pct(close, closes[-21]),
        ema20_gap_pct=_gap_pct(close, ema20),
        ema20_above_ema50=None if ema20 is None or ema50 is None else ema20 > ema50,
        atr1d_pct=None if not atr14 else (atr14 / close * 100).quantize(CENT),
        range_position_20d=_range_position(close, highs, lows, 20),
        range_position_55d=_range_position(close, highs, lows, 55),
        support_distance_atr=(
            None if not atr14 or support is None else ((close - support) / atr14).quantize(CENT)
        ),
        resistance_distance_atr=(
            None
            if not atr14 or resistance is None
            else ((resistance - close) / atr14).quantize(CENT)
        ),
        long_short_pctile_20d=(
            _percentile_rank([Decimal(str(row["longShortRatio"])) for row in ls_rows])
            if ls_rows
            else None
        ),
        taker_buy_sell_24h=(bought / sold).quantize(Decimal("0.0001")) if sold else None,
        oi_change_24h_pct=None if oi_change is None else oi_change.quantize(CENT),
        funding_rate=funding_rate,
    )


# A scan never picks more than the watchlist can show, so its picks always fit on screen.
MAX_PICKS = WATCHLIST_MAX_ACTIVE

FIELD_NOTES = {
    "quote_volume_musd": "24h Spot quote volume in million USDT",
    "change_20d_pct": "percent change over 20 closed daily candles",
    "ema20_gap_pct": "percent distance of the last close above (+) or below (-) daily EMA20",
    "ema20_above_ema50": "daily EMA20 above EMA50",
    "atr1d_pct": "daily ATR14 as percent of the last close",
    "range_position_20d": "last close within the 20-day high-low range, 0 = low, 100 = high",
    "range_position_55d": "last close within the 55-day high-low range, 0 = low, 100 = high",
    "support_distance_atr": "distance down to the nearest daily swing support (or 20-day low), "
    "in daily ATRs",
    "resistance_distance_atr": "distance up to the nearest daily swing resistance, in daily "
    "ATRs; null when no swing high sits above price",
    "long_short_pctile_20d": "rank of the account long/short ratio within ~20 days, 0-100",
    "taker_buy_sell_24h": "24h taker buy volume over sell volume",
    "oi_change_24h_pct": "24h open interest change in percent",
    "funding_rate": "latest perpetual funding rate",
}

SCAN_ROLE = (
    "You are the screening analyst of a long-only Binance Spot research desk. Rank the "
    f"candidates by the strength of evidence for a {REFLECTION_HORIZON_DAYS}-day Spot long "
    f"thesis and return at most {MAX_PICKS} picks, strongest first; return fewer, or none, "
    "when evidence is weak. Strong evidence: ema20_above_ema50 true with price above EMA20; "
    "resistance_distance_atr well above support_distance_atr, or no resistance at all, so "
    "a stop at least one daily ATR below entry still leaves room for gross R:R of 1.5; "
    "long_short_pctile_20d not high; taker_buy_sell_24h and oi_change_24h_pct supportive. "
    "Penalise extended moves (range_position_20d near 100 with a large ema20_gap_pct). "
    "Every figure is computed by code: name the fields you rely on in supporting_fields and "
    "never introduce numbers absent from the table. evidence_score runs from 0 to 10."
)
SCAN_PROMPT = f"{BASE_OUTPUT_CONTRACT}\n\n{SCAN_ROLE}"


class ScanPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    evidence_score: Annotated[Decimal, Field(ge=0, le=10)]
    thesis: str = Field(min_length=1, max_length=400)
    supporting_fields: list[str] = Field(min_length=1, max_length=6)


class ScanRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    picks: list[ScanPick] = Field(max_length=MAX_PICKS)
    summary: str = Field(min_length=1, max_length=600)


class ScanError(RuntimeError):
    pass


def _validate_ranking(ranking: ScanRanking, candidates: set[str]) -> None:
    seen: set[str] = set()
    for pick in ranking.picks:
        if pick.symbol not in candidates:
            raise ValueError(f"pick {pick.symbol} is not a scanned candidate")
        if pick.symbol in seen:
            raise ValueError(f"pick {pick.symbol} is duplicated")
        seen.add(pick.symbol)
        unknown = sorted(set(pick.supporting_fields) - set(FEATURE_FIELDS))
        if unknown:
            raise ValueError(f"unknown supporting fields {unknown}")


def rank_candidates(
    llm: StructuredClient,
    features: list[ScanFeatures],
    *,
    model: str,
    thinking: str,
) -> ScanRanking:
    payload = {
        "horizon_days": REFLECTION_HORIZON_DAYS,
        "fields": FIELD_NOTES,
        "candidates": [to_jsonable(row) for row in features],
    }
    last_error = ""
    for attempt in (1, 2):
        prompt = (
            SCAN_PROMPT
            if attempt == 1
            else f"{SCAN_PROMPT}\nThe previous ranking failed validation: {last_error}. "
            "Return a corrected full JSON object."
        )
        try:
            raw = llm.generate(
                stage="scan",
                model=model,
                thinking=thinking,
                response_model=ScanRanking,
                system_prompt=prompt,
                payload=payload,
            )
            ranking = raw if isinstance(raw, ScanRanking) else ScanRanking.model_validate(raw)
            _validate_ranking(ranking, {row.symbol for row in features})
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:300]
            continue
        picks = sorted(ranking.picks, key=lambda pick: pick.evidence_score, reverse=True)
        return ranking.model_copy(update={"picks": picks})
    raise ScanError(f"scan ranking rejected: {last_error}")


@dataclass(frozen=True, slots=True)
class ScanResult:
    universe: tuple[Candidate, ...]
    dropped: dict[str, str]
    features: tuple[ScanFeatures, ...]
    ranking: ScanRanking | None
    error: str | None


_FETCH_ERRORS = (
    EvidenceError,
    httpx.HTTPError,
    KeyError,
    IndexError,
    TypeError,
    ValueError,
    ArithmeticError,
)


class WatchlistScanner:
    def __init__(
        self,
        client: Any,
        llm: StructuredClient,
        *,
        model: str,
        thinking: str,
        now: Callable[[], datetime] = utcnow,
    ):
        self.client = client
        self.llm = llm
        self.model = model
        self.thinking = thinking
        self.now = now

    def run(self, exclude: frozenset[str]) -> ScanResult:
        try:
            universe = discover_universe(
                self.client.ticker_24h_all().payload,
                self.client.spot_exchange_info_all().payload,
                self.client.futures_exchange_info().payload,
                self.client.coingecko_markets().payload,
                exclude=exclude,
            )
            funding: dict[str, Decimal] = {}
            for row in self.client.premium_index_all().payload:
                rate = row.get("lastFundingRate")
                if rate not in (None, ""):
                    try:
                        funding[str(row["symbol"])] = Decimal(str(rate))
                    except (InvalidOperation, TypeError):
                        pass
        except _FETCH_ERRORS as exc:
            return ScanResult((), {}, (), None, f"universe:{type(exc).__name__}: {exc}"[:300])
        cutoff = _aware(self.now())
        features: list[ScanFeatures] = []
        dropped: dict[str, str] = {}
        for candidate in universe:
            try:
                features.append(self._features(candidate, funding, cutoff))
            except _FETCH_ERRORS as exc:
                dropped[candidate.symbol] = f"{type(exc).__name__}: {exc}"[:200]
        if not features:
            empty = ScanRanking(picks=[], summary="No candidate survived feature extraction.")
            return ScanResult(universe, dropped, (), empty, None)
        try:
            ranking = rank_candidates(self.llm, features, model=self.model, thinking=self.thinking)
        except ScanError as exc:
            return ScanResult(universe, dropped, tuple(features), None, str(exc))
        except ProviderError as exc:
            return ScanResult(universe, dropped, tuple(features), None, str(exc))
        return ScanResult(universe, dropped, tuple(features), ranking, None)

    def _features(
        self, candidate: Candidate, funding: dict[str, Decimal], cutoff: datetime
    ) -> ScanFeatures:
        daily = self.client.klines(candidate.symbol, "1d", 121).payload
        closed = [row for row in daily if _utc_from_ms(row[6]) <= cutoff]
        return compute_features(
            candidate,
            closed,
            self.client.open_interest_hist(candidate.symbol, period="1h", limit=25).payload,
            self.client.global_long_short_ratio(candidate.symbol, period="1h", limit=500).payload,
            self.client.taker_long_short_ratio(candidate.symbol, period="1h", limit=24).payload,
            funding.get(candidate.symbol),
        )
