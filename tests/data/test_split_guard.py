"""反向拆股的价格尺度断裂检测（从母项目 克劳德V3.1/signal_system/data/split_guard.py 移植）。

为什么需要它：
反向拆股（reverse split）当天价格机械地跳涨几倍到几十倍（1:30 → 收盘价变 30 倍）。
EMA10 是**滚动**均线，拆股前后混了两个价格基准 → 「跌破 EMA10」「上穿 EMA10」
变成跨尺度比较的假象。

判据用「倍数」而不是「涨幅超过 X%」：
STAK 某天涨 +84%（比值 1.84）是真行情，被涨幅阈值拦掉就损失了一个真信号；
而 8 倍以上（+700%）的日间跳空在真实市场里几乎不存在，必是机械事件。
"""
import pandas as pd
import pytest

from data.split_guard import MEGA_SPLIT_RATIO, has_split_gap


def _df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=dates)


def test_mega_upward_jump_is_detected():
    """8 倍以上的机械跳涨必须被识别。"""
    df = _df([10.0] * 30 + [300.0])
    assert has_split_gap(df) is True


def test_mega_downward_jump_is_detected():
    """反向的机械跳空（1/30）同样要识别。"""
    df = _df([300.0] * 30 + [10.0])
    assert has_split_gap(df) is True


def test_real_daily_move_is_not_a_split():
    """+84% 的真实暴涨不是机械事件，不该被误杀。"""
    df = _df([10.0] * 30 + [18.4])
    assert has_split_gap(df) is False


def test_flat_series_is_not_a_split():
    df = _df([10.0] * 40)
    assert has_split_gap(df) is False


def test_gap_outside_lookback_window_is_ignored():
    """窗口外的机械跳空不影响当前判断（EMA10 也已经暖过来了）。"""
    df = _df([10.0] * 30 + [300.0] + [300.0] * 40)
    assert has_split_gap(df, lookback=30) is False


def test_empty_and_short_series_are_safe():
    """数据不足 / 无 close 列时不能抛异常。"""
    assert has_split_gap(pd.DataFrame({"close": [10.0]})) is False
    assert has_split_gap(pd.DataFrame({"open": [1.0]})) is False
    assert has_split_gap(None) is False


def test_mega_split_ratio_is_eight():
    assert MEGA_SPLIT_RATIO == 8.0
