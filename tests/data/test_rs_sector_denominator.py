"""
板块排名的**分母**必须是全市场的板块成员，不能只是 live pool 里那几只。

为什么这是硬要求
----------------
`rs_daily_cache.json[date][sector]["rs_list"]` 是「这块板块里每只票的 RS/SPY」
按降序排的表。报告里印的 `排名7/142` 里，142 就是这张表的长度。

旧实现只从 `market_data`（= live pool，本轮真正抓过数据的那些票）里取成员：

    2026-10-26 实测：Financials 板块全市场 1,210 只，live pool 只有 811 只
    -> 分母少了三分之一，排名被系统性抬高

而且被漏掉的恰恰是**弱势股**（它们不在 live pool 里，因为信号系统不持有
它们、没给它们抓数据）—— 分母变小 = 弱票缺席 = 排名虚高。

所以板块成员必须从**宽表**取数：宽表是全市场 10,695 只票的收盘价，
按参考日算出的加权收益就是全市场横截面，跟 RS Rating 用的是同一套数。
"""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import data.rs as rs
from data.rs import _get_sector_rank, _populate_sector_cache

AS_OF = datetime(2026, 10, 22, tzinfo=timezone.utc)

# 板块 5 只票的收盘价终值（越大越强）；SPY 居中
MEMBER_END = {"HC1": 130.0, "HC2": 120.0, "HC3": 110.0, "HC4": 90.0, "HC5": 80.0}
SPY_END = 110.0


def _frame(end_price, n=300, start=100.0, end_date="2026-10-22"):
    idx = pd.date_range(end=end_date, periods=n, freq="D", tz="UTC")
    close = np.linspace(start, end_price, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


@pytest.fixture
def universe(tmp_path, monkeypatch):
    """Health Care 板块 5 只成员。SPY 不在名单里（生产环境 ETF 被名单过滤掉）。"""
    sectors = {sym: "Health Care" for sym in MEMBER_END}
    path = tmp_path / "rs_universe.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-10-23T00:00:00+00:00",
                "symbols": {
                    sym: {"is_etf": False, "is_adr": False, "gics_sector": sec}
                    for sym, sec in sectors.items()
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rs, "DEFAULT_RS_UNIVERSE", path)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_CACHE", None)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_PATH", None)
    return path


@pytest.fixture
def wide_store(tmp_path, monkeypatch):
    """全市场宽表：5 只板块成员 + SPY 都有数据。"""
    frames = {sym: _frame(end) for sym, end in MEMBER_END.items()}
    frames["SPY"] = _frame(SPY_END)
    wide = pd.DataFrame({sym: f["close"] for sym, f in frames.items()})
    path = tmp_path / "closes.pkl"
    wide.to_pickle(path)
    monkeypatch.setattr(rs, "DEFAULT_RS_CLOSES_PATH", path)
    rs._REFERENCE_DAY_CACHE.clear()
    yield path
    rs._REFERENCE_DAY_CACHE.clear()


@pytest.fixture
def market_data():
    """live pool：只抓到 2 只板块成员 + SPY（另外 3 只没进本轮股票池）。"""
    md = {"SPY": _frame(SPY_END)}
    for sym in ("HC1", "HC2"):
        md[sym] = _frame(MEMBER_END[sym])
    return md


def test_sector_denominator_covers_members_outside_the_live_pool(
    universe, wide_store, market_data, tmp_path
):
    """分母必须是全市场 5 只，不是 live pool 里的 2 只。"""
    assert _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=tmp_path / "rs_daily_cache.json",
    ) is True

    saved = json.loads((tmp_path / "rs_daily_cache.json").read_text(encoding="utf-8"))
    rs_list = saved["2026-10-22"]["Health Care"]["rs_list"]

    assert len(rs_list) == 5, f"分母只有 {len(rs_list)} 只，live pool 之外的成员被漏掉了"
    assert {row["ticker"] for row in rs_list} == set(MEMBER_END)


def test_sector_rank_is_computed_against_the_full_market(
    universe, wide_store, market_data, tmp_path
):
    """排名要跟**全部**成员比，不能只跟 live pool 里那几只比。"""
    ticker_wret = rs._compute_weighted_return(market_data["HC1"], AS_OF, ticker="HC1")
    spy_wret = rs._compute_weighted_return(market_data["SPY"], AS_OF, ticker="SPY")
    rs_spy = ticker_wret / spy_wret

    rank, total = _get_sector_rank(
        ticker="HC1",
        sector="Health Care",
        rs_spy=rs_spy,
        as_of=AS_OF,
        market_data=market_data,
        cache_path=tmp_path / "rs_daily_cache.json",
        sector_cache_path=tmp_path / "sc.json",
    )

    assert total == 5
    assert rank == 1, "HC1 是板块里最强的，必须排第 1"


def test_sector_ordering_follows_the_whole_market(
    universe, wide_store, market_data, tmp_path
):
    """板块排名顺序必须反映全市场强弱，不是 live pool 里的相对次序。"""
    assert _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=tmp_path / "rs_daily_cache.json",
    ) is True

    saved = json.loads((tmp_path / "rs_daily_cache.json").read_text(encoding="utf-8"))
    rs_list = saved["2026-10-22"]["Health Care"]["rs_list"]
    order = [row["ticker"] for row in rs_list]

    assert order == ["HC1", "HC2", "HC3", "HC4", "HC5"]
