"""
增量合并必须按**交易日**去重，不能按时间戳。

为什么这是硬要求
----------------
两个数据源的日线 bar 时间戳不一样：

    Alpaca → 04:00 UTC（美东当日零点）
    Yahoo  → 00:00 UTC

合并时代码按**精确时间戳**去重（`duplicated(keep="last")`），于是同一个
交易日的两根 bar 会**同时留在文件里**。而两套 bar 的价格基准还不一样：

    Alpaca adjustment="all" → 拆股复权，**不含**分红
    Yahoo  adjclose          → 分红复权

一份文件里同时住着两套基准，算出来的加权收益是拿两套尺子做比值。
实测（2026-10-23，10,695 只行情 pkl）：

    5,366 只文件同时含 00:00 和 04:00 的 bar
    1,489 只的同日收盘价差 >2%（最大的差 10,415% —— 反向拆股级别）

而 RS 排名池正是拿这些文件的 close 算的百分位。差 2% 时排名错位还算
「看不出来」，差 100 倍时那只票的 RS 完全是另一个数。

修法：合并时按**交易日**去重 —— 文件里已有的那一天，以文件自己的 bar 为准
（保住它原本的价格基准），增量的那根让位。
"""
import pandas as pd
import pytest

from data import fetcher


def _bars(closes, start="2026-10-20", hour=4):
    """
    造一段日线 bar。

    hour=4 → Alpaca 的形态（美东当日零点 = 04:00 UTC）
    hour=0 → Yahoo 的形态（00:00 UTC）
    """
    idx = pd.date_range(start=start, periods=len(closes), freq="D", tz="UTC") + pd.Timedelta(
        hours=hour
    )
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=idx,
    )


def test_a_delta_bar_on_a_day_the_file_already_has_is_dropped():
    """
    增量里重复了文件已有的那一天时，增量的那根必须让位。

    文件是 Alpaca 的历史（04:00、拆股复权），增量来自 Yahoo（00:00、
    分红复权）。同一天两根 bar 的收盘价差 50% —— 留哪根都不能留两根。
    """
    existing = _bars([10.0, 11.0, 12.0])                 # 10-20 / 10-21 / 10-22
    delta = _bars([60.0, 13.0], start="2026-10-22", hour=0)  # 10-22 重复 + 10-23 新

    merged = fetcher._merge_delta(existing, delta)

    dates = [d.strftime("%Y-%m-%d") for d in merged.index.normalize()]
    assert dates == ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"], (
        f"合并后的交易日不对：{dates}"
    )
    assert len(merged) == 4, f"同一天留下了两根 bar：{len(merged)} 根"


def test_the_file_keeps_its_own_price_basis():
    """重复的那一天以**文件自己**的 bar 为准 —— 保住它原本的价格基准。"""
    existing = _bars([10.0, 11.0, 12.0])
    delta = _bars([60.0], start="2026-10-22", hour=0)

    merged = fetcher._merge_delta(existing, delta)

    assert float(merged["close"].iloc[-1]) == 12.0, (
        f"文件自己的价格基准被换掉了：{merged['close'].iloc[-1]}"
    )


def test_a_genuinely_new_day_is_still_added():
    """修「切早了」不能变成「切晚了」—— 新交易日必须照常并进来。"""
    existing = _bars([10.0, 11.0, 12.0])
    delta = _bars([13.0, 14.0], start="2026-10-23", hour=0)

    merged = fetcher._merge_delta(existing, delta)

    assert len(merged) == 5
    assert float(merged["close"].iloc[-1]) == 14.0


def test_the_merged_frame_never_has_two_bars_for_one_day():
    """
    合并结果的硬约束：一个交易日**最多一根** bar。

    这条不变量一旦破了，那只票的加权收益就是拿两套基准做比值 ——
    而 ±50% 跳空那道闸门看不见它（价格量级可能只差 157 倍）。
    """
    existing = _bars([10.0, 11.0, 12.0])
    delta = _bars([60.0, 13.0], start="2026-10-22", hour=0)

    merged = fetcher._merge_delta(existing, delta)

    assert not merged.index.normalize().duplicated().any(), "合并后仍有重复交易日"


def test_merging_into_an_empty_file_just_returns_the_delta():
    """没有历史时不该出岔子。"""
    delta = _bars([10.0, 11.0], hour=0)

    merged = fetcher._merge_delta(pd.DataFrame(), delta)

    assert len(merged) == 2
