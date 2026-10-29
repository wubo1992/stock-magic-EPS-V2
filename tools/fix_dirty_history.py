#!/usr/bin/env python3
"""
重下窗口内含脏数据的行情，校验后**隔离提升**回 data/cache/。

用户红线：全程不删除任何 data/cache/*.pkl
--------------------------------------
替换一律是「改名移动」，不是删除：

    data/cache/XXX.pkl ──mv──> data/cache_quarantine/XXX.pkl   （旧数据留档）
    data/cache_rsfix/XXX.pkl ──mv──> data/cache/XXX.pkl          （新数据上岗）

回滚就一条命令：
    mv data/cache_quarantine/*.pkl data/cache/

为什么要重下
------------
脏数据的根因是数据源不一致：`data/fetcher.py` 的 Alpha Vantage 路径
用 `TIME_SERIES_DAILY` 原始 close，没请求 `ADJUSTED_CLOSE`，**未复权**，
而 Alpaca 路径用 `adjustment="all"`，**已复权**。两者混放在同一个 cache 目录，
于是出现反向拆股痕迹（单日 ±50% 以上）。

本脚本只用 Alpaca `adjustment="all"` 抓，所以新数据必然复权。

用法（分两步，中间有清单）
--------------------------
    # 1) 抓取 + 校验，只写 data/cache_rsfix/，对 data/cache/ 零写入
    uv run python tools/fix_dirty_history.py

    # 2) 看一眼清单再提升
    uv run python tools/fix_dirty_history.py --apply

两步都能续跑：已存在且校验通过的文件会跳过。
"""
from __future__ import annotations

import argparse
import json
import pickle
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.rs import (  # noqa: E402
    DIRTY_RETURN_THRESHOLD,
    DIRTY_WINDOW_WEIGHTED,
    MIN_HISTORY_DAYS,
    _deduplicate_bars,
    _price_span_absurd,
    _slice_to_date,
)
from data.rs_universe import MAX_WINDOW_SPAN_DAYS  # noqa: E402

CACHE_DIR = ROOT / "data" / "cache"
FIX_DIR = ROOT / "data" / "cache_rsfix"      # 影子目录：抓取产物
QUARANTINE_DIR = ROOT / "data" / "cache_quarantine"  # 旧数据留档
MANIFEST = ROOT / "data" / "rsfix_promote.json"

DIRTY_WINDOW = DIRTY_WINDOW_WEIGHTED  # 254
MIN_BARS = MIN_HISTORY_DAYS  # 253
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

# 最后一根 bar 最多落后 as_of 几个自然日。
# as_of=2026-10-22 时，最新可用交易日是 2026-10-21，落后 1 天是正常的。
# 落后更多 = 停牌/退市边缘，拿旧价当现价进排名池会系统性美化它们的百分位。
MAX_STALE_DAYS = 1

# 同一交易日两个数据源的收盘价差多少算「两套价格基准混用」。
#
# 实测分布（10,695 只行情 pkl）：中位 0.5%、p75 2.2%、p90 6.8%、
# 312 只 >10%、30 只 >50%（反向拆股级别）。
# 2% 是「分红复权 vs 拆股复权」这种量级差别的下沿，再低就是正常的数据噪声。
DUAL_SOURCE_CLOSE_GAP = 0.02

# ── 窗口日历跨度：RS 算的是「12 个月相对强度」 ──────────────────
#
# 阈值住在 data/rs_universe.py（上面 import 进来的 MAX_WINDOW_SPAN_DAYS）——
# RS 排名池用它当闸门，这里用它挑「值得重下的文件」。两处必须是同一个数，
# 否则会出现「工具认为要修、管道却放行」的缝隙。
#
# 背景：「253 根 bar」在正常情况下摊在 366 个自然日里（一年 252 个交易日）。
# 数据中间少一大截时，这 253 根 bar 会摊在两年、三年甚至十年里 ——
# 那算出来的根本不是 12 个月的相对强度，而是「上一次有数据到现在」。
#
# 实测（2026-10-26，10,695 只行情 pkl）：窗口日历跨度的分布是
#   p50 = 366 天   p90 = 368 天   p95 = 389 天   p99 = 1,407 天
# 440 只超过 400 天。取 400：一方面离 p95 只有 11 天余量（正常票几乎不受影响），
# 一方面把「半年多的数据空洞」和「正常的日历抖动」分开。

# 比对重下数据与现有数据时，看最近多少个自然日。
# 504 个自然日 ≈ 2 年的交易日 —— 足够覆盖 RS 的 253 根窗口并留出余量。
RECENT_WINDOW_DAYS = 504

# ── 重下的历史深度 ─────────────────────────────────────────────────
#
# Alpaca SIP 日线的**数据底线**是 2016-01-04（实测：AAPL 2719 根、BRKL 2508 根，
# 都从这一天开始，再往前什么都取不到）。
#
# 请求起点必须比这个底线**更早**。曾经写成 `as_of - 3652 天`（正好 10 年），
# 于是 2026-10-27 那轮重下的请求起点是 2016-10-23 —— 比底线晚了一整年。
# 现有缓存（Yahoo 合并）能追到 2016-01-04，重下结果追不到：
# 84 只票各少 204 根 bar，一年历史凭空消失，而**不报任何错**。
ALPACA_SIP_FLOOR = pd.Timestamp("2016-01-04", tz="UTC")

# 往前多要 20 年。多出来的部分 Alpaca 自己会裁掉（它只给到数据底线），
# 但保证了「现有文件有多少年历史，重下结果就也有多少年」。
FETCH_HISTORY_DAYS = 7305


def _fetch_start(as_of) -> datetime:
    """重下的起点：比 Alpaca 的数据底线更早，保证不截短现有历史。"""
    return as_of - timedelta(days=FETCH_HISTORY_DAYS)



# ════════════════════════════════════════════════════════════════
# 校验
# ════════════════════════════════════════════════════════════════


def tail_jump_count(df) -> int:
    """
    数一数尾部 254 根 bar 里有几处 ±50% 跳空。

    **只统计，不拒绝。** 用来回答「这批提升上去的文件里有几处跳空」，
    让清单可审计 —— 看得见不等于拦得住，见 validate_frame 的说明。
    """
    if df is None or not isinstance(df, pd.DataFrame) or "close" not in df.columns:
        return 0
    closes = pd.to_numeric(df["close"], errors="coerce")
    rets = closes.iloc[-DIRTY_WINDOW:].pct_change().dropna()
    return int((rets.abs() > DIRTY_RETURN_THRESHOLD).sum())


def validate_frame(df, as_of) -> tuple[bool, str]:
    """
    判断一份行情能不能替换现有数据。返回 (是否合格, 原因)。

    五道闸门，少一道都可能把「只是换了个脏数据源」的文件提升上去：
      - 列齐全（缺 volume 的文件下游会崩）
      - bar 数够 253（否则算不出 R252）
      - 最后一根 bar 不滞后（不能用停牌前的旧价当现价）
      - 窗口内价格量级不荒谬（跟 RS 排名池同一把尺，见下方说明）

    ⚠️ 这里**没有**「尾部无 ±50% 跳空」这道闸门，而且是有意去掉的。
    本脚本只用 Alpaca `adjustment="all"` 抓，数据已复权；此时窗口里的
    ±50% 不是没复权好，而是真实行情。实测反证：AAOI / AARD / AGIO 的
    raw 与 all 逐 bar 比对零差异（历史上根本没拆过股），跳空当日成交量比
    8.5x~1091x、振幅 9%~68%，是消息面推动的真涨跌幅。

    保留这道闸门时，894 只重下候选有 548 只被毙，其中 539 只是普通股
    （多为生物科技微盘）—— 拿干净数据当脏数据扔掉，比留着旧数据更糟。
    跳空改为**只统计**（tail_jump_count），写进清单的 jumps 字段，
    提升后仍可审计。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False, "空数据"

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return False, f"缺列 {missing}"

    closes = pd.to_numeric(df["close"], errors="coerce")
    if closes.isna().all():
        return False, "close 全是 NaN"

    if len(closes) < MIN_BARS:
        return False, f"只有 {len(closes)} 根 bar，少于 {MIN_BARS}"

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    last_day = idx.max().normalize()

    as_of_ts = pd.Timestamp(as_of)
    as_of_ts = (
        as_of_ts.tz_localize("UTC") if as_of_ts.tz is None else as_of_ts.tz_convert("UTC")
    )
    lag = (as_of_ts.normalize() - last_day).days
    if lag > MAX_STALE_DAYS:
        return False, f"最后一根 bar 滞后 {lag} 天（{last_day.date()}）"

    # 价格量级：跟 RS 排名池用**同一把尺**（`data/rs.py::_price_span_absurd`）。
    #
    # 没有这道闸门的话，一份量级荒谬的数据照样被标成 ok、照样被提升上去，
    # 而 RS 管道转头又把它拒掉 —— 清单写着「ok」，池子里却没有它。
    # 实测（2026-10-26）：BDAI / CDT / RUBI 等 35 只，Alpaca 的
    # adjustment="all" 给出的价格范围是 195,508 倍（复权因子本身是坏的），
    # RS 拒收，清单却说合格。
    #
    # 这里用的是**去重后**的收盘价序列，和 RS 管道同序同源
    # （`_slice_to_date` → `_deduplicate_bars`）。
    dedup = _deduplicate_bars(_slice_to_date(df, as_of))
    if len(dedup) >= MIN_BARS and _price_span_absurd(dedup):
        win = dedup.iloc[-MIN_BARS:]
        return False, (
            f"窗口内价格量级荒谬（max/min = {win.max() / win.min():,.0f} 倍）"
        )

    return True, "ok"


# ════════════════════════════════════════════════════════════════
# 找脏票
# ════════════════════════════════════════════════════════════════

_NON_PRICE_PREFIXES = ("eps_", "polygon_", "finnhub_metric_", "fmp_financial_")


def dual_source_conflicts(
    df, as_of, gap: float = DUAL_SOURCE_CLOSE_GAP
) -> list[pd.Timestamp]:
    """
    找出「同一交易日有两条 bar、且收盘价差超过 gap」的日期（窗口内）。

    为什么这是脏数据的**签名**
    --------------------------
    同一个交易日出现两条 bar，只可能是两个数据源各写了一遍：
    Yahoo 写 00:00 UTC，Alpaca 写 04:00 UTC。两条都来自 Alpaca 的话
    收盘价必然相同（除非中间隔了拆股）。

    收盘价差了 >gap = 两套价格基准混在一起了：
      - Yahoo 的 adjclose 是**分红复权**后的
      - Alpaca 的 adjustment="all" 是**拆股复权**、不含分红
    差 0.5% 是分红级别，差 100 倍是反向拆股级别。

    这种文件算出来的加权收益是拿两套基准做比值 —— 名义上「没报错」，
    实际上 R63/R126/R189/R252 全部带偏。

    而 ±50% 跳空那道闸门**看不见**它：TWG 的价格量级只有 157 倍，
    稳稳地过了闸门，可它的收盘价在同一天里差了 104 倍。

    只看窗口（最后 DIRTY_WINDOW 个交易日）—— 窗口外的旧冲突不影响
    今天这只票的口径，和 RS 管道的窗口保持一致。
    """
    if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
        return []
    if not isinstance(df.index, pd.DatetimeIndex):
        return []

    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    closes = pd.to_numeric(
        pd.Series(df["close"].to_numpy(), index=idx.normalize()), errors="coerce"
    )

    cutoff = pd.Timestamp(as_of)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tz is None else cutoff.tz_convert("UTC")
    closes = closes[closes.index <= cutoff.normalize()]
    if closes.empty:
        return []

    # 只看窗口内的交易日
    dates = closes.index.unique()
    if len(dates) > DIRTY_WINDOW:
        closes = closes[closes.index.isin(dates[-DIRTY_WINDOW:])]

    grouped = closes.groupby(level=0).agg(["min", "max", "count"])
    grouped = grouped[grouped["count"] > 1]
    if grouped.empty:
        return []
    lo = grouped["min"].replace(0.0, np.nan)
    rel = (grouped["max"] - grouped["min"]) / lo
    bad = rel[rel > gap]
    return list(bad.index)


def find_dirty_symbols(cache_dir: Path, as_of) -> list[str]:
    """
    扫 cache 目录，返回**RS 排名池真正会拒掉**的 symbol（升序）。

    判据必须和 RS 管道逐字一致
    --------------------------
    RS 排名池拒绝一只票的理由只有两个（`data/rs.py::_compute_weighted_return`）：

      1. 窗口内价格量级荒谬：max(close)/min(close) > MAX_PRICE_SPAN（1000 倍）
      2. 同日双源、收盘价差 >2%（两套价格基准混用，见 `dual_source_conflicts`）

    这里的清单如果和管道口径不一致，会同时坏在两头：

      - **多算**：真实跳空（AAOI/AARD/AGIO 的 raw 与 all 逐 bar 比对零差异）
        被当成脏数据，而重下根本消除不了它 —— 每轮扫描都把同一批票
        重新列进清单、重新下载、重新替换，排名池却从头到尾没变。
      - **漏算**：管道真正算不出 RS 的票没人管，池子里长期躺着一批
        「RS 恒为空」的票，白占分母。

    实测（2026-10-23，10,695 只行情 pkl）：
      旧口径（单日 ±50% 跳空）  → 1,839 只，其中 409 只是真实跳空
      新口径（本函数）           → 1,439 只（1,430 双源 + 43 量级荒谬）
    """
    dirty: list[str] = []
    for path in sorted(cache_dir.glob("*.pkl")):
        sym = path.stem
        if sym.startswith(_NON_PRICE_PREFIXES):
            continue
        try:
            df = pd.read_pickle(path)
        except Exception:
            continue
        if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
            continue
        if dual_source_conflicts(df, as_of):
            dirty.append(sym)
            continue
        # 与 data/rs.py::_compute_weighted_return 完全同序：先切到 as_of，再按日去重
        closes = _deduplicate_bars(_slice_to_date(df, as_of))
        if len(closes) < MIN_BARS:
            continue
        if _price_span_absurd(closes):
            dirty.append(sym)
    return dirty


# ════════════════════════════════════════════════════════════════
# 窗口被空洞拉长
# ════════════════════════════════════════════════════════════════


def window_calendar_span(df, as_of, window: int = MIN_BARS) -> int:
    """
    最后 window 根 bar 横跨了多少个自然日。历史不足 window 根时返回 0。

    「253 根 bar」正常摊在 366 天里。数据中间少一大截时（缓存里的空洞、
    长期停牌、源数据缺段），这 253 根 bar 会摊在两年甚至十年里 ——
    RS 算出来的就不是 12 个月的相对强度了。
    """
    if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
        return 0
    closes = _deduplicate_bars(_slice_to_date(df, as_of))
    if len(closes) < window:
        return 0
    idx = pd.DatetimeIndex(closes.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return int((idx[-1] - idx[-window]).days)


def find_stretched_symbols(
    cache_dir: Path, as_of, max_span_days: int = MAX_WINDOW_SPAN_DAYS
) -> list[str]:
    """
    扫 cache 目录，返回「RS 窗口被空洞拉长」的 symbol（升序）。

    为什么单列一个探测器
    ----------------------
    排脏判据（价格量级 > 1000 倍）看不见这一类坏数据：
    AKTS 的窗口 max/min = 866 < 1000，稳稳通过；但它中间空了 13 个月，
    收盘价在空洞处被当成**一天**的涨跌读出来（+60,115%），
    wret = 41.61 —— 全市场第一，RS 99。

    价格量级这一关拦不住空洞，**日历跨度**才拦得住。
    """
    stretched: list[str] = []
    for path in sorted(cache_dir.glob("*.pkl")):
        sym = path.stem
        if sym.startswith(_NON_PRICE_PREFIXES):
            continue
        try:
            df = pd.read_pickle(path)
        except Exception:
            continue
        if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
            continue
        if window_calendar_span(df, as_of) > max_span_days:
            stretched.append(sym)
    return stretched


def recent_bar_count(df, as_of, days: int = RECENT_WINDOW_DAYS) -> int:
    """
    最近 days 个自然日里有几根 bar（去重、切片后）。

    用来比对「重下数据 vs 现有数据」。为什么不用总 bar 数：Alpaca 的历史
    深度经常不如 Yahoo，但只要**最近两年**是全的，RS 的 253 根窗口就是
    完整的 —— 总 bar 数少不代表数据差。
    """
    if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
        return 0
    closes = _deduplicate_bars(_slice_to_date(df, as_of))
    if closes.empty:
        return 0
    ts = pd.Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    cutoff = ts.normalize() - pd.Timedelta(days=days)
    idx = pd.DatetimeIndex(closes.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return int((idx >= cutoff).sum())



# ════════════════════════════════════════════════════════════════
# 批量重试：二分拆批
# ════════════════════════════════════════════════════════════════


def fetch_resilient(
    fetch,
    symbols: list[str],
    max_batch: int = 100,
    min_split: int = 1,
) -> dict:
    """
    调 `fetch(symbols) -> dict` 抓一批，失败就**二分拆批重试**。

    为什么必须拆：`data/fetcher.py::_alpaca_fetch_batch` 累计剔除 10 个无效
    代码之后就整批 `return {}`。上万个代码里混着 warrant/unit/preferred，
    一旦触发上限，接下来每一批都是静悄悄的空 —— 项目「成功跑完、什么错都没报」，
    但几千只票的数据根本没更新。

    单只票失败直接放弃（min_split=1 时不再递归），保证一定收敛。
    """
    out: dict = {}

    def _run(batch: list[str], depth: int = 0) -> None:
        if not batch:
            return
        try:
            got = fetch(batch)
        except Exception as exc:  # 网络抖动也走拆批，别让整批陪葬
            got = {}
            if depth == 0:
                print(f"  [重试] 批次失败：{exc}")
        if got:
            out.update(got)
            # 取到一部分也要把没取到的补齐，否则静默丢票
            missing = [s for s in batch if s not in got]
            if not missing:
                return
            batch = missing
            if len(batch) <= min_split:
                return
        if len(batch) <= min_split:
            return
        mid = len(batch) // 2
        _run(batch[:mid], depth + 1)
        _run(batch[mid:], depth + 1)

    for i in range(0, len(symbols), max_batch):
        _run(symbols[i : i + max_batch])
    return out


# ════════════════════════════════════════════════════════════════
# 抓取（Alpaca）
# ════════════════════════════════════════════════════════════════


def _make_alpaca_fetch(client, start: datetime, feed_name: str = "sip"):
    """
    造一个 `fetch(symbols) -> dict`。

    feed 默认 **sip** 而不是 iex：IEX 的日线在 Alpaca 侧被硬截在 2020-07-27，
    无论 start 给到哪一年都一样（实测 HON 从 2010 起请求也只回 1568 根）。
    而本项目旧数据去重后到 2016-04-07 —— 用 IEX 重下会白丢约 4.3 年历史，
    回测覆盖无声缩水。SIP 能回到 2016-01-04，比旧数据还深一点。
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        from alpaca.data.enums import DataFeed

        feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    except (ImportError, AttributeError):
        feed = feed_name

    def fetch(symbols: list[str]) -> dict:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start.replace(tzinfo=None),
            adjustment="all",  # ← 复权。脏数据的根因就是这一条
            feed=feed,
        )
        bars = client.get_stock_bars(req)
        raw = bars.df
        if raw is None or raw.empty:
            return {}
        result = {}
        for sym in raw.index.get_level_values("symbol").unique():
            df = raw.xs(sym, level="symbol").copy()
            keep = [c for c in REQUIRED_COLUMNS if c in df.columns]
            df = df[keep]
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            # Alpaca 按交易所时区给日线时间戳（IEX 04:00 UTC / SIP 05:00 UTC），
            # 而本项目 pkl 的约定是 UTC 零点。不归一的话 `_slice_to_date` 的
            # `index <= as_of`（as_of 在 00:00）会把当天那根整根丢掉。
            df.index = df.index.normalize()
            df = df.dropna()
            df = df[df["volume"] > 0]
            if not df.empty:
                result[sym] = df
        return result

    return fetch


def _alpaca_symbol(symbol: str) -> str:
    """
    原样返回 —— Alpaca 的分级股代码用的就是**点**（2026-10-23 实测）。

    `BRK.A` 返回 2,528 根 bar，`BRK-A` 报 `invalid symbol`。
    早先这里写的是「点换连字符」，方向反了。2026-10-23 那轮 894 只脏票里
    恰好没有带点代码，所以没造成实际损失，但函数还在，锁死它。
    """
    return symbol


def _existing_recent_bars(sym: str, as_of) -> int:
    """现有缓存里这只票最近 RECENT_WINDOW_DAYS 天的 bar 数（读不到算 0）。"""
    path = CACHE_DIR / f"{sym}.pkl"
    if not path.exists():
        return 0
    try:
        df = pd.read_pickle(path)
    except Exception:
        return 0
    return recent_bar_count(df, as_of)


def _existing_total_bars(sym: str, as_of) -> int:
    """现有缓存里这只票总共有多少个**交易日**有数据（按日去重后）。"""
    path = CACHE_DIR / f"{sym}.pkl"
    if not path.exists():
        return 0
    try:
        df = pd.read_pickle(path)
    except Exception:
        return 0
    return int(len(_deduplicate_bars(_slice_to_date(df, as_of))))



# ════════════════════════════════════════════════════════════════
# 阶段 1：抓取 + 校验 + 写清单
# ════════════════════════════════════════════════════════════════


def collect(
    symbols: list[str],
    as_of,
    batch: int = 100,
    sleep: float = 0.5,
    feed_name: str = "sip",
) -> dict:
    import os

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    # 历史 K 线走 StockHistoricalDataClient，不是 TradingClient
    # （TradingClient 没有 get_stock_bars，调用会 AttributeError）
    from alpaca.data.historical import StockHistoricalDataClient

    client = StockHistoricalDataClient(
        os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    )

    # 抓得比 Alpaca 的数据底线更早（见 _fetch_start），保证不截短现有历史
    start = _fetch_start(as_of)
    fetch = _make_alpaca_fetch(client, start, feed_name=feed_name)

    FIX_DIR.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    t0 = time.time()

    # 文件名与 Alpaca 代码现在是同一个形态（都带点，见 _alpaca_symbol），无需映射
    #
    # 已经在影子目录里的票（上一轮跑剩的）**必须重新进清单**。
    # 原来它们被 `todo` 直接过滤掉，`entries` 里就没有它们 ——
    # 而 `promote()` 只认清单，于是这些票永远不会被提升，脏数据原样留在
    # cache 里，而清单看上去是完整的。实测（2026-10-23 第二轮）：
    # 1,439 只里有 4 只正好撞上上一轮的影子文件，被静默漏掉。
    todo: list[str] = []
    for sym in symbols:
        shadow = FIX_DIR / f"{sym}.pkl"
        if not shadow.exists():
            todo.append(sym)
            continue
        try:
            df = pd.read_pickle(shadow)
        except Exception:
            todo.append(sym)  # 读不出来就重下一份
            continue
        ok, reason = validate_frame(df, as_of)
        if not ok:
            todo.append(sym)  # 校验不过（比如是别的 as_of 下抓的）就重下
            continue
        entries[sym] = {
            "status": "ok",
            "bars": int(len(df)),
            "last_bar": str(pd.Timestamp(df.index.max()).date()),
            "jumps": tail_jump_count(df),
        }

    print(f"[抓取] {len(symbols)} 只，其中 {len(todo)} 只需要下载")
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        try:
            got = fetch_resilient(fetch, [_alpaca_symbol(s) for s in chunk], max_batch=batch)
        except Exception as exc:
            print(f"  [WARN] 批次 {i} 失败：{exc}")
            got = {}

        for sym in chunk:
            key = _alpaca_symbol(sym)
            df = got.get(key)
            target = FIX_DIR / f"{sym}.pkl"
            if df is None:
                entries[sym] = {"status": "fetch_failed", "reason": "Alpaca 无数据"}
                continue
            ok, reason = validate_frame(df, as_of)
            if not ok:
                entries[sym] = {"status": "rejected", "reason": reason}
                continue

            # 「不许比现有数据更差」。
            #
            # Alpaca 的 `adjustment="all"` 对个别代码给出**更短**的历史
            # （实测 35 只：195,508 倍的荒谬价格范围，历史也短一截）。
            # 拿它替换现有数据 = 用差数据覆盖好数据 —— 旧文件虽然会留档，
            # 但排名池会白白少掉一只票，而且没有任何报错。
            old_recent = _existing_recent_bars(sym, as_of)
            new_recent = recent_bar_count(df, as_of)
            if old_recent and new_recent < old_recent:
                entries[sym] = {
                    "status": "rejected",
                    "reason": (
                        f"最近 {RECENT_WINDOW_DAYS} 天只有 {new_recent} 根 bar，"
                        f"比现有数据少 {old_recent - new_recent} 根"
                    ),
                }
                continue

            # 总历史也不能更短。
            #
            # 「最近两年」那道闸门管不到「早年很全、近两年稀疏」的形状：
            # 现有文件 600 根（近两年 300 根），重下结果 350 根（近两年 350 根）——
            # 近两年更满，总数却少了一半。
            #
            # 实测 FUSE（2026-10-27）：现有 717 根（2022-03 起），
            # Alpaca 只有 253 根（2025-10 起）。替换 = 回测历史少三年半，
            # 而 RS 那边只关心最近 253 根，**一个错都不会报**。
            old_total = _existing_total_bars(sym, as_of)
            new_total = int(len(_deduplicate_bars(_slice_to_date(df, as_of))))
            if old_total and new_total < old_total:
                entries[sym] = {
                    "status": "rejected",
                    "reason": (
                        f"总共只有 {new_total} 根 bar，"
                        f"比现有数据的 {old_total} 根少 {old_total - new_total} 根"
                    ),
                }
                continue

            with open(target, "wb") as f:
                pickle.dump(df, f)
            entries[sym] = {
                "status": "ok",
                "bars": int(len(df)),
                "last_bar": str(pd.Timestamp(df.index.max()).date()),
                # 跳空只记录不拦截（见 validate_frame）。写进清单是为了提升后仍可审计：
                # 一眼看出哪些上岗文件带真实跳空，而不是靠回头翻日志。
                "jumps": tail_jump_count(df),
            }

        done += len(chunk)
        print(
            f"  [{done}/{len(todo)}] {time.time() - t0:.0f}s "
            f"合格 {sum(1 for v in entries.values() if v['status'] == 'ok')}"
        )
        time.sleep(sleep)

    _write_manifest(entries, as_of)
    return entries


def _write_manifest(entries: dict, as_of) -> None:
    from collections import Counter

    counts = Counter(v["status"] for v in entries.values())
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": str(as_of),
        "counts": dict(counts),
        "entries": entries,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    print(f"\n[清单] {MANIFEST}")
    for status, n in counts.most_common():
        print(f"    {status:14s} {n:5d}")
    rejected = {k: v["reason"] for k, v in entries.items() if v["status"] == "rejected"}
    if rejected:
        print("\n  校验不通过样例：")
        for sym, reason in list(rejected.items())[:10]:
            print(f"    {sym:8s} {reason}")


# ════════════════════════════════════════════════════════════════
# 阶段 2：隔离提升（只做改名移动，绝不删除）
# ════════════════════════════════════════════════════════════════


def _stash_existing_backup(path: Path, prev_dir: Path) -> Path:
    """
    留档目录里已有同名文件时，先把它挪到 prev/ —— 绝不能覆盖掉一代历史。

    「绝不删除任何 data/cache/*.pkl」这条红线也管着留档目录：那里的每一份
    都是旧数据，覆盖它等于毁掉一代历史。

    这不是假想场景：2026-10-23 第二轮重下时，第一轮留档的 781 份里有
    746 份会撞名。
    """
    prev_dir.mkdir(parents=True, exist_ok=True)
    target = prev_dir / path.name
    n = 1
    while target.exists():
        target = prev_dir / f"{path.stem}.{n}.pkl"
        n += 1
    shutil.move(str(path), str(target))
    return target


def promote(as_of) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = manifest["entries"]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    promoted = 0
    for sym, info in sorted(entries.items()):
        if info.get("status") != "ok":
            continue
        new_path = FIX_DIR / f"{sym}.pkl"
        if not new_path.exists():
            print(f"  [WARN] {sym} 影子文件缺失，跳过")
            continue

        # 提升前再验一次：影子文件可能是在别的 as_of 下校验的
        ok, reason = validate_frame(pd.read_pickle(new_path), as_of)
        if not ok:
            print(f"  [WARN] {sym} 复检不通过（{reason}），跳过")
            continue

        old_path = CACHE_DIR / f"{sym}.pkl"
        if old_path.exists():
            backup = QUARANTINE_DIR / f"{sym}.pkl"
            if backup.exists():
                _stash_existing_backup(backup, QUARANTINE_DIR / "prev")
            shutil.move(str(old_path), str(backup))
        shutil.move(str(new_path), str(old_path))
        promoted += 1

    print(f"\n[提升] {promoted} 只已上岗")
    print(f"  旧数据留档在 {QUARANTINE_DIR}（{len(list(QUARANTINE_DIR.glob('*.pkl')))} 份）")
    print(f"  回滚命令：mv {QUARANTINE_DIR}/*.pkl {CACHE_DIR}/")
    return promoted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="执行提升（默认只抓取+校验）")
    ap.add_argument(
        "--as-of",
        default="2026-10-22",
        help="信号日期 YYYY-MM-DD（决定 staleness 与脏窗口的边界）",
    )
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.5, help="批次间隔秒数")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--rescan", action="store_true", help="重新扫 cache 找脏票")
    ap.add_argument(
        "--scan",
        choices=("dirty", "stretched", "both"),
        default="dirty",
        help=(
            "扫哪一种票：dirty=RS 管道会拒收的；"
            "stretched=RS 窗口被空洞拉长的；both=并集"
        ),
    )
    ap.add_argument(
        "--todo-file",
        default="",
        help="待办清单文件（默认 data/rsfix_todo.json）",
    )
    ap.add_argument(
        "--manifest",
        default="",
        help="清单文件（默认 data/rsfix_promote.json）",
    )
    ap.add_argument(
        "--feed",
        choices=("sip", "iex"),
        default="sip",
        help="Alpaca 数据源。默认 sip —— IEX 日线被硬截在 2020-07-27，会白丢约 4.3 年历史",
    )
    args = ap.parse_args()

    global MANIFEST
    if args.manifest:
        MANIFEST = Path(args.manifest)

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.apply:
        promote(as_of)
        return 0

    symbols_path = Path(args.todo_file) if args.todo_file else ROOT / "data" / "rsfix_todo.json"
    if args.rescan or not symbols_path.exists():
        print(f"[扫描] 找 {args.scan} 的票…")
        symbols = []
        if args.scan in ("dirty", "both"):
            symbols += find_dirty_symbols(CACHE_DIR, as_of)
        if args.scan in ("stretched", "both"):
            symbols += find_stretched_symbols(CACHE_DIR, as_of)
        symbols = sorted(set(symbols))
        symbols_path.write_text(json.dumps(symbols, indent=1), encoding="utf-8")
        print(f"  找到 {len(symbols)} 只 -> {symbols_path}")
    else:
        symbols = json.loads(symbols_path.read_text(encoding="utf-8"))
        print(f"[续跑] 从 {symbols_path} 读入 {len(symbols)} 只")

    if args.limit:
        symbols = symbols[: args.limit]

    collect(symbols, as_of, batch=args.batch, sleep=args.sleep, feed_name=args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())