"""EMA10 回踩检测也必须按**交易日**算窗口，不能按行数算。

行情 pkl 里同一天经常有两根 bar（00:00 UTC 合并量 + 04:00 UTC IEX），
`detect_crossback` 里：

    df["avg_vol_20"] = df["volume"].rolling(vol_lookback).mean().shift(1)

`rolling(20)` 数的是**行数**。同日双 bar 的文件里，20 行只剩 10~15 个交易日，
而且窗口里混进 20~50 倍的合并量 → 均量被抬高 → 量比被压低 → **真回踩信号被误杀**。

更糟的是循环 `for date, row in df.iterrows()` 会把**同一天的两根 bar 各扫一遍**：
合并量 bar 的量是 IEX 的 30 倍 → 量比轻松过门槛 → **同一天出两条回踩信号**。
"""
from datetime import datetime, timezone

import pandas as pd

from strategies.ema_crossback.ema_crossback_strategy import detect_crossback


def _trading_days(count: int, end: str = "2026-10-29") -> pd.DatetimeIndex:
    return pd.date_range(end=end, periods=count, freq="B", tz="UTC")


def _with_duplicate_bars(df: pd.DataFrame, dup_from: int) -> pd.DataFrame:
    """给最后 dup_from 个交易日各加一根合并量 bar（00:00 UTC），IEX bar 挪到 04:00 UTC。"""
    keep = df.iloc[-dup_from:].copy()
    head = df.iloc[:-dup_from]
    composite = keep.copy()
    composite.index = composite.index.normalize()
    composite["volume"] = composite["volume"] * 30
    composite["close"] = composite["close"] + 0.01
    keep.index = keep.index + pd.Timedelta(hours=4)
    out = pd.concat([head, composite, keep])
    return out.sort_index(kind="stable")


def _crossback_df() -> pd.DataFrame:
    """40 个交易日：先在 EMA10 之下趴 3 天，最后一天放量上穿 → 一次真回踩。"""
    dates = _trading_days(40)
    close = [10.0] * 36 + [9.0, 9.0, 9.0, 10.5]
    volume = [1_000_000] * 39 + [3_000_000]
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": volume},
        index=dates,
    )


def test_crossback_volume_window_counts_trading_days():
    """20 日均量必须覆盖 20 个交易日，双 bar 不该把回踩信号误杀。"""
    df = _crossback_df()
    df_dup = _with_duplicate_bars(df, dup_from=40)

    # 前提：原始 df 的均量被合并量抬高 → 量比被压到门槛以下
    raw_avg = float(df_dup["volume"].rolling(20).mean().shift(1).iloc[-1])
    assert raw_avg > 1_000_000, f"前提不成立：原始均量应 > 100 万，实际 {raw_avg}"
    assert float(df_dup["volume"].iloc[-1]) / raw_avg < 1.5, "前提不成立：原始量比应低于门槛"

    signals = detect_crossback(
        df_dup,
        signal_date=pd.Timestamp("2026-09-01", tz="UTC"),
        ema_period=10,
        vol_mult=1.5,
        vol_lookback=20,
    )
    assert len(signals) == 1, f"双 bar 不该让回踩信号消失，实际 {len(signals)} 条"
    assert signals[0]["volume_ratio"] == 3.0, (
        f"量比应按 20 个交易日算 = 3.0，实际 {signals[0]['volume_ratio']}"
    )


def test_crossback_emits_one_signal_per_trading_day():
    """同一天两根 bar 只能出一条回踩信号，不能按 bar 出一条。"""
    df = _crossback_df()
    df_dup = _with_duplicate_bars(df, dup_from=40)

    signals = detect_crossback(
        df_dup,
        signal_date=pd.Timestamp("2026-09-01", tz="UTC"),
        ema_period=10,
        vol_mult=1.5,
        vol_lookback=20,
    )
    dates = [s["date"].normalize() for s in signals]
    assert len(dates) == len(set(dates)), f"同一天出现多条回踩信号：{dates}"


def test_clean_df_still_detects_crossback():
    """对照组：没有双 bar 的干净数据，行为不变。"""
    df = _crossback_df()
    signals = detect_crossback(
        df,
        signal_date=pd.Timestamp("2026-09-01", tz="UTC"),
        ema_period=10,
        vol_mult=1.5,
        vol_lookback=20,
    )
    assert len(signals) == 1
    assert signals[0]["volume_ratio"] == 3.0
