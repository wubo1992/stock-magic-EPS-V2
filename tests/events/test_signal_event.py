"""Tests for SignalEvent.create() schema extensions."""
from datetime import datetime, timezone

import pytest

from events import SignalEvent
from events.base import V_EPS_V2_BUY_SIGNAL_TYPE, CROSSBACK_BUY_SIGNAL_TYPE


def test_signal_event_default_signal_type_is_v_eps_v2_buy():
    """SignalEvent.create() should default signal_type to V_EPS_V2_BUY_SIGNAL_TYPE for backward compat."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    )
    assert evt.data["signal_type"] == V_EPS_V2_BUY_SIGNAL_TYPE


def test_signal_event_accepts_explicit_signal_type():
    """SignalEvent.create() should accept signal_type=CROSSBACK_BUY_SIGNAL_TYPE."""
    evt = SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="crossback test",
        signal_type=CROSSBACK_BUY_SIGNAL_TYPE,
    )
    assert evt.data["signal_type"] == CROSSBACK_BUY_SIGNAL_TYPE


def test_signal_event_accepts_signal_price():
    """SignalEvent.create() should store signal_price in event.data."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
        signal_price=152.34,
    )
    assert evt.data["signal_price"] == 152.34


def test_signal_event_signal_price_defaults_to_none():
    """If signal_price not provided, it should be None (backward compat)."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    )
    assert evt.data["signal_price"] is None