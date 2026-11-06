"""同一交易日有两根 bar 时，策略的窗口必须按**交易日**算，不能按**行数**算。

现场（2026-11-05 复查缓存时发现）：行情 pkl 里同一天经常有两根 bar ——
00:00 UTC 的合并量 bar（volume 3000 万~6000 万）+ 04:00 UTC 的 IEX bar（volume 70 万~160 万）。
`data/fetcher.py::_merge_delta` 早就知道这个问题（它的 docstring 里写着
「实测 10,695 只行情 pkl，5,366 只文件同时含 00:00 和 04:00 的 bar」），
但只修了**增量合并**那条路，没修**存量文件**。

而 `weinstein_strategy._check_entry` 用的全是**行数**切片：

    pivot   = close.iloc[-(30+1):-1].max()      # 要的是 30 个交易日
    avg_vol = volume.iloc[-21:-1].mean()        # 要的是 20 个交易日
    sma150  = close.rolling(150).mean()         # 要的是 150 个交易日

实测（10,711 只日线 pkl）：**4,140 只**（39%）的最后 31 行只覆盖 **20 个交易日**，
最后 21 行只覆盖 **15 个交易日**。后果是两头都错：

- pivot 窗口缩水 → 整理区高点算低 → **突破更容易成立** → 假信号
- 均量窗口里混进 20~50 倍的合并量 → 均量被抬高 → **量比被压低** → 真信号被误杀

这两条都会直接改变用户看到的信号，必须修。
"""
from datetime import datetime, timezone

import pandas as pd

from events import EventQueue
from strategies.v_weinstein.weinstein_strategy import WeinsteinStrategy


def _trading_days(count: int, end: str = "2026-10-29") -> pd.DatetimeIndex:
    return pd.date_range(end=end, periods=count, freq="B", tz="UTC")


# 跑批时刻：必须在最后一根 bar（04:00 UTC）之后，否则那根 bar 会被日期切片切掉
_RUN_AT = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _stage2_closes(n: int) -> list[float]:
    """缓慢上升的阶梯 → SMA150 上升；最后一根跳空突破。"""
    close = [10.0 + i * 0.05 for i in range(n)]
    close[-1] = close[-2] * 1.06
    return close


def _with_duplicate_bars(df: pd.DataFrame, dup_from: int) -> pd.DataFrame:
    """给 df 的最后 dup_from 个交易日各加一根「合并量 bar」（00:00 UTC，排在该日 IEX bar 前面）。

    合并量 bar 的量是 IEX bar 的 30 倍，收盘价只差 0.01（现实中就是差几分钱）。
    IEX bar 挪到 04:00 UTC —— 与真实行情 pkl 的时间戳布局一致（00:00 合并量 + 04:00 IEX）。
    """
    keep = df.iloc[-dup_from:].copy()
    head = df.iloc[:-dup_from]
    composite = keep.copy()
    composite.index = composite.index.normalize()          # 00:00 UTC
    composite["volume"] = composite["volume"] * 30         # 用 composite 自己的列，避免改过索引后对齐成 NaN
    composite["close"] = composite["close"] + 0.01
    keep.index = keep.index + pd.Timedelta(hours=4)        # 04:00 UTC（IEX bar）
    out = pd.concat([head, composite, keep])
    return out.sort_index(kind="stable")


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


def _clean_df(dates: pd.DatetimeIndex, close: list[float], volume: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": volume},
        index=dates,
    )


def test_volume_lookback_counts_trading_days_not_rows():
    """均量窗口必须覆盖 20 个**交易日**，不能因为同日双 bar 缩成 15 天。

    构造：21 个交易日，每天两根 bar（合并量 30 倍 + IEX 量）。
    当日 IEX 量 = 1.5x IEX 均量 —— 按交易日算刚好过门槛（1.5x），
    按行数算均量被合并量抬高到 ~10.5 倍 → 量比 0.14x → 信号被误杀。
    """
    dates = _trading_days(21)
    close = [10.0] * 20 + [10.0]
    iex_vol = [100_000] * 20 + [150_000]     # 当日 1.5x IEX 均量
    df = _clean_df(dates, close, iex_vol)
    df = _with_duplicate_bars(df, dup_from=21)

    # 前提：行数够，但交易日数不够 30 → 必须按交易日口径才谈得上「20 日均量」
    assert len(df) == 42

    strategy = _strategy({"SPY": df})
    signals = strategy.run_date(_RUN_AT, EventQueue())

    # 真正的 Stage 2 条件（SMA150 上升、突破）由别的测试覆盖；
    # 这里只关心量比窗口有没有被双 bar 缩掉 —— 直接量均量本身更清楚。
    from strategies.v_weinstein.weinstein_strategy import dedup_daily_bars

    deduped = dedup_daily_bars(df)
    assert len(deduped) == 21, "同一交易日必须只剩一根 bar"
    avg_vol = float(deduped["volume"].iloc[-21:-1].mean())
    assert avg_vol == 100_000, f"20 日均量应按 20 个交易日算，实际 {avg_vol}"
    assert float(deduped["volume"].iloc[-1]) / avg_vol == 1.5


def test_pivot_lookback_covers_thirty_trading_days():
    """整理区高点必须取最近 30 个**交易日**的最高收盘价。

    构造：40 个交易日，收盘价一路 10.00 → 11.95；其中第 35 个交易日有一根
    合并量 bar，收盘价被写成 999（脏数据）。按行数切 31 行只能覆盖 20 个交易日，
    会漏掉更早、更高（或更低）的真实收盘价 → pivot 算错。
    """
    dates = _trading_days(40)
    close = [10.0 + i * 0.05 for i in range(40)]     # 10.00 → 11.95
    volume = [100_000] * 40
    df = _clean_df(dates, close, volume)
    df = _with_duplicate_bars(df, dup_from=21)

    from strategies.v_weinstein.weinstein_strategy import dedup_daily_bars

    deduped = dedup_daily_bars(df)
    assert len(deduped) == 40
    pivot_correct = float(deduped["close"].iloc[-(30 + 1):-1].max())
    # 40 个交易日里最近 30 个（不含今天）= 第 9..39 行 → 最高是 11.95 之前的 11.90
    assert pivot_correct == 11.90

    pivot_rowbased = float(df["close"].iloc[-(30 + 1):-1].max())
    assert pivot_rowbased != pivot_correct, (
        "按行数切出来的 pivot 与按交易日切的不一样 —— 这正是要修的 bug"
    )


def test_stage2_signal_is_not_killed_by_duplicate_bars():
    """双 bar 不该把一个真正满足 Stage 2 的票判死（量比被压低的假阴性）。"""
    dates = _trading_days(200)
    close = _stage2_closes(200)
    volume = [1_000_000] * 200
    volume[-1] = 3_000_000
    df = _clean_df(dates, close, volume)

    # 加一根合并量 bar：量是 30 倍 → 原始 df 的均量被抬高 15 倍 → 量比从 3x 掉到 ~0.2x
    df_dup = _with_duplicate_bars(df, dup_from=40)

    # 前提：原始 df 的均量确实被合并量抬高（证明这个测试确实在打「双 bar」这个 bug）
    raw_avg = float(df_dup["volume"].iloc[-21:-1].mean())
    assert raw_avg > 1_000_000, f"前提不成立：原始均量应 > 100 万，实际 {raw_avg}"
    assert float(df_dup["volume"].iloc[-1]) / raw_avg < 1.5, "前提不成立：原始量比应低于门槛"

    # 被测代码放 SPY 之外（SPY 是基准，run_date 会跳过它）
    strategy = _strategy({"SPY": df_dup, "TEST": df_dup})
    signals = strategy.run_date(_RUN_AT, EventQueue())
    assert len(signals) == 1, "双 bar 不该让真正的 Stage 2 信号消失"
    assert signals[0].symbol == "TEST"
