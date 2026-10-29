"""
排名池的成员必须是「名单里的普通股 + ETF」，不能是宽表里有什么就算什么。

背景（2026-10-28 实测）
------------------------
宽表 `data/rs_universe_closes.pkl` 是从 `data/cache/*.pkl` 汇总的，
**它只反映本地缓存里有什么**，不反映「什么才是股票」：

    旧池 9,591 只  →  白名单后 9,080 只，剔掉 511 只
      · 256 只不在全市场名单里
          21 只权证      RVMDW 排第 3（wret +512%）、DRTSW 第 11
          136 只优先股 / 票据 / 混合票据
          25 只杠杆 / 反向 ETN（MicroSectors 系列，名字里没有 ETF 字样）
          4 只 SPAC 单位（Belpointe PREP LLC Class A Units 等）
          55 只境外 OTC / 已退市（AHCHY / BBBY / FNMA …）
      · 255 只杠杆 / 反向 ETF（名单里有，但涨跌是结构性的）
      · 7 只基准 ETF（SPY/QQQ/DIA/IWM/VOO/EWT/ASHR，另一道闸门）
      · 2 只点号 / 横线写法不一致（BRK-B ↔ BRK.B、BF-B ↔ BF.B）

权证不是股票。混进横截面分母，RS Rating 的 1-99 刻度整体偏掉，
而且日志里什么都看不出来 —— 这正是本项目最常见的那类静默失真。

名单 `data/rs_universe.json` 才是「什么才是股票」的判据：
普通股 + ETF，权证 / 优先股 / 票据 / ETN 全部剔掉。
"""
import json

import pandas as pd

from data import rs as rsmod
from data.rs import _current_pool_signature, _is_pool_excluded


# ════════════════════════════════════════════════════════════════
# 白名单本身
# ════════════════════════════════════════════════════════════════


def _write_universe(tmp_path, symbols: dict) -> str:
    path = tmp_path / "rs_universe.json"
    path.write_text(
        json.dumps({"generated_at": "2026-10-28T00:00:00+00:00", "symbols": symbols}),
        encoding="utf-8",
    )
    return str(path)


def test_whitelist_keeps_common_stocks_and_plain_etfs(tmp_path):
    path = _write_universe(
        tmp_path,
        {
            "AAPL": {"security_type": "common_stock", "is_etf": False, "is_leveraged": False},
            "SCHD": {"security_type": "etf", "is_etf": True, "is_leveraged": False},
        },
    )
    wl = rsmod._load_pool_whitelist(path)
    assert wl == {"AAPL", "SCHD"}


def test_whitelist_drops_leveraged_etfs(tmp_path):
    """杠杆/反向 ETF 不是普通股，涨跌是结构性的，不能进横截面分母。"""
    path = _write_universe(
        tmp_path,
        {
            "TQQQ": {"security_type": "etf", "is_etf": True, "is_leveraged": True},
            "SPY": {"security_type": "etf", "is_etf": True, "is_leveraged": False},
        },
    )
    wl = rsmod._load_pool_whitelist(path)
    assert "TQQQ" not in wl
    assert "SPY" in wl


def test_whitelist_is_none_when_the_list_file_is_missing(tmp_path):
    """名单缺失时返回 None —— 不能因为名单没了就把整个池子清空。"""
    assert rsmod._load_pool_whitelist(str(tmp_path / "nope.json")) is None


def test_whitelist_is_none_when_the_list_file_is_broken(tmp_path):
    path = tmp_path / "rs_universe.json"
    path.write_text("{ not json", encoding="utf-8")
    assert rsmod._load_pool_whitelist(str(path)) is None


def test_whitelist_drops_non_equity_types(tmp_path):
    """名单里若混进 preferred / warrant / unit，本身就该剔掉。"""
    path = _write_universe(
        tmp_path,
        {
            "AAPL": {"security_type": "common_stock", "is_etf": False, "is_leveraged": False},
            "PFD": {"security_type": "preferred", "is_etf": False, "is_leveraged": False},
            "RVMDW": {"security_type": "warrant", "is_etf": False, "is_leveraged": False},
            "OZ": {"security_type": "unit", "is_etf": False, "is_leveraged": False},
        },
    )
    wl = rsmod._load_pool_whitelist(path)
    assert wl == {"AAPL"}


# ════════════════════════════════════════════════════════════════
# 池子过滤
# ════════════════════════════════════════════════════════════════


def _wide(tmp_path, columns: dict) -> pd.DataFrame:
    """造一张小宽表：每只票 300 根连续上涨的 bar（够算 252 日加权收益）。"""
    idx = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    return pd.DataFrame(
        {c: [10.0 + i * 0.01 for i in range(300)] for c in columns}, index=idx
    )


def test_warrant_is_dropped_from_the_pool(tmp_path, monkeypatch):
    """
    权证（RVMDW 那种）不进排名池。

    它是**可算**的（价格连续、有 253 根 bar），所以任何「算不出来就排除」
    的规则都拦不住它 —— 必须按名单显式剔除。
    """
    closes_path = tmp_path / "closes.pkl"
    _wide(tmp_path, {"GOOD": None, "RVMDW": None}).to_pickle(closes_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", closes_path)
    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: {"GOOD"})

    pool = rsmod._pool_wrets_by_symbol()

    assert "RVMDW" not in pool.index
    assert "GOOD" in pool.index


def test_pool_filters_by_whitelist_and_keeps_dot_dash_equivalents(tmp_path, monkeypatch):
    """
    池子 = 宽表 ∩ 名单；且 BRK-B（Alpaca 写法）与 BRK.B（名单写法）视为同一只。
    """
    closes_path = tmp_path / "closes.pkl"
    _wide(tmp_path, {"BRK-B": None, "RVMDW": None, "SPY": None}).to_pickle(closes_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", closes_path)
    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: {"BRK.B", "SPY"})

    values, source = rsmod._universe_wrets_from_wide()

    assert source == "wide"
    # BRK-B 留下（点/横线等价），RVMDW 被名单剔掉，SPY 被基准名单剔掉
    assert len(values) == 1
    assert _is_pool_excluded("SPY")


def test_pool_without_a_whitelist_keeps_everything(tmp_path, monkeypatch):
    """名单不可用时不能把池子清空 —— 退回「宽表里有什么算什么」。"""
    closes_path = tmp_path / "closes.pkl"
    _wide(tmp_path, {"GOOD": None, "RVMDW": None}).to_pickle(closes_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", closes_path)
    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: None)

    pool = rsmod._pool_wrets_by_symbol()

    assert set(pool.index) == {"GOOD", "RVMDW"}


# ════════════════════════════════════════════════════════════════
# 签名必须覆盖白名单
# ════════════════════════════════════════════════════════════════


def test_pool_signature_changes_when_the_whitelist_changes(tmp_path, monkeypatch):
    """
    改了名单 → 池子成分变了 → 签名必须跟着变。

    不签的话旧缓存照样命中，报告里的 RS Rating 还是按旧池子算的，
    而且什么异常都看不出来。
    """
    closes_path = tmp_path / "closes.pkl"
    _wide(tmp_path, {"AAPL": None}).to_pickle(closes_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", closes_path)

    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: {"AAPL"})
    before = _current_pool_signature()

    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: {"AAPL", "MSFT"})
    after = _current_pool_signature()

    assert before != after, "改了白名单，签名却没变"


def test_pool_signature_survives_a_missing_whitelist(tmp_path, monkeypatch):
    """名单缺失时签名照样能算（不能因为白名单没了就崩）。"""
    closes_path = tmp_path / "closes.pkl"
    _wide(tmp_path, {"AAPL": None}).to_pickle(closes_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", closes_path)
    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: None)
    assert _current_pool_signature()
