"""Tests for SignalEvent.create() schema extensions."""
from datetime import datetime, timezone

import pytest

from events import SignalEvent
from events.base import V_EPS_V2_BUY_SIGNAL_TYPE, CROSSBACK_BUY_SIGNAL_TYPE


def test_signal_event_signal_type_defaults_to_none():
    """signal_type 默认留空（None），由 signals/generator.py 按「策略 ID + 方向」推导。

    以前默认是 "v_eps_v2_buy"，于是**别的策略**（v_weinstein、cigar_butt_v1a）的
    信号在各自的 signals.csv 里也被标成 v_eps_v2_buy。
    """
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    )
    assert evt.data["signal_type"] is None


def test_v_eps_v2_strategy_sets_its_signal_type_explicitly():
    """main.py 靠 signal_type == "v_eps_v2_buy" 挑主策略信号进 watchlist，
    所以 v_eps_v2 策略必须显式标注，不能依赖默认值。"""
    from signals.generator import _default_signal_type

    assert _default_signal_type("v_eps_v2", "buy") == V_EPS_V2_BUY_SIGNAL_TYPE
    assert _default_signal_type("v_weinstein", "buy") == "v_weinstein_buy"


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