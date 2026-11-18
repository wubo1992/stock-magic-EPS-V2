"""日线「20 日均量」不能跨量纲平均 —— 合并量 bar 和 IEX bar 的量差 20~50 倍。

2026-11-18 实测故障：行情 pkl 在 2026-10-23 前后换过数据源
（Yahoo 合并量 bar → Alpaca IEX bar）。文件里前半段是合并量、后半段是 IEX，
于是「前 20 根均量」把两个量纲混在一起 → 均量被抬高 20~50 倍 → 量比被压到
0.0x → **放量门槛永远不成立，真信号被误杀**。

实测被误杀的（技术条件全过，只因量窗被污染）：
    ATER 0.27x → 同源 4.78x    LOBO 0.002x → 同源 3.59x
    VAI  0.28x → 同源 8.68x    USEA 0.66x → 同源 6.90x

`data/stage.py` 的周线量比早就是按**同口径**比的（`volume_regime`），
日线这边一直没跟上。

口径判定沿用 stage.py 的约定：
    04:00 / 05:00 UTC = IEX（美东当日零点，夏令时差一小时）
    其余（00:00 UTC）= 合并量
"""
import numpy as np
import pandas as pd
import pytest

from data.daily_bars import (
    REGIME_CONSOLIDATED,
    REGIME_IEX,
    same_regime_volume_ma,
    volume_regimes,
)


def _mixed_frame(consolidated: int = 3, iex: int = 18,
                 consolidated_vol: float = 5_000_000,
                 iex_vol: float = 100_000) -> pd.DataFrame:
    """前 consolidated 根是合并量 bar（00:00 UTC），后 iex 根是 IEX bar（04:00 UTC）。"""
    dates = pd.date_range(end=pd.Timestamp("2026-11-17", tz="UTC"),
                          periods=consolidated + iex, freq="B", tz="UTC")
    hours = [0] * consolidated + [4] * iex
    index = pd.DatetimeIndex([d.replace(hour=h) for d, h in zip(dates, hours)])
    vols = [consolidated_vol] * consolidated + [iex_vol] * iex
    close = [10.0] * (consolidated + iex)
    return pd.DataFrame({"open": close, "high": close, "low": close,
                         "close": close, "volume": vols}, index=index)


# ── 口径判定 ──────────────────────────────────────────────────────────────

def test_iex_and_consolidated_regimes_are_distinguished():
    """04:00 / 05:00 UTC 是 IEX；00:00 UTC 是合并量。"""
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-11-16", tz="UTC"),          # 00:00 → 合并量
        pd.Timestamp("2026-11-16 04:00", tz="UTC"),    # 04:00 → IEX
        pd.Timestamp("2026-11-17 05:00", tz="UTC"),    # 05:00 → IEX（EST）
    ])
    regimes = volume_regimes(idx)
    assert list(regimes) == [REGIME_CONSOLIDATED, REGIME_IEX, REGIME_IEX]


# ── 同口径均量 ───────────────────────────────────────────────────────────

def test_clean_file_matches_plain_rolling_mean():
    """单一口径的文件 → 结果必须与原来的 rolling(20).mean().shift(1) **完全一致**。

    这是「修 bug 不改行为」的锁：绝大多数文件根本没有换过源，它们的量比不能动。
    """
    df = _mixed_frame(consolidated=40, iex=0)
    df["volume"] = np.linspace(1_000_000, 2_000_000, len(df))

    expected = df["volume"].rolling(20).mean().shift(1)
    got = same_regime_volume_ma(df, window=20)

    pd.testing.assert_series_equal(got, expected, check_names=False)


def test_consolidated_bars_do_not_pollute_iex_window():
    """IEX 段的均量里不能混进合并量的量。"""
    df = _mixed_frame(consolidated=3, iex=18)
    got = same_regime_volume_ma(df, window=20)

    # 最后 20 根里有 17 根 IEX + 3 根合并量 → 普通均量被抬高 8 倍
    plain = df["volume"].rolling(20).mean().shift(1).iloc[-1]
    assert plain == pytest.approx(835_000)

    # 同口径均量 = IEX 的量本身
    assert got.iloc[-1] == pytest.approx(100_000)


def test_ratio_after_source_switch_is_readable():
    """换源后量比要回到人能看懂的量级（1x = 与同口径均量持平）。"""
    df = _mixed_frame(consolidated=3, iex=20)
    df.iloc[-1, df.columns.get_loc("volume")] = 200_000   # IEX 日均量的 2 倍

    avg = same_regime_volume_ma(df, window=20).iloc[-1]
    assert float(df["volume"].iloc[-1]) / avg == pytest.approx(2.0)


def test_iex_bars_keep_their_own_average_during_the_switch():
    """刚换源的头几根 IEX bar：同口径样本不足 → 退回普通均量（而不是 NaN）。

    退回而不是置 NaN，是为了让状态机不跳过这些 bar（跳过 = 可能漏掉上穿事件）。
    """
    df = _mixed_frame(consolidated=20, iex=20)
    got = same_regime_volume_ma(df, window=20, min_samples=4)

    iex_start = 20
    for i in range(iex_start, iex_start + 4):
        plain = df["volume"].rolling(20).mean().shift(1).iloc[i]
        assert got.iloc[i] == pytest.approx(plain)


def test_young_file_stays_nan_like_before():
    """历史不够 20 根的位置 → 还是 NaN（与修复前一致，不凭空给出量比）。"""
    df = _mixed_frame(consolidated=15, iex=5)
    got = same_regime_volume_ma(df, window=20)

    assert got.iloc[:15].isna().all()


def test_empty_frame_does_not_crash():
    """空表 / 没有 volume 列 → 返回空 Series，不抛异常。"""
    empty = pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))
    assert same_regime_volume_ma(empty).empty

    no_vol = pd.DataFrame({"close": [1.0, 2.0]},
                           index=pd.DatetimeIndex([pd.Timestamp("2026-11-16", tz="UTC"),
                                                   pd.Timestamp("2026-11-17", tz="UTC")]))
    assert same_regime_volume_ma(no_vol).isna().all()
