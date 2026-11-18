"""第一层（Weinstein Stage 2）的放量确认不能跨量纲比量。

2026-11-18 实测：行情 pkl 在 2026-10-23 前后换过数据源（Yahoo 合并量 →
Alpaca IEX），文件里前段合并量、后段 IEX，量差 20~50 倍。`weinstein_strategy`
的放量确认用 `volume.iloc[-21:-1].mean()` —— 前 20 根均量里混进几根合并量 bar，
均量被抬高一个数量级 → **量比被压到 0.0x → 技术条件全过的票照样被误杀**。

实测 2026-11-17 有 9 只票因此丢掉信号，其中 ATER / VAI / USEA / LOBO
在同源口径下分别是 4.78x / 8.68x / 6.90x / 3.59x —— 都是合格的放量突破。

修法：均量只跟**同口径**（IEX vs 合并量）的历史比，见 `data/daily_bars.py`。
"""
from datetime import datetime, timezone

import pandas as pd

from events import EventQueue
from strategies.v_weinstein.weinstein_strategy import WeinsteinStrategy

# 跑批时刻必须在最后一根 bar 之后
_RUN_AT = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

CONSOLIDATED_VOL = 5_000_000     # 合并量 bar
IEX_VOL = 100_000                 # IEX bar（量差 50 倍）


def _strategy(market_data: dict) -> WeinsteinStrategy:
    cfg = {
        "sma_long": 150,
        "trend_lookback": 30,
        "pivot_lookback": 30,
        "min_breakout_pct": 0.005,
        "volume_mult": 1.5,
        "stop_loss_pct": 0.10,
        "trailing_stop_pct": 0.18,
        "time_stop_days": 30,
        "time_stop_min_gain": 0.05,
        "max_stale_bars": 3,
    }
    return WeinsteinStrategy(cfg, market_data, live_mode=True)


def _frame(n: int, iex_tail: int, today_volume: float,
           consolidated: bool = True) -> pd.DataFrame:
    """前段合并量 bar（00:00 UTC）+ 末段 IEX bar（04:00 UTC）。

    iex_tail = 末段有多少根是 IEX bar —— 换源发生得越近，20 日均量窗口里
    混进来的合并量越多，量比被压得越狠。
    """
    dates = pd.date_range(end=pd.Timestamp("2026-11-17", tz="UTC"),
                          periods=n, freq="B", tz="UTC")
    index = pd.DatetimeIndex(
        [d.replace(hour=4) if i >= n - iex_tail else d.replace(hour=0)
         for i, d in enumerate(dates)]
    )
    closes = [10.0 + i * 0.02 for i in range(n)]
    closes[-1] = closes[-2] * 1.03          # 最后一根放量突破整理区
    vols = []
    for i in range(n):
        if i >= n - iex_tail:
            vols.append(today_volume if i == n - 1 else IEX_VOL)
        else:
            vols.append(CONSOLIDATED_VOL if consolidated else IEX_VOL)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": vols}, index=index)


def test_breakout_survives_when_volume_window_spans_two_sources():
    """换源后技术条件全过 + 同源放量 2x → 必须发信号（修之前被量比 0.24x 误杀）。"""
    df = _frame(220, iex_tail=17, today_volume=200_000)

    strategy = _strategy({"TEST": df})
    signals = strategy.run_date(_RUN_AT, EventQueue())

    assert len(signals) == 1, "同源口径 2.0x 放量突破不该被误杀"
    assert signals[0].symbol == "TEST"


def test_polluted_ratio_would_have_killed_the_signal():
    """前提核对：按旧的（跨口径）算法算，量比确实不到 1.5x。"""
    df = _frame(220, iex_tail=17, today_volume=200_000)
    polluted_avg = float(df["volume"].iloc[-21:-1].mean())
    polluted_ratio = float(df["volume"].iloc[-1]) / polluted_avg
    assert polluted_ratio < 1.5, f"前提不成立：跨口径量比已有 {polluted_ratio:.2f}x"


def test_single_source_file_keeps_its_ratio_unchanged():
    """没换过源的文件 → 量比口径不变（对照组）。"""
    df = _frame(220, iex_tail=220, today_volume=200_000)

    strategy = _strategy({"TEST": df})
    signals = strategy.run_date(_RUN_AT, EventQueue())

    assert len(signals) == 1
    # 全 IEX → 均量 = 100,000 → 量比 2.0x
    assert "2.0x" in signals[0].data["reason"]


def test_insufficient_volume_still_blocks_the_signal():
    """同源口径下量能不够 → 照样不发信号（修复不能变成「放宽门槛」）。"""
    df = _frame(220, iex_tail=17, today_volume=120_000)   # 同源 1.2x < 1.5x

    strategy = _strategy({"TEST": df})
    signals = strategy.run_date(_RUN_AT, EventQueue())

    assert signals == [], "1.2x 同源量比不够 1.5x，不该发信号"
