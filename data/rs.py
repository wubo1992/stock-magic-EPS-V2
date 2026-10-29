"""
data/rs.py — IBD 风格 RS/SPY 相对强度 + O'Neil RS Rating 计算

职责：计算单只 ticker 的 RS/SPY 比 + O'Neil RS Rating（1-99）+ 同板块排名，
用于 v_eps_v2 策略的信号展示与 signals.csv 的 RS 列。

核心公式（IBD 标准 40/20/20/20 加权季度收益）：
    RS_raw(t) = 0.4 × R_63(t)  +  0.2 × R_126(t)  +  0.2 × R_189(t)  +  0.2 × R_252(t)
    RS/SPY(t) = RS_raw(t) / RS_raw(SPY, t)
    RS Rating(t) = RS_raw(t) 在全市场所有可比股票中的百分位（1-99 整数）

数据源：
- 价格：market_data[symbol]（调用方传入的 DataFrame dict）
- 板块：tools/sector_analysis.py 的 Finnhub 缓存 + GICS 映射

性能预算：
- 单只 ticker compute_rs()：< 0.05s（缓存命中）
- 首次运行 sector 缓存未命中：~1.1s/ticker（Finnhub 限流）
- rs_daily_cache.json[today] 未命中（懒加载全市场 RS Rating）：~15s/交易日
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from data import rs_universe as _rsu  # noqa: E402
from data.rs_universe import (  # noqa: E402
    DEFAULT_CLOSES_PATH as DEFAULT_RS_CLOSES_PATH,
    break_inside_window as _break_inside_window,
    load_universe_closes,
    market_reference_day,
    vectorized_weighted_return,
)

logger = logging.getLogger(__name__)

# 复用 tools/sector_analysis.py 的 sector 工具
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_ROOT))

from tools.sector_analysis import (  # noqa: E402
    industry_to_gics,
    load_cache as _load_sector_cache,
    save_cache as _save_sector_cache,
    fetch_profile as _fetch_finnhub_profile,
    INDUSTRY_TO_GICS,
)

# 项目在用的 11 个 GICS L1 板块。industry_to_gics 对认不出的行业返回
# "Unmapped"，它**不是**一个板块名 —— 拿它当 key 会造出一个不存在的板块。
_KNOWN_GICS_SECTORS: frozenset[str] = frozenset(INDUSTRY_TO_GICS.values())

# ── 路径 ─────────────────────────────────────────────────────────
DEFAULT_SECTOR_CACHE = _ROOT / "data" / "sector_cache.json"
DEFAULT_RS_DAILY_CACHE = _ROOT / "data" / "rs_daily_cache.json"
DEFAULT_RS_UNIVERSE = _ROOT / "data" / "rs_universe.json"
RS_DAILY_CACHE_MAX_DAYS = 30  # 滚动保留最近 30 天

# Finnhub 兜底默认关。
#
# 未命中一次阻塞 1.05s（串行 HTTP），而 RS 路径上会有成千上万次未命中 ——
# 一轮全市场回算能把 run 拖成小时级。rs_universe.json 已经覆盖全市场，
# Finnhub 只是在名单没覆盖到的新票上才有意义，留作显式开关。
_ENABLE_FINNHUB_FALLBACK = False

# rs_universe.json 的板块表在进程内缓存（3.5 MB / 11k 条，一次解析即可）
_UNIVERSE_SECTOR_CACHE: dict[str, str] | None = None
_UNIVERSE_SECTOR_PATH: Path | None = None

# 排名池白名单（普通股 + 普通 ETF 的代码集合）在进程内缓存。
# 键 = (路径, mtime) —— 名单刷新后签名必须跟着变，所以缓存要认 mtime。
_POOL_WHITELIST_CACHE: tuple[str, int, frozenset[str]] | None = None

# 已经打过的池子日志（去重）。命中路径每个 ticker 都会走一遍，
# 不去重就是几十行同样的内容，真正的异常反而被淹掉。
_POOL_LOG_SEEN: set[str] = set()

# rs_daily_cache.json[date] 下的全市场分位数缓存 key（与 GICS 板块名不冲突）
UNIVERSE_CACHE_KEY = "__UNIVERSE__"
RS_RATING_MIN = 1
RS_RATING_MAX = 99

# 板块排名的口径版本。任何一次「排名怎么算」的改动都要动这个值，
# 否则旧的板块缓存条目（分母可能来自另一个池子）会继续被命中。
#
#   v2（2026-10-26）：数据源从「live pool 的票」改成「全市场宽表的票」，
#                     并且成员按同一个计量日（参考日）算。
_SECTOR_RANKING_VERSION = "v2-wide-wret"

# ── RS 计算常量 ──────────────────────────────────────────────────
WEIGHT_63 = 0.4
WEIGHT_126 = 0.2
WEIGHT_189 = 0.2
WEIGHT_252 = 0.2
MIN_HISTORY_DAYS = 252 + 1  # 多 1 行用于 ratio 计算
DIRTY_RETURN_THRESHOLD = 0.50  # 单日涨跌幅超过 ±50% 视为脏数据

# ── 脏数据检查窗口 ──────────────────────────────────────────────
# 只留给 tools/fix_dirty_history.py 用：它按「尾部 254 根里有几处 ±50% 跳空」
# 挑出值得重下的文件。那是一次性修复工具，用跳空当启发式没问题。
#
# 254 不能改回 253：R252 取的是 closes.iloc[-(252+1)] = iloc[-253]，
# 要能发现「跳空进入了 iloc[-253]」（它会污染 R252 的基准价）窗口必须是 254。
# 253 会漏掉这一种 —— 253 根切片的 pct_change() 首元素是 NaN，
# 而 iloc[-254] -> iloc[-253] 这一跳正好落在切片之外看不见的位置。
#
# RS 计算本身**不用**跳空做门槛了，三个回看线一律改用价格量级
# （见 _price_span_absurd 与 MAX_PRICE_SPAN）。
DIRTY_WINDOW_WEIGHTED = 252 + 2  # 254


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class RSResult:
    """RS 计算结果"""
    rs_spy: float                       # IBD 加权 RS/SPY 比值 (e.g. 1.05)
    sector: str                         # GICS Level 1 (e.g. "Information Technology")
    rs_10w_spy: Optional[float] = None  # 10 周 (50 日) 相对强度 vs SPY；None 表示无法计算
    rs_10d_spy: Optional[float] = None  # 10 日 相对强度 vs SPY；None 表示无法计算
    sector_rank: Optional[int] = None   # 同板块排名 (e.g. 5)；None 表示无法计算
    sector_total: Optional[int] = None  # 同板块总股数 (e.g. 18)
    rs_rating: Optional[int] = None     # O'Neil RS Rating 全市场百分位 1-99
    computed_at: str = ""               # YYYY-MM-DD

    def format_reason_segment(self) -> str:
        """
        格式化为信号 reason 段落：
          [RS] RS/SPY=X.XXx / RS-10W=Y.YYx / RS-10D=Z.ZZx / RS-Rating=NN (Sector 排名A/B)
        板块信息缺失时只显示 RS 值。

        RS/SPY 必须是第一个数字——signals/report.py 的 _extract_number() 依赖这一点
        提取 RS/SPY 倍数。
        """
        rs_str = f"{self.rs_spy:.2f}x"
        rs_10w_str = f" / RS-10W={self.rs_10w_spy:.2f}x" if self.rs_10w_spy is not None else ""
        rs_10d_str = f" / RS-10D={self.rs_10d_spy:.2f}x" if self.rs_10d_spy is not None else ""
        rs_rating_str = f" / RS-Rating={self.rs_rating}" if self.rs_rating is not None else ""
        rs_extra = rs_10w_str + rs_10d_str + rs_rating_str
        if self.sector_rank is not None and self.sector_total is not None and self.sector != "Unknown":
            return f"[RS] RS/SPY={rs_str}{rs_extra} ({self.sector} 排名{self.sector_rank}/{self.sector_total})"
        if self.sector and self.sector != "Unknown":
            return f"[RS] RS/SPY={rs_str}{rs_extra} ({self.sector})"
        return f"[RS] RS/SPY={rs_str}{rs_extra}"

    def to_column_dict(self) -> dict[str, str]:
        """
        结构化 RS 数据，直接写入 signals.csv 的 RS 列。

        返回的 key 与 signals/generator.py::_CSV_HEADERS 中的 RS 列名一一对应。
        无法计算的项为空字符串（CSV 里留空，而不是写 "None"）。
        """
        sector_rank = (
            f"{self.sector_rank}/{self.sector_total}"
            if self.sector_rank is not None and self.sector_total
            else ""
        )
        return {
            "RS相对强度": str(self.rs_rating) if self.rs_rating is not None else "",
            "RS/SPY": f"{self.rs_spy:.2f}",
            "RS-10W": f"{self.rs_10w_spy:.2f}" if self.rs_10w_spy is not None else "",
            "RS-10D": f"{self.rs_10d_spy:.2f}" if self.rs_10d_spy is not None else "",
            "板块排名": sector_rank,
        }


# signals.csv 里 RS 相关列的列名（generator.py 据此拼 CSV 表头）
RS_COLUMN_NAMES = ["RS相对强度", "RS/SPY", "RS-10W", "RS-10D", "板块排名"]


# ── 排名池排除名单──────────────────────────────────────────────
# 宽基基准 ETF 不能进排名池：SPY 的 RS/SPY 恒等于 1.0，它进池子等于
# 拿自己去比 —— 既占掉一个百分位位置，又是分母自己参与了自己的排序。
# main.py 还会强制注入 SPY / ASHR / EWT 做分市场基准，所以它们必然在
# 本地缓存里、必然混进池子。
#
# 排除只作用于**排名池**。SPY 作为 RS 的**基准**仍然必须能算出加权收益
# 给板块排名当分母 —— 所以不能在宽表那一层就把 SPY 抹掉。
#
# 板块/主题 ETF（XLK、XLF…）不在名单里：它们是可投资标的，而且没有
# GICS 行业，本来就不进板块排名的分母。
POOL_EXCLUDED_SYMBOLS = frozenset({"SPY", "QQQ", "DIA", "IWM", "VOO", "ASHR", "EWT"})


def _is_pool_excluded(symbol: str) -> bool:
    """这个代码该不该被排除在排名池之外。"""
    if not symbol or symbol.startswith("^"):
        return True
    return symbol in POOL_EXCLUDED_SYMBOLS


# ── 排名池白名单：什么才算「股票」─────────────────────────────────
#
# 宽表 `data/rs_universe_closes.pkl` 是从本地缓存汇总的，**它只反映本地
# 缓存里有什么**，不反映「什么才是股票」。实测（2026-10-28）：
#
#     旧池 9,591 只 → 白名单后 9,080 只，剔掉 511 只
#       · 256 只不在全市场名单里
#           21 只权证（RVMDW 排第 3、wret +512%；DRTSW 第 11）
#           136 只优先股 / 票据 / 混合票据
#           25 只杠杆反向 ETN（MicroSectors 系列，名字里没有 ETF 字样）
#           4 只 SPAC 单位、55 只境外 OTC / 已退市
#       · 255 只杠杆 / 反向 ETF（名单里有，但涨跌是结构性的）
#
# 权证不是股票 —— 混进横截面分母，RS Rating 的 1-99 刻度整体偏掉，
# 而日志里什么都看不出来（这是本项目最常见的静默失真）。
#
# 名单 `data/rs_universe.json` 才是判据：普通股 + ETF，
# 权证/优先股/票据/ETN 全部剔掉，杠杆反向 ETF 也不进分母。
#
# 名单缺失/损坏时返回 None —— 表示「不启用白名单」，**不是**「池子清空」。
# 把整个池子清空比多收几只权证更糟：RS Rating 会全部变成 0，日志上也看不出
# 是「名单没读到」还是「市场真的一只都没有」。

_POOL_ALLOWED_SECURITY_TYPES = frozenset({"common_stock", "etf"})


def _pool_symbol_key(symbol: str) -> str:
    """
    排名池里的代码归一形。

    Alpaca 用连字符写类别股（BRK-B），Nasdaq 名单用点（BRK.B）——
    不归一的话 Berkshire 会在池子里被数成两只，分子分母一起被污染。
    实测只有 BRK-B / BF-B 两种写法不一致。
    """
    return symbol.replace("-", ".").upper()


def _load_pool_whitelist(path: str | Path | None = None) -> frozenset[str] | None:
    """
    读全市场名单，返回排名池白名单（普通股 + 普通 ETF 的归一代码集合）。

    返回 None 表示名单不可用（文件缺失 / 损坏）→ 调用方不做名单过滤，
    而不是把池子清空。
    """
    global _POOL_WHITELIST_CACHE

    target = Path(path) if path else DEFAULT_RS_UNIVERSE
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        stamp = -1

    cached = _POOL_WHITELIST_CACHE
    if cached is not None and cached[0] == str(target) and cached[1] == stamp:
        return cached[2]

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("RS 名单 %s 读取失败，排名池不做名单过滤：%s", target, exc)
        return None

    symbols = payload.get("symbols") or {}
    if not isinstance(symbols, dict):
        return None

    keep: set[str] = set()
    for symbol, info in symbols.items():
        if not isinstance(info, dict):
            continue
        if info.get("security_type") not in _POOL_ALLOWED_SECURITY_TYPES:
            continue
        # 杠杆/反向 ETF 的涨跌是结构性的（每日重置），不是个股动量，
        # 放进横截面分母等于拿一个完全不同的东西去比。
        if info.get("is_leveraged"):
            continue
        keep.add(_pool_symbol_key(symbol))

    result = frozenset(keep)
    _POOL_WHITELIST_CACHE = (str(target), stamp, result)
    return result


def _pool_symbol_allowed(symbol: str, whitelist: frozenset[str] | None) -> bool:
    """这个代码有没有资格进排名池（白名单为 None 时不拦）。"""
    if whitelist is None:
        return True
    return _pool_symbol_key(symbol) in whitelist

# RS/SPY 可能为负（SPY 加权收益为负时出现，如 GRO 的 -0.48x），所以要带负号
_RE_RS_SPY = re.compile(r"RS/SPY=(-?[\d.]+)")
_RE_RS_10W = re.compile(r"RS-10W=(-?[\d.]+)")
_RE_RS_10D = re.compile(r"RS-10D=(-?[\d.]+)")
_RE_RS_RATING = re.compile(r"RS-Rating=(\d+)")
_RE_SECTOR_RANK = re.compile(r"排名(\d+)/(\d+)")


# ── 计量日（参考日）────────────────────────────────────────────
# 参考日 = 宽表里最后一个「全市场都有数据」的交易日。
# 排名池的分布、ticker 的加权收益、板块成员的 RS，三者必须按**同一天**算 ——
# 否则就是拿「今天涨了」去比「昨天涨的」，横截面百分位彻底失真。
#
# 缓存键里带宽表文件的 mtime：宽表每轮 live run 都会被增量更新，
# 参考日可能随之前进，缓存必须跟着失效。
_REFERENCE_DAY_CACHE: dict[tuple[str, str, int], pd.Timestamp | None] = {}

# 宽表加权收益（symbol -> wret）的进程内缓存。键含宽表 mtime：
# live run 每轮都会增量更新宽表，参考日可能随之前进，缓存必须跟着失效。
_WIDE_WRETS_CACHE: dict[tuple[str, int, str], pd.Series] = {}


def _wide_wrets_by_symbol(as_of=None, path=None) -> pd.Series | None:
    """
    宽表里每只票的加权收益（Series，index=symbol，算不出的为 NaN）。

    排名池和板块排名必须共用这一份数：RS Rating 的分母和板块排名的分母
    是**同一个横截面**，否则两个百分位互相矛盾（一个说全市场第 3，
    一个说板块第 40/42）。

    宽表缺失时返回 None，调用方退回 market_data 的窄池。
    """
    target = Path(path) if path is not None else DEFAULT_RS_CLOSES_PATH
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(target), stamp, str(as_of))
    if key in _WIDE_WRETS_CACHE:
        return _WIDE_WRETS_CACHE[key]

    wide = load_universe_closes(target)
    if wide is None:
        return None
    wr = vectorized_weighted_return(wide, as_of=as_of)
    _WIDE_WRETS_CACHE[key] = wr
    return wr


def universe_reference_day(as_of=None, path=None) -> pd.Timestamp | None:
    """
    排名池的计量日：宽表里最后一个「全市场都有数据」的交易日。

    宽表由 `data/cache/*.pkl` 汇总而成，而 live run 每轮只刷新 live pool，
    剩下全市场那大半还停在上一交易日 —— 宽表末行因此只覆盖约一半，
    拿末行当「最新交易日」会让排名池静默腰斩（2026-10-26 实测 9,776 → 4,885）。

    返回 None 表示宽表缺失或没有一行够格，调用方按「不切」处理。
    """
    target = Path(path) if path is not None else DEFAULT_RS_CLOSES_PATH
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(target), str(as_of), stamp)
    if key in _REFERENCE_DAY_CACHE:
        return _REFERENCE_DAY_CACHE[key]

    wide = load_universe_closes(target)
    ref = None if wide is None else market_reference_day(wide, as_of=as_of)
    _REFERENCE_DAY_CACHE[key] = ref
    return ref


def _measurement_day(as_of: datetime) -> datetime:
    """
    真正用来算收益的那一天 = min(as_of, 参考日)。

    as_of 是「请求算哪天」（回看历史信号时会更早），参考日是「市场数据齐到哪天」。
    取两者的较小值，保证 ticker 与排名池落在同一天。
    """
    ref = universe_reference_day(as_of=as_of)
    if ref is None:
        return as_of
    ref_dt = ref.to_pydatetime()
    return min(as_of, ref_dt)


def parse_rs_columns_from_reason(reason: str) -> dict[str, str]:
    """
    从历史信号的 触发原因 自由文本里回填 RS 列（老记录没有独立 RS 列时使用）。

    只能回填 RS/SPY / RS-10W / RS-10D / 板块排名——RS-Rating 是新加的字段，
    老记录的 reason 里没有，缺失即为空。
    """
    if not reason:
        return {}
    # 只看 [RS] 段，避免从 [EMA10回踩] 段的 "上穿EMA10($x.xx)" 里误抓数字
    for part in reason.split(" | "):
        part = part.strip()
        if not part.startswith("[RS]"):
            continue
        cols: dict[str, str] = {}
        m = _RE_RS_SPY.search(part)
        if m:
            cols["RS/SPY"] = m.group(1)
        m = _RE_RS_10W.search(part)
        if m:
            cols["RS-10W"] = m.group(1)
        m = _RE_RS_10D.search(part)
        if m:
            cols["RS-10D"] = m.group(1)
        m = _RE_RS_RATING.search(part)
        if m:
            cols["RS相对强度"] = m.group(1)
        m = _RE_SECTOR_RANK.search(part)
        if m:
            cols["板块排名"] = f"{m.group(1)}/{m.group(2)}"
        return cols
    return {}


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def compute_rs(
    ticker: str,
    as_of_date: str | datetime,
    market_data: dict[str, pd.DataFrame],
    sector_cache_path: str | Path | None = None,
    rs_daily_cache_path: str | Path | None = None,
    with_rating: bool = True,
) -> RSResult | None:
    """
    计算 ticker 在 as_of_date 的 RS/SPY + O'Neil RS Rating + 同板块排名。

    参数：
        ticker: 股票代码（如 "AAPL"）
        as_of_date: 截止日期（str "YYYY-MM-DD" 或 datetime）
        market_data: 策略侧持有的价格数据 dict，必须包含 ticker 与 "SPY" 两个 key
        sector_cache_path: 板块缓存文件路径（默认 data/sector_cache.json）
        rs_daily_cache_path: RS 日级缓存文件路径（默认 data/rs_daily_cache.json）
        with_rating: 是否计算 O'Neil RS Rating（全市场百分位，首次未命中需扫全市场约 15s）

    返回：
        RSResult — 成功
        None — 历史不足 / SPY 缺失 / 数据脏

    抛异常：
        不抛。所有失败情形都返回 None 或带 sector='Unknown' 的 RSResult。
    """
    as_of = _normalize_date(as_of_date)
    sector_cache_path = Path(sector_cache_path) if sector_cache_path else DEFAULT_SECTOR_CACHE
    rs_daily_cache_path = Path(rs_daily_cache_path) if rs_daily_cache_path else DEFAULT_RS_DAILY_CACHE

    # 计量日 = min(as_of, 参考日)。排名池的分布按参考日算，ticker 也必须
    # 按参考日算 —— 两边各用各的「最后一根 bar」就是跨日比较，百分位彻底失真。
    # as_of 仍然用于缓存的日期键和 computed_at（那是被请求的那一天）。
    measure_at = _measurement_day(as_of)

    # 1. 计算 ticker 和 SPY 的加权收益
    ticker_ret = _compute_weighted_return(market_data.get(ticker), measure_at, ticker=ticker)
    if ticker_ret is None:
        return None

    spy_df = market_data.get("SPY")
    spy_ret = _compute_weighted_return(spy_df, measure_at, ticker="SPY")
    if spy_ret is None or spy_ret == 0:
        return None

    rs_spy = ticker_ret / spy_ret

    # 1.5 计算 10 周相对强度（独立于加权 RS，纯简单收益率比）
    ticker_10w = _compute_10w_return(market_data.get(ticker), measure_at, ticker=ticker)
    spy_10w = _compute_10w_return(spy_df, measure_at, ticker="SPY")
    # IBD 标准的相对强度公式是 (1+r_ticker)/(1+r_spy) 而非 r_ticker/r_spy
    # 后者在 SPY 短期涨幅很小时（如 10W 仅 +3.5%）会爆炸式放大（如算出 7.99x 而非 1.24x）
    rs_10w_spy: Optional[float] = (
        (1 + ticker_10w) / (1 + spy_10w)
        if (ticker_10w is not None and spy_10w is not None and (1 + spy_10w) != 0)
        else None
    )

    # 1.6 计算 10 日相对强度（更短期的动量确认）
    ticker_10d = _compute_10d_return(market_data.get(ticker), measure_at, ticker=ticker)
    spy_10d = _compute_10d_return(spy_df, measure_at, ticker="SPY")
    rs_10d_spy: Optional[float] = (
        (1 + ticker_10d) / (1 + spy_10d)
        if (ticker_10d is not None and spy_10d is not None and (1 + spy_10d) != 0)
        else None
    )

    # 2. 获取板块（rs_universe.json > sector_cache.json > Finnhub > Unknown）
    sector_cache = _load_sector_cache_safe(sector_cache_path)
    gics_sector = _get_gics_sector(ticker, sector_cache)

    # 3. 板块内排名（优先用日级缓存）
    rank, total = _get_sector_rank(
        ticker=ticker,
        sector=gics_sector,
        rs_spy=rs_spy,
        as_of=as_of,
        market_data=market_data,
        cache_path=rs_daily_cache_path,
        measure_at=measure_at,
    )

    # 4. O'Neil RS Rating：ticker 的加权收益在全市场中的百分位（1-99）
    rs_rating = (
        _get_rs_rating(
            ticker_wret=ticker_ret,
            as_of=as_of,
            market_data=market_data,
            cache_path=rs_daily_cache_path,
            measure_at=measure_at,
        )
        if with_rating
        else None
    )

    return RSResult(
        rs_spy=rs_spy,
        rs_10w_spy=rs_10w_spy,
        rs_10d_spy=rs_10d_spy,
        sector=gics_sector,
        sector_rank=rank,
        sector_total=total,
        rs_rating=rs_rating,
        computed_at=as_of.strftime("%Y-%m-%d"),
    )


# ── 已知「历史断点」（手动 hard-coded） ────────────────────────
# 表和实现都在 data/rs_universe.py（KNOWN_PRICE_BREAKS / break_inside_window）：
# 宽表路径也必须做同一件事，否则断点落入窗口时两条路径一个进池一个落选。
# 实测（2026-10-28）WOLF 那次是 Chapter 11 重组（旧股注销换新股）、
# NEUP 那次是换股合并 —— 断点前后是两个不同的证券，价格不可比，
# 硬算出来的收益率是假的（NEUP 在 as_of=2025-11-15 是 +265.7%）。


# ════════════════════════════════════════════════════════════════
# 内部辅助函数
# ════════════════════════════════════════════════════════════════

def _normalize_date(date: str | datetime) -> datetime:
    """统一日期格式为 tz-aware UTC datetime。"""
    if isinstance(date, str):
        dt = datetime.strptime(date, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    if isinstance(date, datetime):
        if date.tzinfo is None:
            return date.replace(tzinfo=timezone.utc)
        return date
    raise TypeError(f"date must be str or datetime, got {type(date)}")


def _slice_to_date(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """
    切片到 as_of 当日（含），保留 tz 一致性。

    比较的是**日期**不是时刻
    ----------------------
    日线 bar 的时间戳是 04:00 UTC（美东当日零点），而 as_of 是当天
    00:00 UTC。用 `df.index <= as_of` 比时刻会把 as_of 当天那根 bar 切掉：

        data/cache/RCEL.pkl 索引末尾
        2026-10-22 00:00:00+00:00
        2026-10-22 04:00:00+00:00
        2026-10-23 04:00:00+00:00     <- 04:00 > 00:00，被切掉

    宽表路径（`data/rs_universe.py::_slice_to_asof`）比的是**日期**
    （`wide.index <= ts.normalize()`），宽表索引本来就是零点归一的。
    两边一时刻一日期 → ticker 停在昨天、排名池停在今天 → **跨日比较**，
    RS Rating 的百分位直接失真。

    实测偏差（2026-10-26 修复前）：RCEL 在计量日 10-23 上，
    标量路径算出 2.545476（其实是 10-22 的数），宽表算出 2.488095。
    """
    if df is None or df.empty:
        return df
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC") if hasattr(idx, "tz_localize") else idx
        df = df.copy()
        df.index = idx
    if not isinstance(idx, pd.DatetimeIndex):
        return df[df.index <= as_of]
    cutoff = pd.Timestamp(as_of).tz_convert("UTC").normalize()
    return df[idx.normalize() <= cutoff]


def _deduplicate_bars(df: pd.DataFrame) -> pd.Series:
    """
    pkl 缓存里同一日期可能有多个时间戳（4:00 / 13:30 / 14:30 ...），按日期保留第一条。
    返回以 normalize 后的日期为索引的收盘价 Series。
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if not isinstance(df.index, pd.DatetimeIndex):
        return df["close"]

    dates = df.index.normalize()  # 00:00:00 化的日期
    grouped = df.groupby(dates).first()
    return grouped["close"]


def _price_span_absurd(closes: pd.Series, window: int = MIN_HISTORY_DAYS) -> bool:
    """
    整段窗口的 max/min 价格比是否荒谬（超过 MAX_PRICE_SPAN）。

    这是 RS 三个回看线**唯一**的排脏门槛，取代原来的「单日 ±50% 跳空」。
    涨跌幅分不出「RCEL +63.6% 的好消息」和「没复权的反向拆股痕迹」，
    而价格量级分得清：真实股票再疯也到不了 1000 倍，坏数据一眼就被抓住。

    window 取「该函数真正用到的 bar 数」：weighted 是 R252 的 253 根，
    10w 是 51 根，10d 是 11 根。窗口外的价格不参与任何计算，不该牵连入选。

    min <= 0 时比值无意义，一律算荒谬 —— 负价本身就不该进排名池。
    """
    win = closes.iloc[-window:]
    lo = float(win.min())
    if not _usable_price(lo):
        return True
    return bool(win.max() / lo > _rsu.MAX_PRICE_SPAN)


def _usable_price(value: float) -> bool:
    """
    收盘价能否参与收益计算：必须是有限的正数。

    注意不能只写 `value <= 0` —— NaN 与任何数比较都返回 False，
    NaN 会因此穿过守卫、被算进加权收益并污染全市场分位数排名。
    """
    return bool(math.isfinite(value) and value > 0)


def _window_span_days(closes: pd.Series, window: int) -> int:
    """
    最近 window 根 bar 横跨了多少个自然日。

    「253 根 bar」正常摊在 366 个自然日里。数据中间少一大截时（比如
    Alpaca 的 `volume == 0` 占位行被我们的 `volume > 0` 过滤剔掉），
    这 253 根 bar 会摊在两年、三年甚至十年里 —— 那算出来的根本不是
    「12 个月的相对强度」，而是「上一次有数据到现在」。

    量不出跨度时（索引不是时间戳）返回 0：宁可放过，不能因为
    量不出来就把整只票判死。
    """
    win = closes.iloc[-window:]
    if len(win) < 2:
        return 0
    idx = win.index
    if not isinstance(idx, pd.DatetimeIndex):
        return 0
    return int((idx[-1] - idx[0]).days)


def _compute_weighted_return(df: pd.DataFrame | None, as_of: datetime, ticker: str = "") -> float | None:
    """
    计算 0.4*R63 + 0.2*R126 + 0.2*R189 + 0.2*R252 的加权季度收益。

    返回 None 的情形：
      - df 为空
      - 切片后历史不足 253 个交易日（252 + 1 个 ratio）
      - 任意 lookback 点的收盘价为 0/负/NaN
      - 窗口内价格量级荒谬（max/min 超过 MAX_PRICE_SPAN，见该常量注释）
      - 窗口跨越已知历史断点（重组/合并，断点前后的价格不可比）

    参数：
        ticker: 用于查 KNOWN_PRICE_BREAKS（"SPY" 等基准无需传）
    """
    if df is None or df.empty:
        return None

    sliced = _slice_to_date(df, as_of)
    closes = _deduplicate_bars(sliced)

    if len(closes) < MIN_HISTORY_DAYS:
        return None

    # 窗口跨越已知历史断点 → 不给数（重组/合并前后是两个不同的证券）。
    if ticker and _break_inside_window(ticker, closes, MIN_HISTORY_DAYS):
        return None

    # 排脏门槛 = 整段 253 根窗口的 max/min 价格比，**不是**单日涨跌幅。
    # 数据已复权（Alpaca adjustment="all"），此时窗口里的 ±50% 是真实行情，
    # 用涨跌幅去判会把 RCEL（+63.6%、量比 43x）这类强势股整只误杀。
    if _price_span_absurd(closes):
        return None

    # 第二道闸门：窗口的日历跨度。
    # 253 根 bar 摊在 400 天以上 = 中间有大空洞 = 这不是 12 个月的相对强度。
    # 见 MAX_WINDOW_SPAN_DAYS 的注释（实测 440 只票栽在这上面）。
    if _window_span_days(closes, MIN_HISTORY_DAYS) > _rsu.MAX_WINDOW_SPAN_DAYS:
        return None

    current = float(closes.iloc[-1])
    if not _usable_price(current):
        return None

    # 4 个 lookback 点（用 .iloc[-n-1] 而不是 .iloc[-n] 是为了拿更早一个交易日）
    # R63 = (close[-1] / close[-64]) - 1（即 63 个交易日前的 close）
    returns = []
    for lookback in (63, 126, 189, 252):
        if len(closes) < lookback + 1:
            return None
        past = float(closes.iloc[-(lookback + 1)])
        if not _usable_price(past):
            return None
        r = (current / past) - 1
        returns.append(r)

    return (
        WEIGHT_63 * returns[0]
        + WEIGHT_126 * returns[1]
        + WEIGHT_189 * returns[2]
        + WEIGHT_252 * returns[3]
    )


# 10 周 ≈ 50 个交易日
LOOKBACK_10W_DAYS = 50
MIN_HISTORY_10W_DAYS = LOOKBACK_10W_DAYS + 1  # 51 行


def _compute_10w_return(
    df: pd.DataFrame | None,
    as_of: datetime,
    ticker: str = "",
) -> float | None:
    """
    计算 50 个交易日（约 10 周）简单收益率：close[-1] / close[-51] - 1。

    返回 None 的情形：
      - df 为空
      - 切片后历史不足 51 个交易日
      - 窗口内价格量级荒谬（max/min 超过 MAX_PRICE_SPAN，见该常量注释）
      - 当前或 10 周前的收盘价为 0/负/NaN
      - 窗口跨越已知历史断点（重组/合并）

    参数：
        ticker: 用于查 KNOWN_PRICE_BREAKS
    """
    if df is None or df.empty:
        return None

    sliced = _slice_to_date(df, as_of)
    closes = _deduplicate_bars(sliced)

    if len(closes) < MIN_HISTORY_10W_DAYS:
        return None

    # 断点在这条线自己的窗口里 → 不给数（与 weighted 同规则，只是窗口更短）。
    if ticker and _break_inside_window(ticker, closes, MIN_HISTORY_10W_DAYS):
        return None

    # 量级窗口 = 51 根，正好是 close[-51] 这个基准价所在的范围。
    # 与 weighted 同规则，只是不该让更早的价格牵连 10 周这条线。
    if _price_span_absurd(closes, MIN_HISTORY_10W_DAYS):
        return None

    # 跨度闸门按**这条线自己的**窗口量：51 根摊在 100 天以上就是有空洞。
    if _window_span_days(closes, MIN_HISTORY_10W_DAYS) > _rsu.MAX_WINDOW_SPAN_DAYS_10W:
        return None

    current = float(closes.iloc[-1])
    if not _usable_price(current):
        return None

    past = float(closes.iloc[-(LOOKBACK_10W_DAYS + 1)])
    if not _usable_price(past):
        return None

    return (current / past) - 1


# 10 个交易日 ≈ 2 周
LOOKBACK_10D_DAYS = 10
MIN_HISTORY_10D_DAYS = LOOKBACK_10D_DAYS + 1  # 11 行


def _compute_10d_return(
    df: pd.DataFrame | None,
    as_of: datetime,
    ticker: str = "",
) -> float | None:
    """
    计算 10 个交易日（约 2 周）简单收益率：close[-1] / close[-11] - 1。

    返回 None 的情形：
      - df 为空
      - 切片后历史不足 11 个交易日
      - 窗口内价格量级荒谬（max/min 超过 MAX_PRICE_SPAN，见该常量注释）
      - 当前或 10 天前的收盘价为 0/负/NaN
      - 窗口跨越已知历史断点（重组/合并）

    参数：
        ticker: 用于查 KNOWN_PRICE_BREAKS
    """
    if df is None or df.empty:
        return None

    sliced = _slice_to_date(df, as_of)
    closes = _deduplicate_bars(sliced)

    if len(closes) < MIN_HISTORY_10D_DAYS:
        return None

    # 断点在这条线自己的窗口里 → 不给数（与 weighted 同规则，只是窗口更短）。
    if ticker and _break_inside_window(ticker, closes, MIN_HISTORY_10D_DAYS):
        return None

    # 量级窗口 = 11 根，正好是 close[-11] 这个基准价所在的范围。
    if _price_span_absurd(closes, MIN_HISTORY_10D_DAYS):
        return None

    # 跨度闸门按**这条线自己的**窗口量：11 根摊在 25 天以上就是有空洞。
    if _window_span_days(closes, MIN_HISTORY_10D_DAYS) > _rsu.MAX_WINDOW_SPAN_DAYS_10D:
        return None

    current = float(closes.iloc[-1])
    if not _usable_price(current):
        return None

    past = float(closes.iloc[-(LOOKBACK_10D_DAYS + 1)])
    if not _usable_price(past):
        return None

    return (current / past) - 1


def _load_sector_cache_safe(path: Path | None = None) -> dict:
    """读取板块缓存，文件不存在/损坏时返回空 dict。"""
    try:
        return _load_sector_cache(str(path) if path else None)
    except Exception:
        return {}


def _save_sector_cache_safe(cache: dict, path: Path | None = None) -> None:
    """保存板块缓存，失败静默（不影响主流程）。"""
    try:
        _save_sector_cache(cache, str(path) if path else None)
    except Exception:
        pass


def _enable_finnhub_fallback() -> bool:
    """Finnhub 兜底开关（默认关，见常量处的说明）。"""
    return _ENABLE_FINNHUB_FALLBACK


def _load_universe_sectors(path: Path | None = None) -> dict[str, str]:
    """
    读 rs_universe.json，返回 {symbol: GICS 板块}。

    只收普通股的**已知**板块 —— ETF 与 Unknown 一律不进：
      - ETF 没有 GICS 行业，混进去只会让板块分母变得不可比；
      - Unknown 进表会让「Unknown 板块」凭空拥有一堆成员，
        而 `_get_sector_rank` 本来就把 Unknown 排除在排名之外。

    文件缺失/损坏返回空 dict，调用方退回老的 sector_cache 路径。
    """
    global _UNIVERSE_SECTOR_CACHE, _UNIVERSE_SECTOR_PATH

    target = Path(path) if path else DEFAULT_RS_UNIVERSE
    if _UNIVERSE_SECTOR_CACHE is not None and _UNIVERSE_SECTOR_PATH == target:
        return _UNIVERSE_SECTOR_CACHE

    sectors: dict[str, str] = {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        for symbol, info in (payload.get("symbols") or {}).items():
            if info.get("is_etf"):
                continue
            gics = info.get("gics_sector") or "Unknown"
            if gics and gics != "Unknown":
                sectors[symbol] = gics
    except Exception as exc:
        logger.warning("RS 名单 %s 读取失败，板块退回 sector_cache：%s", target, exc)
        sectors = {}

    _UNIVERSE_SECTOR_CACHE = sectors
    _UNIVERSE_SECTOR_PATH = target
    return sectors


def _get_gics_sector(
    ticker: str,
    sector_cache: dict,
    universe_path: Path | None = None,
) -> str:
    """
    取 ticker 的 GICS L1 板块，优先级：

      1. rs_universe.json 的 gics_sector —— Nasdaq 直接给 L1，全市场覆盖
      2. sector_cache.json 的 Finnhub 行业串 → industry_to_gics()
      3. Finnhub 实时拉取（默认关，见 _ENABLE_FINNHUB_FALLBACK）
      4. Unknown

    为什么名单优先：sector_cache.json 只有 375 条，绝大多数票落 Unknown 直接
    退出板块排名 —— 报告里 `Consumer Staples 排名2/2` 就是这么来的，
    不是板块真的小。而 Finnhub 的 industry 是 Level 2/3 名称，还要再过一次
    industry_to_gics()；多一层转换就多一处「静默变 Unmapped」的机会。
    """
    sector = _load_universe_sectors(universe_path).get(ticker)
    if sector:
        return sector

    info = _get_sector_info(ticker, sector_cache)
    if info:
        industry = info.get("industry") or ""
        if industry and industry != "Unknown":
            gics = industry_to_gics(industry)
            # industry_to_gics 对认不出的行业返回 "Unmapped"。那不是一个板块名，
            # 报告里出现 "Unmapped 排名3/50" 是错数据 —— 老实落 Unknown。
            return gics if gics in _KNOWN_GICS_SECTORS else "Unknown"
    return "Unknown"


def _get_sector_info(ticker: str, cache: dict) -> dict | None:
    """从缓存读 ticker 的行业信息，未命中时尝试 Finnhub 拉取。"""
    if ticker in cache:
        info = cache[ticker]
        if info.get("fetch_failed") or info.get("empty"):
            return None
        return info

    # 缓存未命中 → Finnhub（默认关：未命中一次阻塞 1.05s，
    # 全市场回算会有成千上万次未命中，见 _ENABLE_FINNHUB_FALLBACK）
    if not _enable_finnhub_fallback():
        return None
    try:
        profile = _fetch_finnhub_profile(ticker)
    except Exception:
        return None
    if profile is None:
        return {"industry": "Unknown", "name": "", "market_cap": 0, "fetch_failed": True}
    return profile


def _get_sector_rank(
    ticker: str,
    sector: str,
    rs_spy: float,
    as_of: datetime,
    market_data: dict[str, pd.DataFrame],
    cache_path: Path,
    sector_cache_path: Path | None = None,
    measure_at: datetime | None = None,
) -> tuple[Optional[int], Optional[int]]:
    """
    获取 ticker 在 sector 内按 RS/SPY 的排名。

    优先读 rs_daily_cache.json[date][sector] 缓存；
    未命中时懒加载：对所有 ticker 计算 RS 并写入缓存。

    as_of 是缓存的日期键（被请求的那一天）；measure_at 是真正用来算收益的
    计量日 —— 板块成员必须跟 ticker 按同一天算，否则「排名 x/N」的分母
    跟分子来自不同的日子。measure_at 省略时退回 as_of。
    """
    if sector in ("Unknown", "Unmapped"):
        return None, None

    date_str = as_of.strftime("%Y-%m-%d")
    cache = _load_rs_daily_cache(cache_path)
    sector_cache_path = sector_cache_path or DEFAULT_SECTOR_CACHE

    cached_sector = cache.get(date_str, {}).get(sector)
    if cached_sector is not None:
        # 签名对不上 = 这份排名是按**另一个板块成员名单**算的，必须重算。
        # 板块成员从 sector_cache.json（375 条）换成 rs_universe.json（全市场）之后，
        # 旧条目还在原地，报告里照样印「Consumer Staples 排名2/2」。
        current_sig = _sector_pool_signature(sector, sector_cache_path)
        if cached_sector.get("pool_sig") == current_sig:
            # 缓存命中：用缓存里同 sector 的所有 ticker 的 RS 列表与 ticker 的 RS 比较
            rs_list = cached_sector.get("rs_list", [])
            # 找到 ticker 的 rs_spy 在列表里的位置
            rank = _rank_in_list(rs_spy, rs_list)
            total = len(rs_list)
            return rank, total
        logger.info(
            "板块排名缓存已失效（%s 签名 %s -> %s），重算 %s",
            sector,
            cached_sector.get("pool_sig", "<无>"),
            current_sig,
            date_str,
        )

    # 缓存未命中 → 懒加载同 sector 所有 ticker
    try:
        populated = _populate_sector_cache(
            sector=sector,
            as_of=as_of,
            market_data=market_data,
            sector_cache_path=sector_cache_path or DEFAULT_SECTOR_CACHE,
            rs_daily_cache_path=cache_path,
            measure_at=measure_at,
        )
    except Exception:
        return None, None

    if not populated:
        return None, None

    # 重新读缓存
    cache = _load_rs_daily_cache(cache_path)
    cached_sector = cache.get(date_str, {}).get(sector)
    if cached_sector is None:
        return None, None

    rs_list = cached_sector.get("rs_list", [])
    rank = _rank_in_list(rs_spy, rs_list)
    total = len(rs_list)
    return rank, total


def _rank_in_list(target_rs: float, rs_list: list[dict]) -> Optional[int]:
    """
    给定 target_rs 和 rs_list（每项含 rs_spy），返回 target_rs 排名（1-based）。
    rs_list 中可能不含 target 自身，因此按值排名而非按 ticker 匹配。

    rank 范围：1..len(rs_list)
    - target 最大：rank = 1
    - target 最小且唯一：rank = len(rs_list)
    - target 比 rs_list 中所有都小（rs_list 不含 target）：rank = len(rs_list)
    """
    if not rs_list:
        return None
    higher = sum(1 for x in rs_list if x["rs_spy"] > target_rs)
    return min(higher + 1, len(rs_list))


# ════════════════════════════════════════════════════════════════
# 日级 RS 缓存（按 sector 懒加载）
# ════════════════════════════════════════════════════════════════

def _load_rs_daily_cache(path: Path) -> dict:
    """读取 rs_daily_cache.json，文件不存在时返回空 dict。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_rs_daily_cache(cache: dict, path: Path) -> None:
    """保存 rs_daily_cache.json，保留最近 RS_DAILY_CACHE_MAX_DAYS 天的数据。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 滚动：删除超过保留期的日期
        if cache:
            all_dates = sorted(cache.keys())
            if len(all_dates) > RS_DAILY_CACHE_MAX_DAYS:
                for d in all_dates[:-RS_DAILY_CACHE_MAX_DAYS]:
                    cache.pop(d, None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
    except Exception:
        pass


def _sector_members(
    sector: str,
    sector_cache_path: Path,
    universe_path: Path | None = None,
) -> list[str]:
    """
    列出属于该 GICS 板块的 ticker（只含普通股，ETF 天然被名单过滤掉）。

    优先用 rs_universe.json；它缺失/为空时才退回 sector_cache.json。
    """
    universe = _load_universe_sectors(universe_path)
    if universe:
        return [t for t, gics in universe.items() if gics == sector]

    sector_cache = _load_sector_cache_safe(sector_cache_path)
    members: list[str] = []
    for t, info in sector_cache.items():
        if t in ("SPY", "^VIX"):
            continue
        industry = info.get("industry") or ""
        if not industry or industry == "Unknown":
            continue
        if industry_to_gics(industry) == sector:
            members.append(t)
    return members


def _sector_pool_signature(
    sector: str,
    sector_cache_path: Path,
    universe_path: Path | None = None,
) -> str:
    """
    板块排名池的指纹：这块板块**有哪些成员** + **排名口径是哪一版**。

    为什么板块条目也要签名：`rs_daily_cache.json[date][sector]["rs_list"]`
    是「按当时那块板块有哪些票」算出来的排名表。2026-10-23 之前板块成员
    来自 `sector_cache.json`（只有 375 条 Finnhub 行业记录），
    Consumer Staples 只能数出 **2** 只 —— 报告里就印 `Consumer Staples 排名2/2`。

    换成 `rs_universe.json` 之后成员变成 142 只，**但旧缓存条目还在原地**：
    报告照样打 `排名2/2`。改了代码看不到效果，而且不报任何错 ——
    这跟 `__UNIVERSE__` 条目加 `pool_sig` 要防的是同一类静默空跑，
    只是当时漏在了板块条目上。

    签名只覆盖**成员名单**，不覆盖价格：某个历史日期的板块排名在价格定稿后
    就是稳定的，按成员名单失效就够了，也不会天天把缓存全部作废。

    但**口径版本必须进签名**：2026-10-26 板块排名的数据源从「live pool 的票」
    换成「全市场宽表的票」，成员名单一个字没变（Financials 还是那 1,210 只），
    算出来的分母却从 811 变成 1,210 —— 签名不变的话旧条目照样命中，
    报告里印的还是那个用窄池算出来的分母。
    """
    members = sorted(_sector_members(sector, sector_cache_path, universe_path))
    digest = hashlib.sha256()
    digest.update(sector.encode("utf-8"))
    digest.update(_SECTOR_RANKING_VERSION.encode("utf-8"))
    digest.update("\x00".join(members).encode("utf-8"))
    return digest.hexdigest()[:16]


def _populate_sector_cache(
    sector: str,
    as_of: datetime,
    market_data: dict[str, pd.DataFrame],
    sector_cache_path: Path,
    rs_daily_cache_path: Path,
    universe_path: Path | None = None,
    measure_at: datetime | None = None,
) -> bool:
    """
    对 sector 内所有已知 ticker 计算 RS，写入 rs_daily_cache.json[date][sector]。

    成员名单优先取 rs_universe.json（全市场 GICS L1）；名单缺失时退回
    sector_cache.json 的 Finnhub 行业串 + industry_to_gics()。

    为什么不能用 sector_cache.json 当唯一来源：它只有 375 条，
    Consumer Staples 只能数出 2 只票，报告里就打印 `排名2/2` ——
    分母小到没有意义，却看起来像「这个板块就这么小」。

    ETF 不进板块排名（无 GICS 行业），返回 False 表示没生成成功。

    measure_at 是计量日（见 compute_rs）：成员必须跟 ticker 按同一天算。
    """
    sector_tickers = _sector_members(sector, sector_cache_path, universe_path)
    if not sector_tickers:
        return False

    measure_at = measure_at or as_of
    wide_wrets = _wide_wrets_by_symbol(measure_at)

    def _wret(symbol: str) -> float | None:
        """
        一只票的加权收益。

        宽表优先 —— 它覆盖全市场，而 market_data 只有 live pool。
        宽表里算不出来（落后于参考日 / 攒不满 253 根 bar / 价格量级荒谬）
        就直接排除，跟排名池同口径；宽表里**根本没有**这只票时（还没进过
        数据缓存）才退回 market_data。
        """
        if wide_wrets is not None and symbol in wide_wrets.index:
            value = wide_wrets.get(symbol)
            if value is None or pd.isna(value):
                return None
            return float(value)
        return _compute_weighted_return(market_data.get(symbol), measure_at, ticker=symbol)

    spy_r = _wret("SPY")
    if spy_r is None or spy_r == 0:
        return False

    rs_list = []
    for t in sector_tickers:
        r = _wret(t)
        if r is None:
            continue
        rs_list.append({"ticker": t, "rs_spy": r / spy_r})

    if not rs_list:
        return False

    # 按 rs_spy 降序
    rs_list.sort(key=lambda x: -x["rs_spy"])

    # 写缓存
    cache = _load_rs_daily_cache(rs_daily_cache_path)
    date_str = as_of.strftime("%Y-%m-%d")
    cache.setdefault(date_str, {})[sector] = {
        "rs_list": rs_list,
        "pool_sig": _sector_pool_signature(sector, sector_cache_path, universe_path),
    }
    _save_rs_daily_cache(cache, rs_daily_cache_path)
    return True


# ════════════════════════════════════════════════════════════════
# O'Neil RS Rating（全市场百分位 1-99）
# ════════════════════════════════════════════════════════════════

def _get_rs_rating(
    ticker_wret: float,
    as_of: datetime,
    market_data: dict[str, pd.DataFrame],
    cache_path: Path,
    measure_at: datetime | None = None,
) -> Optional[int]:
    """
    O'Neil RS Rating：把 ticker 的 IBD 加权收益换算成全市场百分位（1-99）。

    计算方式（与 IBD 官方口径一致的部分）：
      1. 每只股票算 0.4*R63 + 0.2*R126 + 0.2*R189 + 0.2*R252 加权收益
      2. 与全市场所有可比股票排序
      3. 百分位 → 1-99 整数

    与 IBD 的差异（重要）：
      - IBD 用自己维护的 ~4800 只专有股票池，这里用全市场宽表
        （10,694 列里 9,582 只有效，2026-10-22 实测）
      - 落选的是：最后一个交易日没有 bar 的（969）、窗口内攒不满 253 根 bar 的
        低流动性票（107）、价格量级荒谬的坏数据（36）
      - 排脏判据是**价格量级**不是涨跌幅 —— 见 MAX_PRICE_SPAN。旧判据
        （单日 ±50%）会把 RCEL（+63.6%、量比 43x）这类真实强势股整只误杀，
        实测 6303 只里只有 3844 只进池，换成量级判据后是 5001 只，
        再补齐全市场历史后是 9582 只
      - 所以绝对数值跟 IBD 官网对不上，但相对排序可用于筛选领涨股

    历史不足 253 个交易日的股票会被排除（返回 None），因为算不出加权收益。
    """
    wrets = _get_universe_wrets(as_of, market_data, cache_path, measure_at=measure_at)
    if not wrets:
        return None

    n = len(wrets)
    # wrets 已升序排列；bisect_left 返回严格小于 ticker_wret 的个数。
    # 用 (below + 1) / n 而不是 below / n，否则最强的那只只能拿到 98 而不是 99。
    below = bisect_left(wrets, ticker_wret)
    rating = round(((below + 1) / n) * RS_RATING_MAX)
    return max(RS_RATING_MIN, min(RS_RATING_MAX, int(rating)))


def _log_pool_once(key: str, message: str) -> None:
    """
    池子信息打到 **stdout**，不是 logger.info。

    本项目没有 logging.basicConfig：logger.info 会落到 logging.lastResort，
    而 lastResort 只处理 WARNING 及以上 —— info 直接被丢掉。2026-10-28 实测：
    live run 日志里一条「RS 排名池：9080 只」都没有，这个闸门形同虚设。

    去重的粒度是**一个池子**（日期 + 签名），不是一次调用：
    同一日期的池子先算后命中是同一个池子，打两行纯属噪声；
    而签名变更（30 个日期的缓存同时作废）也只该报一次。
    """
    if key in _POOL_LOG_SEEN:
        return
    _POOL_LOG_SEEN.add(key)
    print(message)


def _pool_log_line(date_str: str, count: int, source: str, pool_sig: str) -> str:
    """池子信息的统一格式：命中路径和重算路径打的是同一句话。"""
    whitelist = "启用" if _load_pool_whitelist() is not None else "不可用"
    return (
        f"[RS 排名池] {count} 只（{date_str}，来源 {source}，"
        f"签名 {pool_sig}，白名单 {whitelist}）"
    )


def _get_universe_wrets(
    as_of: datetime,
    market_data: dict[str, pd.DataFrame],
    cache_path: Path,
    measure_at: datetime | None = None,
) -> list[float]:
    """
    取 as_of 当日全市场加权收益的升序列表，缓存未命中时懒加载全市场。

    成本：未命中约 1.5s（宽表路径）或 ~20s（回退的逐只 pkl），当天只付一次。

    as_of 是缓存的日期键；measure_at 是真正用来算池子的一天（见 compute_rs）。
    """
    date_str = as_of.strftime("%Y-%m-%d")
    cache = _load_rs_daily_cache(cache_path)
    cached = cache.get(date_str, {}).get(UNIVERSE_CACHE_KEY)
    if cached is not None and cached.get("wrets"):
        pool_sig = _current_pool_signature()
        if cached.get("pool_sig") == pool_sig:
            # 命中也要报池子大小：排名池无声地缩水/换血时，RS Rating 整体
            # 位移而没人发现 —— 这正是本项目反复踩的那类静默失真。
            _log_pool_once(
                f"pool:{date_str}:{pool_sig}",
                _pool_log_line(
                    date_str,
                    cached.get("count", len(cached["wrets"])),
                    cached.get("source", "?"),
                    pool_sig,
                ),
            )
            return cached["wrets"]
        # 池子组成变了还沿用旧百分位 = 静默给出错评级，且不报任何错。
        # 一次签名变更会让**所有日期**的缓存条目同时作废，所以按「旧签名 -> 新签名」
        # 去重，只报一次（第一次遇到的日期）；不按日期去重就是 30 行同样的告警。
        old_sig = cached.get("pool_sig", "<无>")
        _log_pool_once(
            f"stale:{old_sig}->{pool_sig}",
            f"[RS 排名池] 缓存已失效（签名 {old_sig} -> {pool_sig}），重算 {date_str}",
        )

    try:
        populated = _populate_universe_cache(
            as_of, market_data, cache_path, measure_at=measure_at
        )
    except Exception:
        return []

    if not populated:
        return []

    cache = _load_rs_daily_cache(cache_path)
    cached = cache.get(date_str, {}).get(UNIVERSE_CACHE_KEY) or {}
    return cached.get("wrets", [])


def _current_pool_signature() -> str:
    """
    排名池的指纹：宽表内容 + 尺寸 + **算池子的规则**。

    没有它，股票池一变（加票、删票、重下数据）而日期没变时，
    `_get_universe_wrets` 会直接命中旧缓存 —— 项目「成功跑完，
    评级却还是按旧池子算的」，而且日志里什么都看不出来。这是本项目
    最容易犯也最难发现的错误，所以指纹必须跟着缓存一起落盘并校验。

    规则也必须进指纹
    --------------
    2026-10-26 加「窗口日历跨度」闸门时，宽表一个字都没动，池子的**成分**
    却变了（440 只票落选）。只签池子不签规则的话，旧缓存照样命中 ——
    改了算法却没改数据，比改了数据没改缓存更难发现。
    """
    wide = load_universe_closes(DEFAULT_RS_CLOSES_PATH)
    if wide is None:
        return "no-wide-table"
    digest = hashlib.sha256()
    digest.update(str(wide.shape).encode())
    # 列名（池子组成）+ 末行现价（池子状态）都参与指纹
    digest.update("\x00".join(map(str, wide.columns)).encode())
    last = wide.iloc[-1]
    digest.update(np.asarray(last.to_numpy(), dtype="float64").tobytes())
    # 排除名单同样参与指纹：改了名单，池子内容就变了，签名必须跟着变。
    # 漏了这一条，旧条目照样命中 —— 报告里的 RS Rating 还是按旧池子算的。
    digest.update("\x00".join(sorted(POOL_EXCLUDED_SYMBOLS)).encode())
    # 全市场名单白名单也一样：名单改了（多收/少收一类证券），池子的**成分**
    # 就变了，签名必须跟着变。否则旧缓存命中，报告里的评级仍按旧池子算，
    # 而且日志里什么都看不出来。
    whitelist = _load_pool_whitelist()
    digest.update(
        ("whitelist:none" if whitelist is None else "\x00".join(sorted(whitelist))).encode()
    )
    # 算池子的规则：公式 + 两道排脏闸门。读的是 data.rs_universe 的**模块属性**，
    # 跟 vectorized_weighted_return 实际用的值同源。
    rules = {
        "weighted": repr(_rsu.WEIGHTED_PARTS),
        "deepest": _rsu.DEEPEST_LOOKBACK,
        "max_price_span": _rsu.MAX_PRICE_SPAN,
        "max_window_span_days": _rsu.MAX_WINDOW_SPAN_DAYS,
        # 硬编码历史断点表也是「算池子的规则」：改了它，池子里那些票的数值就变了。
        "known_breaks": repr(sorted(_rsu.KNOWN_PRICE_BREAKS.items())),
    }
    digest.update(repr(sorted(rules.items())).encode())
    return digest.hexdigest()[:16]


def _populate_universe_cache(
    as_of: datetime,
    market_data: dict[str, pd.DataFrame],
    rs_daily_cache_path: Path,
    measure_at: datetime | None = None,
) -> bool:
    """
    对全市场算加权收益，写入 rs_daily_cache.json[date][__UNIVERSE__]。

    优先走 data/rs_universe.py 的宽表（向量化，约 1.5s）；宽表缺失时
    回退到逐只 pkl 的老路径（约 20s），保证任何环境下都能算出结果。

    存升序浮点列表（供 bisect 查百分位）+ pool_sig（防静默空跑），返回是否成功生成。

    as_of 是缓存的日期键；measure_at 是算池子用的那一天（见 compute_rs）——
    池子和 ticker 必须落在同一天，否则是跨日比较。
    """
    wrets, source = _universe_wrets_from_wide(measure_at or as_of)
    if not wrets:
        wrets, source = _universe_wrets_from_market_data(measure_at or as_of, market_data), "market_data"

    if not wrets:
        return False

    wrets.sort()
    cache = _load_rs_daily_cache(rs_daily_cache_path)
    date_str = as_of.strftime("%Y-%m-%d")
    pool_sig = _current_pool_signature()
    # 池子大小 + 签名必须打出来：排名池一旦无声地缩水或换血，RS Rating 会
    # 整体位移而没人发现 —— 这正是本项目反复踩的那类静默失真。
    _log_pool_once(
        f"pool:{date_str}:{pool_sig}",
        _pool_log_line(date_str, len(wrets), source, pool_sig),
    )
    cache.setdefault(date_str, {})[UNIVERSE_CACHE_KEY] = {
        "wrets": [round(x, 6) for x in wrets],
        "count": len(wrets),
        "pool_sig": pool_sig,
        "source": source,
    }
    _save_rs_daily_cache(cache, rs_daily_cache_path)
    return True


def _pool_wrets_by_symbol(as_of: datetime | None = None) -> pd.Series:
    """
    排名池里每只票的加权收益（index=symbol，算不出的不在里面）。

    两道闸门：
      1. 基准 ETF / 指数（SPY、^VIX…）—— 自指偏差
      2. 全市场名单白名单 —— 权证/优先股/票据/杠杆 ETF 不是股票
    """
    wr = _wide_wrets_by_symbol(as_of)
    if wr is None:
        return pd.Series(dtype=float)
    whitelist = _load_pool_whitelist()
    keep = [
        sym
        for sym in wr.index
        if not _is_pool_excluded(sym) and _pool_symbol_allowed(sym, whitelist)
    ]
    return wr[keep].dropna()


def _universe_wrets_from_wide(as_of: datetime | None = None) -> tuple[list[float], str]:
    """走宽表的快速路径；宽表不存在或算不出东西时返回空，交给调用方回退。"""
    try:
        pool = _pool_wrets_by_symbol(as_of)
    except Exception as exc:
        logger.warning("宽表计算失败，回退逐只 pkl：%s", exc)
        return [], "wide-error"
    if pool.empty:
        return [], "wide-missing"
    values = [float(x) for x in pool.to_numpy()]
    if not values:
        return [], "wide-empty"
    return values, "wide"


def _universe_wrets_from_market_data(
    as_of: datetime, market_data: dict[str, pd.DataFrame]
) -> list[float]:
    """回退路径：逐只 pkl 算，约 20s。语义比宽表宽（不排除落后的票）。"""
    whitelist = _load_pool_whitelist()
    wrets: list[float] = []
    for symbol, df in market_data.items():
        # 跳过指数 / 基准 ETF（^VIX、^GSPC、SPY…），它们不该参与个股排名
        if _is_pool_excluded(symbol):
            continue
        # 名单外的非股票（权证 / 优先股 / 票据）同样不进排名池
        if not _pool_symbol_allowed(symbol, whitelist):
            continue
        r = _compute_weighted_return(df, as_of, ticker=symbol)
        if r is None:
            continue
        wrets.append(r)
    return wrets


# ════════════════════════════════════════════════════════════════
# 公开工具（供批量回算 CLI 调用）
# ════════════════════════════════════════════════════════════════

def compute_rs_for_tickers(
    tickers: list[str],
    as_of_date: str | datetime,
    market_data: dict[str, pd.DataFrame],
    sector_cache_path: str | Path | None = None,
    rs_daily_cache_path: str | Path | None = None,
) -> dict[str, RSResult | None]:
    """
    批量计算一组 ticker 的 RS，返回 {ticker: RSResult} 字典。

    用于 tools/analyze_rs_sector_rank.py。
    """
    results = {}
    for t in tickers:
        results[t] = compute_rs(
            ticker=t,
            as_of_date=as_of_date,
            market_data=market_data,
            sector_cache_path=sector_cache_path,
            rs_daily_cache_path=rs_daily_cache_path,
        )
    return results