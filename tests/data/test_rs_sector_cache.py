"""
板块排名缓存的**池子签名**（`rs_daily_cache.json[date][sector]`）。

为什么板块也要签名
------------------
`rs_daily_cache.json[date][sector]["rs_list"]` 是「按当时那块板块有哪些票」
算出来的排名表。2026-10-23 之前，板块成员来自 `sector_cache.json`（只有 375 条
Finnhub 行业记录），Consumer Staples 只能数出 **2** 只 —— 报告里就印
`Consumer Staples 排名2/2`。

换成 `rs_universe.json` 之后成员变成 142 只，**但旧缓存条目还在原地**：
报告照样打 `排名2/2`。改了代码看不到效果，而且不报任何错 ——
这正是 `__UNIVERSE__` 条目加 `pool_sig` 要防的同一类静默空跑，
只是这次漏在了板块条目上。

签名只覆盖**成员名单**，不覆盖价格：某个历史日期的板块排名在价格定稿后
就是稳定的，按成员名单失效就够了，也不会天天把缓存全部作废。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import data.rs as rs
from data.rs import (
    _get_sector_rank,
    _populate_sector_cache,
    _sector_pool_signature,
)

AS_OF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _frame(n=300, start=10.0, end=20.0, end_date="2026-10-21"):
    idx = pd.date_range(end=end_date, periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": np.linspace(start, end, n),
            "high": np.linspace(start, end, n) * 1.01,
            "low": np.linspace(start, end, n) * 0.99,
            "close": np.linspace(start, end, n),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _write_universe(path: Path, sectors: dict[str, str]) -> Path:
    """造一份 rs_universe.json：{symbol: gics_sector}。"""
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
    return path


@pytest.fixture
def universe(tmp_path, monkeypatch):
    """一个受控的板块名单：Health Care 3 只、Financials 2 只，外加 SPY。"""
    sectors = {
        "HC1": "Health Care",
        "HC2": "Health Care",
        "HC3": "Health Care",
        "FN1": "Financials",
        "FN2": "Financials",
        "SPY": "Health Care",
    }
    path = _write_universe(tmp_path / "rs_universe.json", sectors)
    monkeypatch.setattr(rs, "DEFAULT_RS_UNIVERSE", path)
    # 名单是模块级缓存的，换了文件必须失效，否则读到别的测试的名单
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_CACHE", None)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_PATH", None)
    return path


@pytest.fixture
def market_data():
    md = {sym: _frame() for sym in ("HC1", "HC2", "HC3", "FN1", "FN2", "SPY")}
    return md


# ════════════════════════════════════════════════════════════════
# 签名
# ════════════════════════════════════════════════════════════════


def test_sector_pool_signature_is_stable_for_the_same_membership(universe, tmp_path):
    a = _sector_pool_signature("Health Care", tmp_path / "sc.json")
    b = _sector_pool_signature("Health Care", tmp_path / "sc.json")
    assert a == b


def test_sector_pool_signature_changes_when_membership_changes(universe, tmp_path):
    """名单变了（加了票）签名必须跟着变 —— 否则改了池子也命中不了旧缓存。"""
    before = _sector_pool_signature("Health Care", tmp_path / "sc.json")

    _write_universe(
        universe,
        {
            "HC1": "Health Care",
            "HC2": "Health Care",
            "HC3": "Health Care",
            "HC4": "Health Care",  # 新增
            "FN1": "Financials",
            "FN2": "Financials",
            "SPY": "Health Care",
        },
    )
    rs._UNIVERSE_SECTOR_CACHE = None
    rs._UNIVERSE_SECTOR_PATH = None

    assert _sector_pool_signature("Health Care", tmp_path / "sc.json") != before


def test_sector_pool_signature_differs_per_sector(universe, tmp_path):
    assert _sector_pool_signature(
        "Health Care", tmp_path / "sc.json"
    ) != _sector_pool_signature("Financials", tmp_path / "sc.json")


def test_sector_pool_signature_includes_the_ranking_source_version(
    universe, tmp_path, monkeypatch
):
    """
    排名口径（数据源）必须进签名。

    2026-10-26 板块排名从「live pool 的票」改成「全市场宽表的票」，
    成员名单一个字没变 —— 签名只哈希名单的话，旧条目照样命中，
    报告里照样印着那个用窄池算出来的分母，而且不报任何错。
    """
    before = _sector_pool_signature("Health Care", tmp_path / "sc.json")
    monkeypatch.setattr(rs, "_SECTOR_RANKING_VERSION", "another-source")
    after = _sector_pool_signature("Health Care", tmp_path / "sc.json")
    assert before != after, "换了排名口径，签名必须跟着变"


# ════════════════════════════════════════════════════════════════
# 缓存读写
# ════════════════════════════════════════════════════════════════


def test_populated_sector_cache_records_the_pool_signature(universe, tmp_path, market_data):
    """落盘的条目必须带 pool_sig，否则下次永远判定为失效、每次都重算。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    assert _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=cache_path,
    ) is True

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = saved["2026-10-22"]["Health Care"]
    assert entry["pool_sig"] == _sector_pool_signature("Health Care", tmp_path / "sc.json")


def test_matching_pool_signature_reuses_the_cached_list(universe, tmp_path, market_data):
    """签名对得上就直接用缓存，不重算。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=cache_path,
    )
    # 把缓存内容改成一个一眼可辨的值：命中它就说明没有重算
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    saved["2026-10-22"]["Health Care"]["rs_list"] = [{"ticker": "ZZZ", "rs_spy": 99.0}]
    cache_path.write_text(json.dumps(saved), encoding="utf-8")

    rank, total = _get_sector_rank(
        ticker="HC1",
        sector="Health Care",
        rs_spy=99.0,
        as_of=AS_OF,
        market_data=market_data,
        cache_path=cache_path,
        sector_cache_path=tmp_path / "sc.json",
    )
    assert (rank, total) == (1, 1), "签名对得上就该直接用缓存里的名单"


def test_entry_without_pool_signature_is_recomputed(universe, tmp_path, market_data):
    """
    没有 pool_sig 的旧条目必须作废重算。

    这是最阴险的一类 bug：板块成员换了、日期没变，旧缓存照样命中，
    报告里「Consumer Staples 排名2/2」照旧印着 —— 改了代码却毫无效果。
    """
    cache_path = tmp_path / "rs_daily_cache.json"
    _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=cache_path,
    )
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    del saved["2026-10-22"]["Health Care"]["pool_sig"]
    saved["2026-10-22"]["Health Care"]["rs_list"] = [{"ticker": "ZZZ", "rs_spy": 99.0}]
    cache_path.write_text(json.dumps(saved), encoding="utf-8")

    rank, total = _get_sector_rank(
        ticker="HC1",
        sector="Health Care",
        rs_spy=99.0,
        as_of=AS_OF,
        market_data=market_data,
        cache_path=cache_path,
        sector_cache_path=tmp_path / "sc.json",
    )
    assert total == 4, "没有签名的旧条目必须重算，不能拿旧的窄名单当分母"
    assert rank is not None


def test_entry_with_stale_pool_signature_is_recomputed(universe, tmp_path, market_data):
    """签名对不上（旧的池子留下的）同样作废。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    _populate_sector_cache(
        sector="Health Care",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sc.json",
        rs_daily_cache_path=cache_path,
    )
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    saved["2026-10-22"]["Health Care"]["pool_sig"] = "stale-from-old-pool"
    saved["2026-10-22"]["Health Care"]["rs_list"] = [{"ticker": "ZZZ", "rs_spy": 99.0}]
    cache_path.write_text(json.dumps(saved), encoding="utf-8")

    rank, total = _get_sector_rank(
        ticker="HC1",
        sector="Health Care",
        rs_spy=99.0,
        as_of=AS_OF,
        market_data=market_data,
        cache_path=cache_path,
        sector_cache_path=tmp_path / "sc.json",
    )
    assert total == 4, "签名不匹配就必须重算"
