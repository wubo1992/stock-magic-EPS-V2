"""
「计量日」必须处处一致：排名池、ticker、板块排名都得按同一天算。

为什么这是硬要求
----------------
RS Rating 是**横截面百分位**：把 ticker 的加权收益放到全市场的分布里比。
排名池的分布来自宽表，ticker 的加权收益来自策略手里的 market_data。
两边各用各的「最后一根 bar」，就是跨日比较：

    2026-10-26 实测：宽表末行只覆盖 5,155 只（live pool 被刷新），
    参考日回退到 10-22；策略手里的 ticker 数据却是 10-23 的。
    拿 10-23 的分数去比 10-22 的分布 = 拿「今天涨了」去比「昨天涨的」。

实测偏差有多大：把同一批票按「统一 10-22」和「各按自己的最后一根」
两种口径算，19.1% 的票 RS Rating 差 >1 分，p90 差 3 分，最大差 98 分。
这不是可以忽略的噪声。

所以本文件锁死三条：
  1. 参考日 = 宽表里最后一个「全市场都有数据」的交易日；
  2. ticker 的加权收益必须按参考日算；
  3. 排名池的分布也必须按参考日算。
"""
import numpy as np
import pandas as pd
import pytest

import data.rs as rs

REF_DAY = pd.Timestamp("2026-10-22", tz="UTC")
AFTER = pd.Timestamp("2026-10-23", tz="UTC")


def _flat(n=400, start=100.0):
    """价格恒定 -> 加权收益恒为 0。"""
    return [start] * n


def _drift(n=400, rate=0.001, start=100.0):
    """缓慢爬升 -> 加权收益非零（SPY 必须非零，否则 RS/SPY 无定义）。"""
    return [start * (1 + rate) ** i for i in range(n)]


def _wide(n_symbols=20, n_rows=400):
    """全市场平价宽表：末行之后再补一个「只刷新了一小撮」的稀疏行。"""
    idx = pd.date_range(end=REF_DAY, periods=n_rows, freq="D", tz="UTC")
    wide = pd.DataFrame(
        {f"S{i}": [100.0 + i] * n_rows for i in range(n_symbols)}, index=idx
    )
    # 参考日之后的那一行只有 5 只票有数据（= 只刷新了 live pool 那一小撮）
    wide.loc[AFTER] = np.nan
    for i in range(5):
        wide.loc[AFTER, f"S{i}"] = 100.0 + i
    return wide


def _market_df(closes, end):
    idx = pd.date_range(end=end, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _no_universe_whitelist(monkeypatch):
    """
    本文件测的是「计量日」，用的全是假代码（S0…S19）。

    名单白名单一开，假代码全被剔掉 → 池子空了 → 测的就不是原来那件事。
    名单过滤本身由 tests/data/test_rs_pool_whitelist.py 单独负责。
    """
    monkeypatch.setattr(rs, "_load_pool_whitelist", lambda *a, **k: None)


@pytest.fixture
def wide_store(tmp_path, monkeypatch):
    """把宽表指到一个受控的临时文件，并让进程内的参考日缓存失效。"""
    path = tmp_path / "closes.pkl"
    _wide().to_pickle(path)
    monkeypatch.setattr(rs, "DEFAULT_RS_CLOSES_PATH", path)
    rs._REFERENCE_DAY_CACHE.clear()
    yield path
    rs._REFERENCE_DAY_CACHE.clear()


def test_reference_day_skips_a_partially_refreshed_row(wide_store):
    """末行只覆盖一小撮时，参考日必须回退到全市场都有数据的那天。"""
    assert rs.universe_reference_day() == REF_DAY


def test_ticker_is_measured_at_the_reference_day(wide_store, tmp_path):
    """
    ticker 的加权收益必须按参考日算，不能用参考日之后的行情。

    参考日之后那根 bar 给一个 +900% 的暴涨：按参考日算它一文不值，
    按自己的最后一根 bar 算它就是全市场最强。
    """
    market_data = {
        "TICKER": _market_df(_flat(400) + [1000.0], AFTER),  # 最后一天暴涨
        "SPY": _market_df(_drift(401), AFTER),
    }

    result = rs.compute_rs(
        ticker="TICKER",
        as_of_date=AFTER.to_pydatetime(),
        market_data=market_data,
        sector_cache_path=tmp_path / "sector.json",
        rs_daily_cache_path=tmp_path / "rs_daily.json",
    )

    assert result is not None
    assert result.rs_rating is not None
    # 全市场都是平价（wret=0），ticker 按参考日算也是 wret=0 -> 中位水平
    assert result.rs_rating < 90, (
        f"参考日之后的暴涨被算进去了（RS Rating={result.rs_rating}）"
    )


def test_measurement_day_does_not_depend_on_how_fresh_the_ticker_is(
    wide_store, tmp_path
):
    """
    同一个 ticker，参考日之后有没有新 bar，测出来的结果必须一模一样。

    这是「统一计量日」的直接表述：计量日由**市场**决定，不由 ticker 决定。
    """
    market_data = {
        "TICKER": _market_df(_flat(400) + [1000.0], AFTER),
        "SPY": _market_df(_drift(401), AFTER),
    }
    kwargs = dict(
        ticker="TICKER",
        market_data=market_data,
        sector_cache_path=tmp_path / "sector.json",
        rs_daily_cache_path=tmp_path / "rs_daily.json",
    )

    at_after = rs.compute_rs(as_of_date=AFTER.to_pydatetime(), **kwargs)
    at_ref = rs.compute_rs(as_of_date=REF_DAY.to_pydatetime(), **kwargs)

    assert at_after is not None and at_ref is not None
    assert at_after.rs_rating == at_ref.rs_rating, (
        f"参考日之后的数据改变了评级（{at_after.rs_rating} vs {at_ref.rs_rating}）"
    )


def test_rating_pool_uses_the_reference_day(wide_store, tmp_path):
    """
    排名池的分布也必须按参考日算，不能把参考日之后的行算进去。

    参考日之后那 5 只票暴涨到 1000，如果被算进分布，池子的分位就全被带偏。
    """
    wide = _wide()
    for i in range(5):
        wide.loc[AFTER, f"S{i}"] = 1000.0  # 参考日之后暴涨
    wide.to_pickle(wide_store)
    rs._REFERENCE_DAY_CACHE.clear()

    wrets, source = rs._universe_wrets_from_wide(AFTER.to_pydatetime())

    assert source == "wide"
    assert len(wrets) == 20, "参考日之后没数据的票不该被踢出池子"
    assert max(wrets) < 0.01, "参考日之后的暴涨进了排名池的分布"
