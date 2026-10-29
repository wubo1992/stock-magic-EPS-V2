"""Tests for detect_crossback() — EMA 10 crossback detection with volume confirmation."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    detect_crossback,
    _crossback_strength,
)


def _make_df(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Build a simple OHLCV DataFrame from close prices."""
    n = len(close_prices)
    if volumes is None:
        volumes = [1_000_000] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": close_prices,
            "high": [p * 1.01 for p in close_prices],
            "low": [p * 0.99 for p in close_prices],
            "close": close_prices,
            "volume": volumes,
        },
        index=dates,
    )


def test_detect_crossback_insufficient_data_returns_empty():
    """When df has fewer rows than ema_period + vol_lookback, return []."""
    short_close = [10.0] * 20  # only 20 rows, need 30
    df = _make_df(short_close)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_all_above_returns_empty():
    """When price stays above EMA10 the whole time, no signal."""
    # Monotonically rising prices from 10 to 20 over 60 days
    closes = list(np.linspace(10, 20, 60))
    df = _make_df(closes)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_no_recovery_returns_empty():
    """When price breaks EMA10 down and never recovers, no signal."""
    # First 40 days rising, then drops below and stays
    closes = list(np.linspace(10, 20, 40)) + [10] * 20  # last 20 days at 10
    df = _make_df(closes)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_then_recovery_no_volume_no_signal():
    """When price recovers but volume < 1.5x avg, no signal."""
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + list(np.linspace(10.5, 12, 15))
    # All volumes uniform → ratio = 1.0
    df = _make_df(closes, volumes=[1_000_000] * 60)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_recovery_with_volume_emits_signal():
    """Full case: breakdown + recovery + 2x volume → 1 signal."""
    # 40 days rising → 10 days at 10 (allow EMA to drop to ~11) → recovery at 13 (with 2x vol spike)
    # Then 19 days of stable recovery to keep EMA warm
    closes = list(np.linspace(10, 20, 40)) + [10] * 10 + [13.0] + [13.0] * 19  # 70 total
    vols = [1_000_000] * 50 + [2_000_000] * 20  # align with 70
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert len(signals) == 1
    assert signals[0]["close"] == 13.0
    assert signals[0]["volume_ratio"] >= 1.5


def test_detect_crossback_multiple_breakdowns_emit_multiple_signals():
    """Multiple breakdown-recovery cycles → multiple signals (per spec §2)."""
    closes = (
        list(np.linspace(10, 20, 20))    # 20 days above (EMA warms)
        + [10] * 10                       # 10 days below (EMA drops to ~10)
        + [12]                           # recovery #1 (volume spike)
        + list(np.linspace(12, 22, 20))  # 20 days above (EMA warms again)
        + [11] * 10                       # 10 days below (EMA drops to ~11)
        + [13]                           # recovery #2 (volume spike)
        + [13] * 10                       # 10 days above
    )
    n = len(closes)
    assert n >= 60, f"Need at least 60 days for EMA + volume lookback, got {n}"
    # 2x volume on recovery days (indexes 30 and 61), 1x elsewhere
    vols = [1_000_000] * n
    vols[30] = 2_000_000
    vols[61] = 2_000_000
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert len(signals) == 2


def test_detect_crossback_signal_date_close_below_ema10_still_works():
    """Issue 3 fix: signal_date close < EMA10 → state starts as 'below'."""
    closes = list(np.linspace(10, 20, 30)) + [12] * 6 + [25] + [25] * 5
    vols = [1_000_000] * 36 + [2_000_000] * 6
    df = _make_df(closes, volumes=vols)
    signal_date = df.index[30]  # close=12, below EMA10
    signals = detect_crossback(df, signal_date=signal_date)
    # Should emit signal on day 36 (close=25 crosses above EMA10)
    assert len(signals) >= 1
    assert signals[0]["date"] == df.index[36]


def test_detect_crossback_volume_zero_no_signal():
    """When volume=0 (halted), no signal even if price pattern is right."""
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + [12] * 15
    vols = [1_000_000] * 45 + [0] * 15  # halted
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_crossback_strength_thresholds():
    """_crossback_strength maps volume_ratio to 1-5 rating."""
    assert _crossback_strength(1.5) == 3
    assert _crossback_strength(1.9) == 3
    assert _crossback_strength(2.0) == 4
    assert _crossback_strength(2.9) == 4
    assert _crossback_strength(3.0) == 5
    assert _crossback_strength(10.0) == 5


def test_detect_crossback_handles_tz_naive_signal_date_with_tz_aware_index():
    """Regression: tz-naive signal_date + tz-aware df.index → no TypeError.

    fetch() in live mode returns a tz-aware UTC index; signal_date is parsed
    from a plain "YYYY-MM-DD" string and is tz-naive. Without the
    tz-aware fix in detect_crossback, `df.index >= signal_date` raises
    TypeError. This test pins the fix.
    """
    # Reuse the breakdown-recovery-with-volume fixture (70 rows, 1 crossback signal)
    closes = list(np.linspace(10, 20, 40)) + [10] * 10 + [13.0] + [13.0] * 19
    vols = [1_000_000] * 50 + [2_000_000] * 20
    df = _make_df(closes, volumes=vols)
    # Make the index tz-aware (mimics fetch() live-mode behavior)
    df.index = df.index.tz_localize("UTC")
    # signal_date is tz-naive (parsed from a plain "YYYY-MM-DD" string in callers)
    signal_date = pd.Timestamp("2026-01-01")
    assert signal_date.tz is None
    assert df.index.tz is not None
    # Should not raise TypeError; should produce same result as tz-naive case.
    signals = detect_crossback(df, signal_date=signal_date)
    assert len(signals) == 1
    assert signals[0]["close"] == 13.0
