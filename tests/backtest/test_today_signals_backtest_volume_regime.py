"""回测脚本里的 20 日均量也不能跨量纲比。

`backtest/today_signals_backtest.py` 的 `find_stage2_signals` 用
`volume.shift(1).rolling(20).mean()` 算均量。行情 pkl 在 2026-10-23 前后换过
数据源（Yahoo 合并量 → Alpaca IEX），量差 20~50 倍 → 窗口里混进几根合并量 bar
→ 均量被抬高一个数量级 → 量比被压到 0.0x → **回测里那些天的 Stage 2 信号被
凭空抹掉**（历史胜率被系统性低估）。

修法与策略侧一致：均量只跟**同口径**（IEX vs 合并量）的历史比，
见 `data/daily_bars.py::same_regime_volume_ma`。
"""
import numpy as np
import pandas as pd

from backtest.today_signals_backtest import find_stage2_signals

CONSOLIDATED_VOL = 5_000_000
IEX_VOL = 100_000


def _frame(total: int = 250, iex_tail: int = 20,
           breakout_volume: float = 200_000) -> pd.DataFrame:
    """稳步上涨 + 最后一根放量突破；末段 iex_tail 根是 IEX bar（04:00 UTC）。"""
    dates = pd.date_range(end="2026-11-17", periods=total, freq="B", tz="UTC")
    index = pd.DatetimeIndex(
        [d.replace(hour=4) if i >= total - iex_tail else d.replace(hour=0)
         for i, d in enumerate(dates)]
    )
    closes = list(np.linspace(10.0, 20.0, total - 1)) + [20.0 * 1.01]
    vols = [CONSOLIDATED_VOL] * (total - iex_tail) + [IEX_VOL] * iex_tail
    vols[-1] = breakout_volume
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": vols}, index=index)


def test_stage2_signal_survives_when_volume_window_spans_two_sources():
    """换源后同源 2.0x 放量突破 → 必须仍被识别为 Stage 2 触发日。"""
    df = _frame()

    signals = find_stage2_signals(df)

    assert len(signals) == 1, "同源 2.0x 的突破不该被跨口径均量误杀"
    assert signals[-1] == df.index[-1]


def test_polluted_ratio_would_have_killed_the_signal():
    """前提核对：按旧的（跨口径）算法算，量比确实不到 1.5x。"""
    df = _frame()
    polluted_avg = float(df["volume"].shift(1).rolling(20).mean().iloc[-1])
    polluted_ratio = float(df["volume"].iloc[-1]) / polluted_avg
    assert polluted_ratio < 1.5, f"前提不成立：跨口径量比已有 {polluted_ratio:.2f}x"


def test_single_source_file_keeps_its_signals_unchanged():
    """没换过源的文件 → 触发行完全一致（对照组）。"""
    clean = _frame(iex_tail=250)
    mixed = _frame(iex_tail=250)

    assert list(find_stage2_signals(clean)) == list(find_stage2_signals(mixed))


def test_insufficient_volume_still_blocks_the_signal():
    """同源 1.2x < 门槛 1.5x → 不该有信号（修复不能变成放宽门槛）。"""
    df = _frame(breakout_volume=120_000)

    signals = find_stage2_signals(df)

    assert len(signals) == 0
