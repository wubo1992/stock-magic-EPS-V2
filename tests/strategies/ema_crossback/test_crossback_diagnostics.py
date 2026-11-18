"""第二层「今天 0 个回踩」时，必须说得清每一只候选卡在哪个协议条件上。

2026-11-18 实测：当天 0 个回踩新增。报告只写「0 个」等于什么都没说 ——
用户分不清「市场真的没有机会」和「协议把机会挡掉了」，两种情况的行动完全不同。
（那天 SKWD 只差一天就新鲜度过期，HNGE 则是又跌回 EMA10 下方。）

`diagnose_crossback()` 对**最近一次上穿 EMA10 事件**逐条件判定：

    ① 曾跌破 EMA10        —— 状态机隐含保证（没跌破过就不会有上穿事件）
    ② 当前仍站回 EMA10 上方
    ③ 穿回日放量 ≥ volume_mult × 20 日均量
    ④ 穿回日距今 ≤ max_days_since_crossback 天

关键点：`diagnose_crossback` 必须能看到**被量能挡掉**的事件 ——
`detect_crossback` 在量能不足时压根不记录这个事件（直接丢弃），
所以诊断必须走另一条扫描路径，而不是复用 detect_crossback 的返回值。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    detect_crossback,
    diagnose_crossback,
)

TODAY = datetime(2026, 11, 12, tzinfo=timezone.utc)
LAST_CLOSE = pd.Timestamp("2026-11-11", tz="UTC")
TOTAL = 60
CROSS_IDX = 56           # 2026-11-06，在 7 天新鲜度窗口内
SIGNAL_DATE = pd.Timestamp("2026-08-01")


def _df(cross_idx: int = CROSS_IDX, total: int = TOTAL,
        cross_volume: float = 3_000_000,
        tail_closes: list[float] | None = None) -> pd.DataFrame:
    """先跌破 EMA10 → 在 cross_idx 那天放量上穿。

    前 20 天 10 → 20（EMA10 暖起来），之后趴在 10（跌破 EMA10），
    cross_idx 那天跳到 22（> EMA10）→ 上穿事件。
    """
    closes = list(np.linspace(10, 20, 20)) + [10.0] * (total - 20)
    for i in range(cross_idx, total):
        closes[i] = 22.0
    if tail_closes:
        for offset, val in enumerate(tail_closes):
            closes[total - len(tail_closes) + offset] = val
    vols = [1_000_000] * total
    vols[cross_idx] = cross_volume
    dates = pd.date_range(end=LAST_CLOSE, periods=total, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": vols},
        index=dates,
    )


def _check(result: dict, cid: str) -> dict:
    return next(c for c in result["checks"] if c["id"] == cid)


def test_no_crossback_at_all_returns_none():
    """从没跌破过 EMA10 → 压根没有上穿事件 → 没有可诊断的东西。"""
    flat = pd.DataFrame(
        {"open": [10.0] * 40, "high": [10.0] * 40, "low": [10.0] * 40,
         "close": [10.0] * 40, "volume": [1_000_000] * 40},
        index=pd.date_range(end=LAST_CLOSE, periods=40, freq="B", tz="UTC"),
    )
    assert diagnose_crossback(flat, SIGNAL_DATE, today=TODAY) is None


def test_all_conditions_pass():
    """标准场景：上穿后仍站 EMA10 上方、放量、新鲜 → 全部通过。"""
    df = _df()
    result = diagnose_crossback(df, SIGNAL_DATE, today=TODAY)

    assert result is not None
    assert result["passed"] is True
    assert result["blocked_by"] == []
    assert _check(result, "②")["passed"] is True
    assert _check(result, "③")["passed"] is True
    assert _check(result, "④")["passed"] is True
    assert result["date"].date().isoformat() == "2026-11-06"


def test_condition2_fails_when_price_fell_back_below_ema10():
    """上穿之后又跌回 EMA10 下方 → 条件② 不成立（那次上穿已作废）。"""
    df = _df(tail_closes=[10.0, 10.0, 10.0])
    result = diagnose_crossback(df, SIGNAL_DATE, today=TODAY)

    assert result is not None
    assert result["passed"] is False
    assert "②" in result["blocked_by"]
    assert _check(result, "②")["passed"] is False
    assert _check(result, "②")["detail"], "条件②失败时必须给出具体数字"


def test_condition3_fails_when_volume_insufficient():
    """量能不足的上穿 —— detect_crossback 压根不记录它，诊断必须看得见。"""
    df = _df(cross_volume=500_000)          # 量比 0.5x < 1.0x
    result = diagnose_crossback(df, SIGNAL_DATE, today=TODAY)

    assert result is not None
    assert "③" in result["blocked_by"]
    assert _check(result, "③")["passed"] is False
    assert "0.5" in _check(result, "③")["detail"]

    # 同一只票：detect_crossback 不发信号（行为不变）
    assert detect_crossback(df, SIGNAL_DATE, max_days_since_crossback=7,
                            require_currently_above=True, today=TODAY) == []


def test_condition4_fails_when_too_old():
    """上穿发生太久 → 条件④ 不成立。"""
    df = _df(cross_idx=40)                   # 20 个交易日前的上穿
    expected = (TODAY.date() - df.index[40].date()).days
    assert expected > 7, "前提不成立：这只票的上穿本来就在新鲜度窗口内"

    result = diagnose_crossback(df, SIGNAL_DATE, today=TODAY)

    assert result is not None
    assert "④" in result["blocked_by"]
    assert _check(result, "④")["passed"] is False
    assert result["days_since"] == expected
    assert str(expected) in _check(result, "④")["detail"]


def test_detect_crossback_behaviour_unchanged():
    """重构 detect_crossback 后的回归锁：正常上穿照样发信号，字段齐全。"""
    df = _df()
    events = detect_crossback(df, SIGNAL_DATE, max_days_since_crossback=7,
                              require_currently_above=True, today=TODAY)

    assert len(events) == 1
    ev = events[0]
    assert ev["date"].date().isoformat() == "2026-11-06"
    assert ev["volume_ratio"] == pytest.approx(3.0)
    assert ev["strength"] == 5
    assert set(ev) == {"date", "close", "ema10", "volume", "avg_vol_20",
                       "volume_ratio", "strength"}


def test_diagnose_reports_multiple_blocking_conditions():
    """又跌回 EMA10 下方 + 又过期 → 两个条件同时挡。"""
    df = _df(cross_idx=40, tail_closes=[10.0, 10.0, 10.0])
    result = diagnose_crossback(df, SIGNAL_DATE, today=TODAY)

    assert result is not None
    assert set(result["blocked_by"]) == {"②", "④"}
