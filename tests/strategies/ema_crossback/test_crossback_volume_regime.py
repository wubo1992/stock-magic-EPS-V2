"""第二层（EMA10 回踩）的条件③「放量」不能跨量纲比量。

与第一层同一个病根：行情 pkl 在 2026-10-23 前后换过数据源（Yahoo 合并量 →
Alpaca IEX），量差 20~50 倍。`ema_crossback_strategy` 的 20 日均量用
`volume.rolling(20).mean()` —— 窗口里混进几根合并量 bar，均量被抬高一个数量级
→ 量比被压到 0.0x → **条件③ 永远不成立，合格的上穿被误杀**。

实测 2026-11-18：watchlist 里 7 只票的量窗跨了两个口径（NCPL 0.01x、IMCC 0.00x、
FDBC 0.05x、GCBC 0.32x、CFFI 0.62x、ESP 0.88x、VABK 1.17x）。

修法：均量只跟**同口径**（IEX vs 合并量）的历史比，见 `data/daily_bars.py`。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from strategies.ema_crossback.ema_crossback_strategy import detect_crossback

TODAY = datetime(2026, 11, 18, tzinfo=timezone.utc)
LAST_CLOSE = pd.Timestamp("2026-11-17", tz="UTC")
SIGNAL_DATE = pd.Timestamp("2026-08-01")

CONSOLIDATED_VOL = 5_000_000
IEX_VOL = 100_000


def _df(iex_tail: int = 8, total: int = 39,
        cross_volume: float = 200_000) -> pd.DataFrame:
    """先跌破 EMA10 → 在最后一根 bar 放量上穿。

    前 20 根收盘价 10 → 20（EMA10 暖起来），之后趴在 10（跌破 EMA10），
    最后一根跳到 22（> EMA10）→ 上穿事件。

    iex_tail = 末段有多少根是 IEX bar（04:00 UTC）；更早的是合并量 bar（00:00 UTC）。
    """
    dates = pd.date_range(end=LAST_CLOSE, periods=total, freq="B", tz="UTC")
    index = pd.DatetimeIndex(
        [d.replace(hour=4) if i >= total - iex_tail else d.replace(hour=0)
         for i, d in enumerate(dates)]
    )
    closes = list(np.linspace(10, 20, 20)) + [10.0] * (total - 20)
    closes[-1] = 22.0
    vols = [CONSOLIDATED_VOL] * (total - iex_tail) + [IEX_VOL] * iex_tail
    vols[-1] = cross_volume
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": vols}, index=index)


def test_crossback_survives_when_volume_window_spans_two_sources():
    """换源后上穿 + 同源放量 2x → 必须发信号（修之前量比 0.03x 被误杀）。"""
    df = _df()

    signals = detect_crossback(df, SIGNAL_DATE, vol_mult=1.0,
                               max_days_since_crossback=7,
                               require_currently_above=True, today=TODAY)

    assert len(signals) == 1, "同源 2.0x 放量的上穿不该被误杀"
    assert signals[0]["volume_ratio"] == 2.0


def test_polluted_ratio_would_have_killed_the_signal():
    """前提核对：按旧的（跨口径）算法算，量比确实不到 1.0x。"""
    df = _df()
    polluted_avg = float(df["volume"].rolling(20).mean().shift(1).iloc[-1])
    polluted_ratio = float(df["volume"].iloc[-1]) / polluted_avg
    assert polluted_ratio < 1.0, f"前提不成立：跨口径量比已有 {polluted_ratio:.2f}x"


def test_single_source_file_keeps_its_ratio_unchanged():
    """没换过源的文件 → 量比口径不变（对照组）。"""
    df = _df(iex_tail=39)

    signals = detect_crossback(df, SIGNAL_DATE, vol_mult=1.0,
                               max_days_since_crossback=7,
                               require_currently_above=True, today=TODAY)

    assert len(signals) == 1
    assert signals[0]["volume_ratio"] == 2.0


def test_insufficient_same_regime_volume_still_blocks_the_signal():
    """同源口径下量能不够 → 照样不发信号（修复不能变成「放宽门槛」）。"""
    df = _df(cross_volume=120_000)     # 同源 1.2x... 门槛是 1.0x → 仍应通过

    signals = detect_crossback(df, SIGNAL_DATE, vol_mult=1.5,
                               max_days_since_crossback=7,
                               require_currently_above=True, today=TODAY)

    assert signals == [], "同源 1.2x < 门槛 1.5x，不该发信号"
