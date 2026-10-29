"""Tests for 板块查找优先级：rs_universe.json > sector_cache.json > Unknown。

这一步修的是报告里 `Consumer Staples 排名2/2` 的根因 ——
不是板块真的小，是 sector_cache.json 只有 375 条，绝大多数票落 Unknown
直接退出板块排名。用 Nasdaq 的全市场 sector 覆盖之后，
Consumer Staples 的分母应该从 2 变成 100+。
"""
import json

import pytest

from data.rs import _get_gics_sector, _load_universe_sectors


@pytest.fixture
def universe_json(tmp_path):
    """造一份 rs_universe.json（只保留测试用到的几个 symbol）。"""
    payload = {
        "generated_at": "2026-10-23T00:00:00+00:00",
        "symbols": {
            "AAPL": {"gics_sector": "Information Technology", "is_etf": False},
            "KO": {"gics_sector": "Consumer Staples", "is_etf": False},
            "SPY": {"gics_sector": "Unknown", "is_etf": True},
        },
    }
    path = tmp_path / "rs_universe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_universe_sector_wins_over_sector_cache(universe_json):
    """
    名单里的 GICS 板块优先于 sector_cache.json 的 Finnhub 行业串。

    Finnhub 的 industry 是 Level 2/3 名称（"Technology" / "Semiconductors"），
    要再过一次 industry_to_gics() 才能变成板块名；Nasdaq 直接给的就是 L1，
    少一层转换就少一处「静默变 Unmapped」的机会。
    """
    sector_cache = {"AAPL": {"industry": "Semiconductors"}}
    assert _get_gics_sector("AAPL", sector_cache, universe_json) == (
        "Information Technology"
    )


def test_falls_back_to_sector_cache_when_universe_lacks_ticker(universe_json):
    """名单没覆盖到的票（新股/改名）仍走老的 sector_cache 路径。"""
    sector_cache = {"NVDA": {"industry": "Semiconductors"}}
    assert _get_gics_sector("NVDA", sector_cache, universe_json) == (
        "Information Technology"
    )


def test_unknown_when_neither_source_has_the_ticker(universe_json):
    assert _get_gics_sector("ZZZZ", {}, universe_json) == "Unknown"


def test_missing_universe_file_falls_back_gracefully(tmp_path):
    """名单文件不存在时不能抛异常 —— 退回老的 sector_cache 行为。"""
    sector_cache = {"NVDA": {"industry": "Semiconductors"}}
    assert _get_gics_sector("NVDA", sector_cache, tmp_path / "nope.json") == (
        "Information Technology"
    )


def test_unmapped_finnhub_industry_does_not_become_a_sector_name(universe_json):
    """
    industry_to_gics 返回 "Unmapped" 时必须落 Unknown。

    报告里出现 "Unmapped 排名3/50" 是错数据，不是"某个叫 Unmapped 的板块"。
    """
    sector_cache = {"XYZ": {"industry": "Some Brand New Industry"}}
    assert _get_gics_sector("XYZ", sector_cache, universe_json) == "Unknown"


def test_finnhub_fallback_is_off_by_default(monkeypatch):
    """
    未命中时不能去打 Finnhub。

    实测未命中一次阻塞 1.05s，而 RS 路径上会有成千上万次未命中 ——
    一轮全市场回算能把整个 run 拖成小时级。名单已经覆盖全市场，
    Finnhub 只是兜底，默认关掉。
    """
    import data.rs as rs

    def _boom(ticker):
        raise AssertionError(f"不该调用 Finnhub：{ticker}")

    monkeypatch.setattr(rs, "_fetch_finnhub_profile", _boom)
    assert rs._enable_finnhub_fallback() is False


def test_finnhub_fallback_can_be_enabled_explicitly(monkeypatch):
    import data.rs as rs

    monkeypatch.setattr(rs, "_ENABLE_FINNHUB_FALLBACK", True)
    assert rs._enable_finnhub_fallback() is True


def test_universe_sector_loader_returns_empty_for_missing_file(tmp_path):
    assert _load_universe_sectors(tmp_path / "nope.json") == {}


def test_universe_sector_loader_skips_unknown_and_etfs(tmp_path):
    """
    ETF 与 Unknown 不能进板块表。

    ETF 没有 GICS 行业，混进去只会让板块分母变得不可比；
    Unknown 进板块表则会让「Unknown 板块」凭空拥有一堆成员。
    """
    payload = {
        "symbols": {
            "AAPL": {"gics_sector": "Information Technology", "is_etf": False},
            "SPY": {"gics_sector": "Financials", "is_etf": True},
            "ZZZZ": {"gics_sector": "Unknown", "is_etf": False},
        }
    }
    path = tmp_path / "rs_universe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _load_universe_sectors(path) == {"AAPL": "Information Technology"}


def test_universe_sector_loader_survives_corrupt_file(tmp_path):
    """损坏的 JSON 不能让整个 RS 计算崩掉 —— 返回空，退回老路径。"""
    path = tmp_path / "rs_universe.json"
    path.write_text("{not json", encoding="utf-8")
    assert _load_universe_sectors(path) == {}


# ════════════════════════════════════════════════════════════════
# 板块排名的分母
# ════════════════════════════════════════════════════════════════


def test_sector_rank_denominator_uses_full_market_list(tmp_path, monkeypatch):
    """
    板块排名的分母必须来自全市场名单，不能只数 sector_cache.json 里那 375 条。

    旧实现遍历 sector_cache.json，于是 Consumer Staples 只有 2 只票能算出分母，
    报告里直接打印 `Consumer Staples 排名2/2` —— 分母小到毫无意义，
    而且看起来像「这个板块就这么小」。
    """
    import data.rs as rs

    universe_path = tmp_path / "rs_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "symbols": {
                    **{f"CP{i}": {"gics_sector": "Consumer Staples"} for i in range(40)},
                    "TECH": {"gics_sector": "Information Technology"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_CACHE", None)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_PATH", None)

    market_data = _market_data([f"CP{i}" for i in range(40)] + ["TECH"])
    daily_cache = tmp_path / "rs_daily_cache.json"

    assert rs._populate_sector_cache(
        sector="Consumer Staples",
        as_of=AS_OF,
        market_data=market_data,
        sector_cache_path=tmp_path / "sector_cache.json",
        rs_daily_cache_path=daily_cache,
        universe_path=universe_path,
    )

    saved = json.loads(daily_cache.read_text(encoding="utf-8"))
    rs_list = saved["2026-10-22"]["Consumer Staples"]["rs_list"]
    assert len(rs_list) == 40, f"分母应覆盖名单里全部 40 只，实际 {len(rs_list)}"
    assert "TECH" not in {x["ticker"] for x in rs_list}


def test_sector_rank_denominator_excludes_etfs(tmp_path, monkeypatch):
    """ETF 没有 GICS 行业，绝不能进板块分母。"""
    import data.rs as rs

    universe_path = tmp_path / "rs_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "symbols": {
                    "CP1": {"gics_sector": "Consumer Staples", "is_etf": False},
                    "CP2": {"gics_sector": "Consumer Staples", "is_etf": False},
                    "CPETF": {"gics_sector": "Consumer Staples", "is_etf": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_CACHE", None)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_PATH", None)

    daily_cache = tmp_path / "rs_daily_cache.json"
    assert rs._populate_sector_cache(
        sector="Consumer Staples",
        as_of=AS_OF,
        market_data=_market_data(["CP1", "CP2", "CPETF"]),
        sector_cache_path=tmp_path / "sector_cache.json",
        rs_daily_cache_path=daily_cache,
        universe_path=universe_path,
    )
    saved = json.loads(daily_cache.read_text(encoding="utf-8"))
    rs_list = saved["2026-10-22"]["Consumer Staples"]["rs_list"]
    assert [x["ticker"] for x in rs_list] == ["CP1", "CP2"]


def test_sector_rank_still_works_without_universe_file(tmp_path, monkeypatch):
    """名单缺失时退回 sector_cache.json 的老行为，不能整个板块排名消失。"""
    import data.rs as rs

    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_CACHE", None)
    monkeypatch.setattr(rs, "_UNIVERSE_SECTOR_PATH", None)

    sector_cache = tmp_path / "sector_cache.json"
    sector_cache.write_text(
        json.dumps(
            {
                "CP1": {"industry": "Beverages"},
                "CP2": {"industry": "Beverages"},
                "TECH": {"industry": "Semiconductors"},
            }
        ),
        encoding="utf-8",
    )
    daily_cache = tmp_path / "rs_daily_cache.json"
    assert rs._populate_sector_cache(
        sector="Consumer Staples",
        as_of=AS_OF,
        market_data=_market_data(["CP1", "CP2", "TECH"]),
        sector_cache_path=sector_cache,
        rs_daily_cache_path=daily_cache,
        universe_path=tmp_path / "nope.json",
    )
    saved = json.loads(daily_cache.read_text(encoding="utf-8"))
    rs_list = saved["2026-10-22"]["Consumer Staples"]["rs_list"]
    assert [x["ticker"] for x in rs_list] == ["CP1", "CP2"]


AS_OF = __import__("datetime").datetime(
    2026, 10, 22, tzinfo=__import__("datetime").timezone.utc
)


def _market_data(symbols):
    """
    造 400 天线性上涨的行情，让每只票都能算出加权收益。

    SPY 必须一起给：板块排名的 RS/SPY 拿 SPY 的加权收益做分母，
    没有它整个板块的 rs_list 会是空的（真实现里 market_data 一定有 SPY）。
    """
    import numpy as np
    import pandas as pd

    n = 400
    idx = pd.date_range(end="2026-10-22", periods=n, freq="D", tz="UTC")
    names = list(symbols) + (["SPY"] if "SPY" not in symbols else [])
    return {
        sym: pd.DataFrame(
            {"close": np.linspace(100.0 + i, 200.0 + i, n)}, index=idx
        )
        for i, sym in enumerate(names)
    }