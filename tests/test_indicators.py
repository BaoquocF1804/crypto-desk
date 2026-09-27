from decimal import Decimal

from crypto_desk.indicators import atr, ema, rsi


def test_ema_uses_sma_seed_and_all_closed_values():
    assert ema(tuple(map(Decimal, (1, 2))), 3) is None
    assert ema(tuple(map(Decimal, (1, 2, 3))), 3) == Decimal("2")
    assert ema(tuple(map(Decimal, (1, 2, 3, 4))), 3) == Decimal("3")


def test_wilder_rsi_handles_rising_falling_flat_and_smoothed_series():
    assert rsi(tuple(map(Decimal, (1, 2))), 2) is None
    assert rsi(tuple(map(Decimal, (1, 2, 3))), 2) == Decimal("100")
    assert rsi(tuple(map(Decimal, (3, 2, 1))), 2) == Decimal("0")
    assert rsi((Decimal("1"),) * 3, 2) == Decimal("50")
    assert rsi(tuple(map(Decimal, (10, 11, 10, 11, 10))), 2) == Decimal("37.5")


def test_wilder_atr_covers_gap_up_inside_bar_and_gap_down_ranges():
    highs = tuple(map(Decimal, ("2", "3", "4", "4", "3")))
    lows = tuple(map(Decimal, ("1", "2", "3", "3", "2.8")))
    closes = tuple(map(Decimal, ("1.5", "2.5", "3.5", "3.5", "2.9")))

    assert atr(highs[:2], lows[:2], closes[:2], 2) is None
    # True ranges: 1.5 (high - prior close), 1.5, 1 (high - low), 0.7 (prior close - low).
    assert atr(highs[:3], lows[:3], closes[:3], 2) == Decimal("1.5")
    assert atr(highs, lows, closes, 2) == Decimal("0.975")
