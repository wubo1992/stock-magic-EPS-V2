"""Shared pytest fixtures for signal_system_v_eps_v2 tests."""
import sys
from pathlib import Path

# Ensure project root is on sys.path so tests can import strategies.*, signals.*, etc.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Return a tmp path mimicking output/{strategy_id}/ structure."""
    out = tmp_path / "output" / "v_eps_v2"
    out.mkdir(parents=True)
    return out


@pytest.fixture
def make_ohlcv_df():
    """Factory: build a simple OHLCV DataFrame with given close prices."""

    def _make(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
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

    return _make