"""Tests for O'Neil RS Rating (全市场百分位 1-99) 与 RS 列解析。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data.rs import (
    UNIVERSE_CACHE_KEY,
    RSResult,
    _current_pool_signature,
    _get_rs_rating,
    _get_universe_wrets,
    _populate_universe_cache,
    parse_rs_columns_from_reason,
)

AS_OF = datetime(2026, 10, 22, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_universe_whitelist(monkeypatch):
    """
    本文件测的是「池子签名 / 缓存失效」，用的全是假代码（S0…S7、AAA）。

    名单白名单一开，假代码全被剔掉 → 池子空了 → 测的就不是原来那件事。
    名单过滤本身由 tests/data/test_rs_pool_whitelist.py 单独负责。
    """
    monkeypatch.setattr("data.rs._load_pool_whitelist", lambda *a, **k: None)


@pytest.fixture
def universe_cache(tmp_path):
    """
    写入 100 只股票、wret = 0.01..1.00 的全市场缓存。

    必须带上**当前** pool_sig：缓存只在「池子组成没变」时才可信，
    不带签名的旧条目会被当成失效缓存丢弃，然后拿真实宽表重算 ——
    测试就会莫名拿到 4,000 多只票的池子结果。详见 test_pool_signature_*。
    """
    path = tmp_path / "rs_daily_cache.json"
    wrets = [round(0.01 * i, 6) for i in range(1, 101)]
    path.write_text(
        json.dumps(
            {
                "2026-10-22": {
                    UNIVERSE_CACHE_KEY: {
                        "wrets": wrets,
                        "count": 100,
                        "pool_sig": _current_pool_signature(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_rs_rating_strongest_gets_99(universe_cache):
    assert _get_rs_rating(1.00, AS_OF, {}, universe_cache) == 99


def test_rs_rating_weakest_gets_1(universe_cache):
    assert _get_rs_rating(0.01, AS_OF, {}, universe_cache) == 1


def test_rs_rating_clamped_outside_universe(universe_cache):
    """比全市场最强还强 / 比最弱还弱，都要夹在 1-99。"""
    assert _get_rs_rating(99.0, AS_OF, {}, universe_cache) == 99
    assert _get_rs_rating(-99.0, AS_OF, {}, universe_cache) == 1


def test_rs_rating_is_monotonic(universe_cache):
    """加权收益越高，RS Rating 不能变小。"""
    ratings = [
        _get_rs_rating(round(0.01 * i, 6), AS_OF, {}, universe_cache)
        for i in range(1, 101)
    ]
    assert ratings == sorted(ratings)


def _write_wide_table(path, columns):
    """造一张受控宽表：columns = {symbol: (起点, 终点)} 的等差上升序列。"""
    n = 300
    idx = pd.date_range(end="2026-10-21", periods=n, freq="D", tz="UTC")
    pd.DataFrame(
        {k: np.linspace(lo, hi, n) for k, (lo, hi) in columns.items()}, index=idx
    ).to_pickle(path)
    return path


@pytest.fixture
def narrow_wide_table(tmp_path, monkeypatch):
    """
    一张**只有 8 只票**的宽表，且 8 只的加权收益全部高于被测的 wret=0.50。

    它的作用是让「缓存命中」和「缓存失效后重算」给出截然不同的答案：
    预置缓存的 100 只池子是 0.01..1.00，wret=0.50 落在中位 -> 评级 50；
    这张表的 8 只票 wret 全在 0.6 以上，0.50 一只都比不过 -> 评级 12。
    于是断言具体数字就能区分两条路径，不用靠「断言不成立」这种反直觉写法。

    ⚠️ 别把池子缩到 2 只：(below+1)/n 在 n=2 时给出 1/2*99 = 49.5，
    四舍五入又是 50 —— 跟预置缓存撞成同一个数，两条路径就分不开了。
    """
    path = _write_wide_table(
        tmp_path / "closes.pkl",
        {f"S{i}": (100.0, 400.0 + 100.0 * i) for i in range(8)},
    )
    monkeypatch.setattr("data.rs.DEFAULT_RS_CLOSES_PATH", path)
    return path


def test_pool_signature_valid_cache_is_used(narrow_wide_table, universe_cache):
    """签名对得上：直接用缓存里的池子，wret=0.50 -> 50。"""
    assert _get_rs_rating(0.50, AS_OF, {}, universe_cache) == 50


def test_pool_signature_missing_invalidates_stale_cache(narrow_wide_table, universe_cache):
    """
    没有 pool_sig 的旧缓存条目必须作废重算。

    这是最阴险的一类 bug：股票池变了、日期没变，旧缓存照样命中，
    项目「成功跑完、什么错都没报」，但 RS Rating 全是按旧池子算的。
    """
    entry = json.loads(universe_cache.read_text(encoding="utf-8"))
    del entry["2026-10-22"][UNIVERSE_CACHE_KEY]["pool_sig"]
    universe_cache.write_text(json.dumps(entry), encoding="utf-8")

    # 池子换成窄表后重算 -> 评级 12，而不是沿用缓存的 50
    assert _get_rs_rating(0.50, AS_OF, {}, universe_cache) == 12


def test_pool_signature_change_invalidates_cache(narrow_wide_table, universe_cache):
    """签名对不上（旧的池子留下的）时同样作废。"""
    entry = json.loads(universe_cache.read_text(encoding="utf-8"))
    entry["2026-10-22"][UNIVERSE_CACHE_KEY]["pool_sig"] = "stale-from-old-pool"
    universe_cache.write_text(json.dumps(entry), encoding="utf-8")

    assert _get_rs_rating(0.50, AS_OF, {}, universe_cache) == 12


def test_pool_signature_changes_when_wide_table_changes(tmp_path, monkeypatch):
    """宽表内容一变，签名必须跟着变 —— 否则改了池子也命中不了缓存。"""
    path = _write_wide_table(tmp_path / "c.pkl", {"AAA": (100.0, 400.0)})
    monkeypatch.setattr("data.rs.DEFAULT_RS_CLOSES_PATH", path)
    before = _current_pool_signature()

    _write_wide_table(path, {"AAA": (100.0, 400.0), "BBB": (100.0, 400.0)})
    assert _current_pool_signature() != before, "池子加了票，签名却没变"

    path2 = _write_wide_table(tmp_path / "c2.pkl", {"AAA": (100.0, 401.0)})
    monkeypatch.setattr("data.rs.DEFAULT_RS_CLOSES_PATH", path2)
    assert _current_pool_signature() != before, "现价变了，签名却没变"


def test_pool_signature_covers_the_computation_rules(monkeypatch):
    """
    改了排名池的**算法**，签名必须跟着变。

    签名原本只覆盖「池子里有哪些票、现价是多少」。但 2026-10-26 加了
    「窗口日历跨度」闸门之后，池子的**成分**变了（440 只票落选），
    而宽表一个字都没动 —— 旧缓存照样命中，RS Rating 于是还按旧规则算，
    报告里什么异常都看不出来。

    这是本项目最容易被忽略的一类静默空跑：改了算法却没改数据。
    """
    before = _current_pool_signature()
    monkeypatch.setattr("data.rs_universe.MAX_WINDOW_SPAN_DAYS", 12345)
    assert _current_pool_signature() != before, "改了排脏闸门，签名却没变"


def test_rules_change_invalidates_the_cached_pool(narrow_wide_table, universe_cache, monkeypatch):
    """
    改了闸门常量 → 旧缓存条目作废 → 重算池子。

    端到端：预置缓存给的是 100 只票的池子（wret=0.50 -> 评级 50），
    规则一变，签名对不上，重算后用窄表那 8 只票（评级 12）。
    """
    entry = json.loads(universe_cache.read_text(encoding="utf-8"))
    assert entry["2026-10-22"][UNIVERSE_CACHE_KEY]["pool_sig"] == _current_pool_signature()

    monkeypatch.setattr("data.rs_universe.MAX_WINDOW_SPAN_DAYS", 12345)
    assert _current_pool_signature() != entry["2026-10-22"][UNIVERSE_CACHE_KEY]["pool_sig"]
    assert _get_rs_rating(0.50, AS_OF, {}, universe_cache) == 12


def test_rs_rating_missing_cache_returns_none(tmp_path, monkeypatch):
    """缓存缺失且**宽表也没有**时返回 None。"""
    monkeypatch.setattr("data.rs.DEFAULT_RS_CLOSES_PATH", tmp_path / "nope_closes.pkl")
    assert _get_rs_rating(0.5, AS_OF, {}, tmp_path / "nope.json") is None


def test_populated_cache_records_pool_signature(narrow_wide_table, tmp_path):
    """落盘的条目必须带 pool_sig，否则下一次运行永远判定为失效、每次都重算。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    assert _populate_universe_cache(AS_OF, {}, cache_path) is True

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = saved["2026-10-22"][UNIVERSE_CACHE_KEY]
    assert entry["source"] == "wide"
    assert entry["pool_sig"] == _current_pool_signature()
    assert entry["count"] == 8

    # 写完之后应当能直接命中缓存，不再重算
    assert _get_universe_wrets(AS_OF, {}, cache_path) == entry["wrets"]


def test_universe_falls_back_to_market_data_without_wide_table(tmp_path, monkeypatch):
    """宽表缺失时必须回退到逐只 pkl，不能整个 RS 评级直接空掉。"""
    monkeypatch.setattr("data.rs.DEFAULT_RS_CLOSES_PATH", tmp_path / "nope.pkl")
    idx = pd.date_range(end="2026-10-21", periods=300, freq="D", tz="UTC")
    md = {"AAA": pd.DataFrame({"close": np.linspace(10, 20, 300)}, index=idx)}

    cache_path = tmp_path / "rs_daily_cache.json"
    assert _populate_universe_cache(AS_OF, md, cache_path) is True
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["2026-10-22"][UNIVERSE_CACHE_KEY]["source"] == "market_data"


def test_parse_rs_columns_from_reason():
    reason = (
        "[EMA10回踩] 上穿EMA10($1.60, +4.5%) + 量能2.1x均量 | "
        "[RS] RS/SPY=3.10x / RS-10W=1.30x / RS-10D=1.44x / RS-Rating=88 "
        "(Information Technology 排名1/45)"
    )
    cols = parse_rs_columns_from_reason(reason)
    assert cols["RS/SPY"] == "3.10"
    assert cols["RS-10W"] == "1.30"
    assert cols["RS-10D"] == "1.44"
    assert cols["RS相对强度"] == "88"
    assert cols["板块排名"] == "1/45"


def test_parse_rs_columns_ignores_non_rs_sections():
    """[EMA10回踩] 里的价格不能被误当成 RS/SPY。"""
    reason = "[EMA10回踩] 上穿EMA10($1.60, +4.5%) + 量能2.1x均量"
    assert parse_rs_columns_from_reason(reason) == {}


def test_parse_rs_columns_handles_negative_rs_spy():
    """RS/SPY 可以是负数（SPY 加权收益为负时），不能被正则漏掉。"""
    reason = (
        "[EMA10回踩] 上穿EMA10($2.25, +2.1%) + 量能3.4x均量 | "
        "[RS] RS/SPY=-0.48x / RS-10W=1.05x / RS-10D=0.93x / RS-Rating=32 "
        "(Materials 排名7/8)"
    )
    cols = parse_rs_columns_from_reason(reason)
    assert cols["RS/SPY"] == "-0.48"
    assert cols["RS-10W"] == "1.05"
    assert cols["RS相对强度"] == "32"


def test_parse_rs_columns_without_rs_section():
    reason = "[EPS] EPS+$0.25 | [Stage2] SMA150上升30天 | [量能] 5.6x均量"
    assert parse_rs_columns_from_reason(reason) == {}


def test_rs_result_to_column_dict():
    rs = RSResult(
        rs_spy=1.234,
        sector="Information Technology",
        rs_10w_spy=1.24,
        rs_10d_spy=1.10,
        sector_rank=3,
        sector_total=12,
        rs_rating=87,
    )
    cols = rs.to_column_dict()
    assert cols == {
        "RS相对强度": "87",
        "RS/SPY": "1.23",
        "RS-10W": "1.24",
        "RS-10D": "1.10",
        "板块排名": "3/12",
    }


def test_rs_result_to_column_dict_with_missing_values():
    """算不出来的项留空字符串，不能写 "None"。"""
    rs = RSResult(rs_spy=0.98, sector="Unknown")
    cols = rs.to_column_dict()
    assert cols["RS相对强度"] == ""
    assert cols["RS-10W"] == ""
    assert cols["板块排名"] == ""
    assert cols["RS/SPY"] == "0.98"


def test_format_reason_segment_keeps_rs_spy_first():
    """
    RS/SPY 必须是 [RS] 段里的第一个数字。

    signals/report.py 的 _extract_number() 靠这个位置提取 RS/SPY 倍数，
    RS-Rating 排到它后面就不会干扰。
    """
    rs = RSResult(
        rs_spy=3.92,
        sector="Health Care",
        rs_10w_spy=0.83,
        rs_10d_spy=0.71,
        sector_rank=1,
        sector_total=180,
        rs_rating=99,
    )
    segment = rs.format_reason_segment()
    assert segment.startswith("[RS] RS/SPY=3.92x")
    assert "RS-Rating=99" in segment
    assert "Health Care 排名1/180" in segment
    # 解析回来能对上
    cols = parse_rs_columns_from_reason(segment)
    assert cols["RS/SPY"] == "3.92"
    assert cols["RS相对强度"] == "99"