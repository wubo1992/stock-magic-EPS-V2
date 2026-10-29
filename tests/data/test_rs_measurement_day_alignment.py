"""
ticker 和排名池必须**用同一天的收盘价**算加权收益。

为什么这是硬要求
----------------
RS Rating 是横截面百分位：把 ticker 的加权收益放进全市场的分布里比。
「同一天」有两层含义，任何一层错位都会让百分位失真：

  1. **同一天**：ticker 用哪天，池子就用哪天；
  2. **同一根 bar**：同一天里，ticker 取的收盘价必须和池子里存的是同一根。

第 2 层是最阴的：宽表（`data/rs_universe.py`）的索引是**UTC 零点归一**的日期，
而 pkl 里的日线 bar 的时间戳是**04:00 UTC**（美东当日零点）。

```
data/cache/RCEL.pkl 索引（真实数据，末尾几根）
2026-10-21 00:00:00+00:00
2026-10-21 04:00:00+00:00
2026-10-22 00:00:00+00:00
2026-10-22 04:00:00+00:00
2026-10-23 04:00:00+00:00     <- 只有这根没有 00:00 的孪生 bar
```

标量路径 `_slice_to_date` 用 `df.index <= as_of` 比较**时刻**，而 as_of 是
当天 00:00 UTC —— 于是 10-23 那根 04:00 的 bar 被切掉了。
宽表路径 `_slice_to_asof` 用 `wide.index <= ts.normalize()` 比较**日期**，
10-23 那一行留着。

结果：池子停在 10-23，ticker 停在 10-22。**跨日比较。**

实测偏差（2026-10-26，修复前）：

```
pkl 标量路径 measure_at=10-23 -> RCEL wret = 2.545476   （其实是 10-22 的数）
宽表向量路径      as_of=10-23 -> RCEL wret = 2.488095   （10-23 的数）
```

同一只票、同一个计量日，两个数字。拿到百分位里的就是错的。

而**测试全绿**：现有的 `test_rs_reference_day.py` 造的假数据用
`pd.date_range(..., tz="UTC")` —— 时间戳正好落在 00:00，
跟宽表口径重合，测不出这个错位。只有真实数据才会暴露。

所以本文件锁死：
  1. `_slice_to_date` 必须包含计量日当天的 bar（按日期比，不按时刻比）；
  2. 计量日之后的 bar 必须仍然被排除；
  3. 标量路径和宽表路径在同一天上必须给出**同一个**加权收益。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import data.rs as rs
from data.rs_universe import vectorized_weighted_return

MEASURE_DAY = datetime(2026, 10, 23, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _pkl_style_frame(closes, end_day=datetime(2026, 10, 23, tzinfo=timezone.utc)):
    """
    造一个和真实 pkl 同构的日线帧：**时间戳在 04:00 UTC**（不是零点）。

    真实数据是 Alpaca 写的，时间戳是美东当日零点 = UTC 04:00。
    用 `pd.date_range(tz="UTC")` 造出来的是零点，会让本文件失去意义。
    """
    n = len(closes)
    days = pd.date_range(end=end_day, periods=n, freq="D", tz="UTC")
    idx = pd.DatetimeIndex(days).tz_convert("UTC") + pd.Timedelta(hours=4)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000.0] * n,
        },
        index=idx,
    )


def _wide_from_frame(closes, end_day=datetime(2026, 10, 23, tzinfo=timezone.utc)):
    """同一串收盘价铺成宽表（索引 = UTC 零点归一的日期，和 rs_universe 一致）。"""
    n = len(closes)
    days = pd.date_range(end=end_day, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"TICK": closes}, index=days)


def _wide_from_pkl_frame(df):
    """
    按 `rs_universe._read_close_series_from_df` 的口径把 pkl 帧铺成宽表。

    索引归一到 UTC 零点、同一交易日保留**第一条** —— 和宽表真实构建路径一致。
    """
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    flat = df.copy()
    flat.index = idx.normalize()
    s = pd.to_numeric(flat["close"], errors="coerce")
    s = s[~s.index.duplicated(keep="first")]
    return pd.DataFrame({"TICK": s.sort_index()})


def _drift(n=400, seed=7):
    """
    确定性的随机游走，起点 100。

    **不能用几何漂移**（`100 * 1.001**i`）：那种序列的 252 日收益率
    只跟窗口长度有关，跟窗口从哪天开始无关 —— 少一根 bar 算出来
    一模一样，测不出「少算了一天」。随机游走的 252 日收益率依赖
    起止日，才能把错位暴露出来。
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.012, size=n)
    return list(100.0 * np.cumprod(1.0 + steps))


def test_the_bar_on_the_measurement_day_is_included():
    """
    计量日当天的 bar 必须被切进窗口 —— 它是 04:00 UTC，不在 00:00 之前。

    切掉它，ticker 的窗口就停在昨天，而排名池停在今天（跨日比较）。
    """
    df = _pkl_style_frame(_drift(300))

    sliced = rs._slice_to_date(df, MEASURE_DAY)

    assert len(sliced) == 300, f"切掉了 {300 - len(sliced)} 根 bar"
    assert sliced.index[-1].normalize() == pd.Timestamp("2026-10-23", tz="UTC"), (
        f"最后一根停在 {sliced.index[-1]}，不是计量日当天"
    )


def test_the_bar_after_the_measurement_day_is_still_excluded():
    """修「切早了」不能变成「切晚了」—— 计量日之后的 bar 必须挡在外面。"""
    df = _pkl_style_frame(_drift(300), end_day=datetime(2026, 10, 24, tzinfo=timezone.utc))

    sliced = rs._slice_to_date(df, MEASURE_DAY)

    assert sliced.index[-1].normalize() == pd.Timestamp("2026-10-23", tz="UTC")
    assert len(sliced) == 299, f"计量日之后的 bar 混进来了（{len(sliced)} 根）"


def test_the_ticker_and_the_pool_agree_on_the_measurement_day():
    """
    标量路径和宽表路径，在同一个计量日上必须算出**同一个**加权收益。

    这是整个「统一计量日」的最终形式：两条路径的数字一致，
    百分位才有意义。差一个交易日就是错的。
    """
    closes = _drift(400)
    df = _pkl_style_frame(closes)
    wide = _wide_from_frame(closes)

    scalar = rs._compute_weighted_return(df, MEASURE_DAY, ticker="TICK")
    vector = vectorized_weighted_return(wide, as_of=MEASURE_DAY)["TICK"]

    assert scalar is not None, "标量路径算不出来"
    assert np.isfinite(vector), "宽表路径算不出来"
    assert scalar == pytest.approx(float(vector), rel=1e-9), (
        f"两条路径不同日：标量 {scalar:.6f} vs 宽表 {float(vector):.6f}"
    )


def test_a_ticker_that_moved_on_the_measurement_day_is_measured_on_that_day():
    """
    计量日当天的涨跌必须被算进去。

    如果 `_slice_to_date` 把当天那根切掉，那么「计量日」这个概念
    对 ticker 就是假的 —— 当天涨 50% 和没涨，算出来完全一样。
    """
    closes = _drift(400)
    bumped = list(closes)
    bumped[-1] = closes[-1] * 1.5  # 只在最后一天（= 计量日当天）暴涨

    plain_wret = rs._compute_weighted_return(
        _pkl_style_frame(closes), MEASURE_DAY, ticker="TICK"
    )
    bumped_wret = rs._compute_weighted_return(
        _pkl_style_frame(bumped), MEASURE_DAY, ticker="TICK"
    )

    assert plain_wret is not None and bumped_wret is not None
    assert bumped_wret != pytest.approx(plain_wret), (
        "计量日当天的 bar 根本没被用上 —— 计量日对 ticker 是假的"
    )


def test_a_duplicate_bar_of_the_same_day_keeps_one_close():
    """
    同一交易日的多条 bar（00:00 的 Yahoo bar + 04:00 的 Alpaca bar）
    必须折叠成**一根**，且两条路径挑到的必须是同一根。

    真实数据就是这样：RCEL 的 10-20/10-21/10-22 各有两根。
    标量路径用 groupby.first，宽表路径用 duplicated(keep="first")——
    两边都得先按时间排序，否则「第一条」是随机的。
    """
    closes = _drift(300)
    df = _pkl_style_frame(closes)
    # 塞一根同一天的 00:00 bar（模拟 Yahoo 写的孪生 bar），值故意不同
    twin = pd.Timestamp("2026-10-23", tz="UTC")
    twin_row = df.iloc[[-1]].copy()
    twin_row.index = pd.DatetimeIndex([twin])
    twin_row["close"] = float(closes[-1]) * 1.05
    df = pd.concat([df, twin_row]).sort_index()

    wide = _wide_from_pkl_frame(df)

    scalar = rs._compute_weighted_return(df, MEASURE_DAY, ticker="TICK")
    vector = float(vectorized_weighted_return(wide, as_of=MEASURE_DAY)["TICK"])

    assert scalar == pytest.approx(vector, rel=1e-9), (
        f"同一天的两根 bar 在两条路径上挑到了不同的：标量 {scalar:.6f} vs 宽表 {vector:.6f}"
    )
