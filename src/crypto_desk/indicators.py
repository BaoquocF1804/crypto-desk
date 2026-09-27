"""Closed-candle indicators used as research evidence, not trading rules."""

from __future__ import annotations

from decimal import Decimal


def ema(closes: tuple[Decimal, ...], period: int) -> Decimal | None:
    """EMA seeded with the SMA of the first ``period`` closes."""
    if len(closes) < period:
        return None
    value = sum(closes[:period]) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for close in closes[period:]:
        value += alpha * (close - value)
    return value


def rsi(closes: tuple[Decimal, ...], period: int) -> Decimal | None:
    """Wilder RSI; a completely flat series is neutral (50)."""
    if len(closes) <= period:
        return None
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    average_gain = sum(max(change, Decimal(0)) for change in changes[:period]) / period
    average_loss = sum(max(-change, Decimal(0)) for change in changes[:period]) / period
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(change, Decimal(0))) / period
        average_loss = (average_loss * (period - 1) + max(-change, Decimal(0))) / period
    if average_gain == average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    return Decimal(100) - Decimal(100) / (Decimal(1) + average_gain / average_loss)


def atr(
    highs: tuple[Decimal, ...],
    lows: tuple[Decimal, ...],
    closes: tuple[Decimal, ...],
    period: int,
) -> Decimal | None:
    """Wilder ATR over true ranges that each have a prior close."""
    if len(closes) <= period:
        return None
    ranges = [
        max(high - low, abs(high - previous), abs(low - previous))
        for high, low, previous in zip(highs[1:], lows[1:], closes)
    ]
    value = sum(ranges[:period]) / Decimal(period)
    for true_range in ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def swing_points(
    highs: tuple[Decimal, ...],
    lows: tuple[Decimal, ...],
    width: int = 2,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Indices of confirmed swing highs and lows.

    A swing high beats the ``width`` bars before it strictly and is not exceeded by the
    ``width`` bars after it, so a flat top counts once and the last ``width`` bars are
    never swings: nothing has confirmed them yet.
    """
    last = len(highs) - width
    swing_highs = tuple(
        index
        for index in range(width, last)
        if highs[index] > max(highs[index - width : index])
        and highs[index] >= max(highs[index + 1 : index + width + 1])
    )
    swing_lows = tuple(
        index
        for index in range(width, last)
        if lows[index] < min(lows[index - width : index])
        and lows[index] <= min(lows[index + 1 : index + width + 1])
    )
    return swing_highs, swing_lows
