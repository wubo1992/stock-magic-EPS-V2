"""
排名池不能包含**基准 ETF** —— SPY 在自己的排名池里是自指偏差。

为什么这是硬要求
----------------
RS Rating 是横截面百分位：`RS/SPY` = 个股加权收益 ÷ SPY 加权收益。
SPY 自己的 `RS/SPY` 恒等于 1.0，它进排名池等于**拿自己去比**：

  - SPY 占掉池子里一个位置，抬高/压低所有人的百分位；
  - 更糟的是 SPY 同时是**分母**，它出现在分布里就让分母自己参与了自己的排序。

`main.py:126-130` 还会强制注入 SPY / ASHR / EWT 做分市场基准，
所以这几个基准 ETF 必然在本地缓存里、必然在宽表里、必然会混进池子。

排除的是**宽基基准 ETF**（SPY / QQQ / DIA / IWM / VOO / ASHR / EWT）：
它们是市场的代理，不是可被选股的个股。板块/主题 ETF（XLK、XLF…）留着——
它们是可投资标的，而且没有 GICS 行业，本来就不进板块排名的分母。

注意：排除只作用于**排名池**。SPY 仍然是 RS 的**基准**，仍然必须能算出
加权收益给板块排名当分母 —— 所以不能在宽表那一层就把 SPY 抹掉。
"""
import numpy as np
import pandas as pd
import pytest

import data.rs as rs

REF_DAY = pd.Timestamp("2026-10-22", tz="UTC")
AS_OF = REF_DAY.to_pydatetime()

BENCHMARKS = ("SPY", "QQQ", "DIA", "IWM", "VOO", "ASHR", "EWT")


def _wide(symbols=("SPY", "QQQ", "AAPL", "MSFT"), n_rows=400):
    """全列都覆盖到参考日的宽表（末行不稀疏，参考日 = 最后一行）。"""
    idx = pd.date_range(end=REF_DAY, periods=n_rows, freq="D", tz="UTC")
    return pd.DataFrame(
        {sym: [100.0 + i] * n_rows for i, sym in enumerate(symbols)}, index=idx
    )


@pytest.fixture
def wide_store(tmp_path, monkeypatch):
    path = tmp_path / "closes.pkl"
    _wide().to_pickle(path)
    monkeypatch.setattr(rs, "DEFAULT_RS_CLOSES_PATH", path)
    rs._REFERENCE_DAY_CACHE.clear()
    rs._WIDE_WRETS_CACHE.clear()
    yield path
    rs._REFERENCE_DAY_CACHE.clear()
    rs._WIDE_WRETS_CACHE.clear()


def test_benchmark_etfs_are_excluded_by_name():
    """基准名单写死在这里，别悄悄改小。"""
    for sym in BENCHMARKS:
        assert sym in rs.POOL_EXCLUDED_SYMBOLS, f"{sym} 不该在排名池里"


def test_indices_are_excluded_from_the_pool():
    """^VIX / ^GSPC 这类指数代码同样不能进池。"""
    assert rs._is_pool_excluded("^VIX")
    assert rs._is_pool_excluded("^GSPC")


def test_benchmark_etfs_are_not_in_the_rating_pool(wide_store):
    """池子里只剩 AAPL / MSFT，SPY 和 QQQ 都不许在。"""
    wrets, source = rs._universe_wrets_from_wide(AS_OF)

    assert source == "wide"
    assert len(wrets) == 2, f"排名池里混进了基准 ETF（{len(wrets)} 只，应为 2 只）"


def test_benchmark_etf_is_still_usable_as_the_rs_benchmark(wide_store):
    """
    排除只作用于排名池 —— SPY 作为**基准**必须仍然算得出加权收益。

    板块排名靠它当分母（rs_spy = 个股 wret / SPY wret），
    在宽表那一层就把 SPY 抹掉会让所有板块排名整个算不出来。
    """
    wr = rs._wide_wrets_by_symbol(AS_OF)

    assert wr is not None
    assert "SPY" in wr.index
    assert not pd.isna(wr["SPY"]), "SPY 的加权收益算不出来了，板块排名会全崩"


def test_fallback_pool_also_excludes_benchmark_etfs():
    """回退路径（逐只 pkl）必须用同一份排除名单。"""
    idx = pd.date_range(end=REF_DAY, periods=400, freq="D", tz="UTC")
    frames = {}
    for i, sym in enumerate(("SPY", "AAPL")):
        close = [100.0 + i] * 400
        frames[sym] = pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": [1_000_000.0] * 400,
            },
            index=idx,
        )

    wrets = rs._universe_wrets_from_market_data(AS_OF, frames)

    assert len(wrets) == 1, "回退路径把 SPY 也放进池子了"


def test_pool_signature_covers_the_exclusion_list(wide_store, monkeypatch):
    """
    排除名单改了，池子签名必须跟着变。

    池子签名是 `__UNIVERSE__` 缓存条目的唯一校验依据。改了排除名单
    却签名不变，旧条目照样命中 —— 报告里的 RS Rating 还是按「含 SPY 的
    旧池子」算的，而且不报任何错。跟板块签名漏掉排名口径是同一类坑。
    """
    before = rs._current_pool_signature()
    monkeypatch.setattr(rs, "POOL_EXCLUDED_SYMBOLS", frozenset({"SPY", "QQQ"}))
    after = rs._current_pool_signature()

    assert before != after, "改了排除名单，池子签名却没变（旧缓存会静默命中）"
