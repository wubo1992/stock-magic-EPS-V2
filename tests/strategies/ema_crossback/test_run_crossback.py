"""Tests for run_ema_crossback_strategy orchestrator and helpers."""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    _get_df_for_symbol,
    run_ema_crossback_strategy,
)


def _make_df(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
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


def test_get_df_for_symbol_prefers_in_memory(tmp_path: Path):
    """_get_df_for_symbol should return in-memory market_data first."""
    df_in_mem = _make_df([10.0] * 60)
    df_disk = _make_df([99.0] * 60)
    df_disk.to_pickle(tmp_path / "SYMBOL.pkl")

    result = _get_df_for_symbol("SYMBOL", market_data={"SYMBOL": df_in_mem}, cache_dir=tmp_path)
    pd.testing.assert_frame_equal(result, df_in_mem)


def test_get_df_for_symbol_falls_back_to_cache(tmp_path: Path):
    """When symbol not in memory, when symbol not in market_data, read from cache pkl."""
    df_disk = _make_df([10.0] * 60)
    df_disk.to_pickle(tmp_path / "SYM.pkl")

    result = _get_df_for_symbol("SYM", market_data={}, cache_dir=tmp_path)
    pd.testing.assert_frame_equal(result, df_disk)


def test_get_df_for_symbol_returns_none_when_missing(tmp_path: Path):
    """When symbol not in memory and not in cache, return None."""
    result = _get_df_for_symbol("MISSING", market_data={}, cache_dir=tmp_path)
    # Without fetch() fallback in tests, this should return None
    assert result is None


def test_run_returns_signal_event_for_crossback(tmp_path: Path):
    """run_ema_crossback_strategy should emit SignalEvent when crossback detected."""
    signal_date_str = "2026-07-15"
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": signal_date_str,
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": today.isoformat(),
            "expires_at": today.isoformat(),
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }

    # Build data that simulates: pre-signal (above EMA) + post-signal (below EMA, then crossback up)
    closes = list(np.linspace(10, 20, 40)) + [10] * 10 + [13.0] + [13.0] * 19  # 70 rows
    vols = [1_000_000] * 50 + [2_000_000] * 20
    df = _make_df(closes, volumes=vols)
    df.index = pd.date_range(start="2026-06-01", periods=len(closes), freq="B")
    signal_ts = pd.Timestamp(signal_date_str)

    market_data = {"CRDL": df, "SPY": _make_df([400.0] * 60)}

    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        events = run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    assert len(events) == 1
    evt = events[0]
    assert evt.symbol == "CRDL"
    assert evt.data["signal_type"] == "crossback_buy"
    assert evt.data["strength"] >= 3
    assert evt.data["stop_loss"] == 9.2


def test_run_skips_watchlist_with_no_data(tmp_path: Path):
    """When watchlist symbol has no data anywhere, skip silently."""
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "MISSING": {
            "symbol": "MISSING",
            "signal_date": "2026-07-15",
            "stage2_stop": 9.2,
        }
    }
    market_data = {"SPY": _make_df([400.0] * 60)}

    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        events = run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    assert events == []


def test_run_updates_watchlist_crossback_count(tmp_path: Path):
    """After detecting, run_ema_crossback_strategy should update watchlist crossback_count."""
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-07-15",
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": today.isoformat(),
            "expires_at": today.isoformat(),
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }
    closes = list(np.linspace(10, 20, 40)) + [10] * 10 + [13.0] + [13.0] * 19  # 70 rows
    vols = [1_000_000] * 50 + [2_000_000] * 20
    df = _make_df(closes, volumes=vols)
    df.index = pd.date_range(start="2026-06-01", periods=len(closes), freq="B")
    market_data = {"CRDL": df, "SPY": _make_df([400.0] * 60)}

    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    # Watchlist should be updated in-place (crossback_count++)
    assert watchlist["CRDL"]["crossback_count"] == 1
    assert watchlist["CRDL"]["last_crossback_date"] is not None
