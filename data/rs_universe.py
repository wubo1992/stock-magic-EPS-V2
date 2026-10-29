"""
RS 排名池用的「全市场收盘价宽表」。

为什么不直接遍历 pkl
--------------------
`_compute_weighted_return` 每只票 ~2.4ms，6,300 只 ≈ 15s。排名池一旦扩到全市场
（~12,000 只，含 ETF），逐只读 pkl + 计算会变成几十秒，而且每天 live run
都要付一次（crossback 还会按信号日期各付一次）。

所以把「收盘价」抽成一张宽表：index=交易日，columns=symbol。
读 pkl 的成本从「每次 live run」变成「一次性 + 增量」，
加权收益改成 pandas 向量化（实测 1,500 只 0.020s vs 循环 0.884s，44×）。

不 ffill（关键）
----------------
缺失一律留 NaN。最新 254 行只要有一格是 NaN，这只票就落选。
如果 ffill，一只三周没更新的票会被静默地拿三周前的价格当现价入池，
污染整个百分位排名 —— 比让它落选糟糕得多。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 加权收益的权重与回看期，与 data/rs.py 的 WEIGHT_* / LOOKBACK_* 保持一致
WEIGHTED_PARTS: tuple[tuple[float, int], ...] = ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252))
DEEPEST_LOOKBACK = 252

# 脏数据窗口 = 最深回看期 + 2。
# +1 是 R252 基准价所在的 iloc[-253]；再 +1 是因为 pct_change() 的首元素是 NaN，
# 少一根就看不见 iloc[-254] -> iloc[-253] 这一跳（跳空会污染 R252 基准价）。
# 见 DIRTY_WINDOW_WEIGHTED 的注释。
DIRTY_WINDOW = DEEPEST_LOOKBACK + 2  # 254

# 宽表保留最近多少个**日历行**（全体 symbol 日期的并集）。
#
# 只留 254 行是不够的：低流动性票一年可能只成交一百来天，在 400 行
# （约 18.5 个月）里攒不满 253 根 bar，会被当成「历史不足」落选 ——
# 可它们在标量路径下有几千根 bar，是能算的。实测 106 只栽在这上面
# （例：ACU 每年约 146 根 bar，400 行里只有 235 根）。
#
# 实测「攒满 253 根」所需日历天数：p50 = 367 天，p90 = 923 天，p99 = 11,912 天。
# 取 700 行（约 2.4 年）覆盖到 p90 附近。再往上收益急剧衰减 ——
# p99 那种票要 32 年才攒得满 253 根 bar，本来就无从排名。
# 代价：6,300 只 × 700 × 8B ≈ 35 MB；扩到全市场 12,000 只约 67 MB。
TAIL_ROWS = 700

DEFAULT_CLOSES_PATH = Path("data/rs_universe_closes.pkl")

# data/cache 里混着 EPS / Polygon 的 dict 型 pkl，靠前缀区分
_NON_PRICE_PREFIXES = ("eps_", "polygon_")

DIRTY_RETURN_THRESHOLD = 0.50

# ── 参考日：全市场都有数据的那一天 ──────────────────────────────
# 「全市场覆盖」的判定门槛（相对典型覆盖量）。
#
# 背景：live run 每轮只刷新 live pool（6,317 只里约 5,155 只有盘），
# 剩下 4,377 只全市场票停在上一交易日。于是宽表的**末行只覆盖约一半**。
# 早期版本直接拿末行当「最新交易日」，排名池从 9,776 静默腰斩到 4,885 ——
# 不报任何错，只是 RS Rating 的分位悄悄换了个小得多的池子。
#
# 参考日因此必须自己去找「全市场都有数据」的那一行：
# 覆盖率 ≥ 典型覆盖率 × 本门槛 的**最后一行**。
# 典型覆盖率取「看起来是完整交易日」的那些行的中位数，
# 这样少数长期停牌的票不会把门槛压低。
MARKET_COVERAGE_RATIO = 0.90

# 一行至少要有典型覆盖量的这个比例，才算「像个完整交易日」，
# 用来在估典型覆盖率时排除掉节假日行和半刷新行。
DENSE_ROW_RATIO = 0.5

# ── 排名池的排脏门槛：价格量级合理性 ──────────────────────────────
# 曾经用「单日 ±50% 跳空」当门槛，2026-10-23 废掉。原因见下。
#
# 排名池的数据来自 Alpaca `adjustment="all"`，已复权。复权之后窗口里仍有
# ±50% 的，**不是**「没复权好」，而是真实行情 —— 涨跌幅根本分不出
# 「RCEL 2026-08-07 涨 63.6% 的好消息」和「没复权的反向拆股痕迹」。
# 实测（逐 bar 比对 raw 与 all，两侧都有这次跳空）：
#   RCEL 4.75 → 7.77，当日量比 43x、振幅 34.7%（纳斯达克 100 成分股）
#   AEMD +372%，量比 199x
# 旧门槛把 603 只一并排除，其中 RCEL 的 wret=+253% 本该排进前 1%。
#
# 改用**与涨跌幅无关**的量：252 根窗口内 max(close) / min(close)。
# 真实股票再疯也到不了这个量级，而坏数据一眼就被抓住：
#   SXTC 101,760,000 → 0.048（跨 20 亿倍）/ HUBC 256 万倍 / OFAL 139 倍
#
# 阈值 1000 的来历（2026-10-22 实测 5,029 只合格者的 max/min 比值）：
#   p50 = 1.99    p90 = 8.11    p99 = 410    p99.9 = 41,093    max = 2,562,500
# 真实行情票最高 AAOI 11.45、AEMD 20.1、RCEL 4.68 —— 离 1000 差两个数量级；
# 取 1000 正好卡在 p99 与损坏尾部之间，只误伤 28 只。
MAX_PRICE_SPAN = 1000.0

# ── 排名池的第二道闸门：窗口的日历跨度 ────────────────────────────
#
# 「253 根 bar」在正常情况下摊在 366 个自然日里（一年 252 个交易日）。
# 数据中间少一大截时，这 253 根 bar 会摊在两年、三年甚至十年里 ——
# 那算出来的根本不是「12 个月的相对强度」，而是「上一次有数据到现在」。
#
# 空洞的来源：Alpaca 返回的日线里混着 `volume == 0` 的占位行（停牌日，
# 价格是上一根的平推），我们 pipeline 里的 `df[df["volume"] > 0]` 过滤
# 把它们剔掉，于是 253 根 bar 少了半年（实测 GOGL 断 311 天）。
#
# 实测（2026-10-26，10,695 只行情 pkl）窗口日历跨度的分布：
#   weighted 253 根: p50 = 366 天  p90 = 368  p95 = 389  p99 = 1,407
#   10w      51 根: p50 = 71 天   p90 = 71   p95 = 72   p99 = 126
#   10d      11 根: p50 = 14 天   p90 = 14   p95 = 14   p99 = 21
#
# 取 400：一方面离 p95 只有 11 天余量（正常票几乎不受影响），
# 一方面把「半年多的数据空洞」和「正常的日历抖动」分开。
# 超过 400 天的有 440 只 —— 它们的 wret p99 = 40.7、max = 130.7（GNMX），
# 垃圾数值全都坐在 RS 排行榜的头名上（AKTS 曾是全市场第 1，wret 41.6）。
#
# 10w / 10d 的窗口更短，正常摊开的自然日也更少，所以各自按比例收紧：
# 51 根 → 100 天（正常 50 天，2 倍余量）、11 根 → 25 天（正常 10 天）。
MAX_WINDOW_SPAN_DAYS = 400
MAX_WINDOW_SPAN_DAYS_10W = 100
MAX_WINDOW_SPAN_DAYS_10D = 25

# ── 已知「历史断点」：证券身份变了，断点前后的价格不可比 ─────────────
#
# 格式：{ticker: "YYYY-MM-DD"}
#
# 为什么不是「拆股比例表」
# ------------------------
# Alpaca 的 adjustment="all" 对绝大多数反向拆股是有效的（221 只已由
# tools/fix_dirty_history.py 重下替换）。剩下这几只不是普通拆股，
# 而是**证券本身换了**。2026-10-28 用 Alpaca 的 corporate-actions 接口
# （CorporateActionsRequest）+ WOLF 的 8-K（2025-09-29，Item 8.01）核实：
#
#   WOLF 2025-09-29  unit_split：old_rate=1.0 → new_rate=0.008352。
#                     按 8-K：旧股全部注销（约 1.5648 亿股），按
#                     0.008352 新股/旧股换发新股（初始 1,306,903 股），
#                     旧 CUSIP 977852102 → 新 CUSIP 97785W106。
#                     **断点前后是两个不同的证券**，价格没有任何可比性。
#   NEUP 2024-12-24  stock_merger：1 股 BNOX → 0.08333333 股 NEUP。
#                     价格从 0.2531 跳到 6.63（+2519%）—— 那不是收益，
#                     是两个不同的证券拼在一起。
#
# 这类事件**不能**用「把断点前的价格乘一个比例」来修：
#   按 1/0.008352 = 119.73 倍还原 → 2025-09-26 的 1.21 变成 144.87，
#                                 而断点后是 22.10 → 凭空造出 -85% 的「跌幅」
#   按 20 倍还原（曾经的猜测）  → 24.20 vs 22.10 → -8.7%
#                             只是把错误缩小，没有变对
#
# 唯一的诚实做法：**窗口跨过断点就不给数**。
# 窗口起点（倒数第 253 根 bar）早于断点 = 这只票还没有「干净的 12 个月」，
# 与新股、刚重组的公司一样，暂时不进排名池；等断点后攒满 253 根 bar 自然回归。
#
# 危害是实测出来的：NEUP 在 as_of=2025-11-15 的加权收益是 **+265.7%**
# （R252 的基准价落在合并前的 0.2531），在历史排名池里直接排到第 1。
KNOWN_PRICE_BREAKS: dict[str, str] = {
    # Chapter 11 重组：旧股注销、按 0.008352 新股/旧股换发新股
    "WOLF": "2025-09-29",
    # 换股合并（stock merger）：1 股 BNOX → 0.08333333 股 NEUP
    "NEUP": "2024-12-24",
}


def break_inside_window(ticker: str, closes: pd.Series, window: int) -> bool:
    """
    已知历史断点是否落在 [窗口起点, 最后一根 bar] 之间。

    closes：这条线真正用到的 bar 序列（索引是日期）。
            标量路径传 _deduplicate_bars 的结果，宽表路径传该列 dropna 的结果。
    window：这条线的 bar 数（weighted=253、10w=51、10d=11）。

    断点正好落在窗口起点（= 基准价那根 bar）**不算**跨过：
    基准价已经是断点后的价格，与现价同尺度。
    """
    break_str = KNOWN_PRICE_BREAKS.get(ticker)
    if break_str is None or closes is None or len(closes) < window:
        return False
    idx = closes.index
    if not isinstance(idx, pd.DatetimeIndex):
        return False
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    break_ts = pd.Timestamp(break_str, tz="UTC")
    return bool(idx[-window] < break_ts <= idx[-1])


def breaks_inside_wide_window(wide: pd.DataFrame, window: int) -> pd.Series:
    """
    宽表路径的断点判定：返回每个 symbol 是否跨过了已知断点。

    必须和标量路径的 break_inside_window 做同一件事，否则两条路径
    在断点落入窗口时给出不同的答案（一边进池、一边落选）。
    """
    out = pd.Series(False, index=wide.columns)
    if wide is None or wide.empty:
        return out
    for ticker, break_str in KNOWN_PRICE_BREAKS.items():
        if ticker not in wide.columns:
            continue
        bars = wide[ticker].dropna()
        if len(bars) < window:
            continue
        idx = bars.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        break_ts = pd.Timestamp(break_str, tz="UTC")
        if idx[-window] < break_ts <= idx[-1]:
            out[ticker] = True
    return out


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """把索引统一成 UTC 零点的 DatetimeIndex，丢掉无法解析的行。"""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    out = df.copy()
    out.index = idx.normalize()
    return out[~out.index.isna()]


def _read_close_series_from_df(df: pd.DataFrame) -> pd.Series | None:
    """
    抽出 close 列并规整索引。

    **不 dropna**：NaN 必须原样留在原位。丢掉的行会让这一天的日期从并集索引里
    消失，缺口自动合拢 —— 一只票看起来就「连续」了，而它其实缺了一天。
    标量路径的 `_deduplicate_bars`（groupby.first）同样保留 NaN 行，两边必须一致。
    """
    if not isinstance(df, pd.DataFrame) or "close" not in df.columns:
        return None
    df = _normalize_index(df)
    s = pd.to_numeric(df["close"], errors="coerce")
    # 同一交易日的多条 intraday bar 与 _deduplicate_bars 保持一致：取当天第一条
    s = s[~s.index.duplicated(keep="first")]
    return s.sort_index()


def _read_close_series(path: Path) -> pd.Series | None:
    """读单个 pkl 的 close 列；不是行情文件就返回 None。"""
    try:
        df = pd.read_pickle(path)
    except Exception as exc:  # 损坏/非 pickle 一律跳过，不让一只票拖垮整批
        logger.debug("跳过 %s：读取失败 %s", path.name, exc)
        return None
    return _read_close_series_from_df(df)


def discover_symbols(cache_dir: Path) -> list[str]:
    """扫 cache 目录，返回所有行情 pkl 的 symbol（跳过 EPS / Polygon 缓存）。"""
    syms = [
        p.stem
        for p in cache_dir.glob("*.pkl")
        if not p.stem.startswith(_NON_PRICE_PREFIXES)
    ]
    return sorted(syms)


def build_universe_closes(
    symbols: list[str] | None = None,
    cache_dir: Path = Path("data/cache"),
    out_path: Path = DEFAULT_CLOSES_PATH,
    tail_rows: int = TAIL_ROWS,
    max_workers: int = 8,
) -> pd.DataFrame:
    """
    从 data/cache/*.pkl 构建宽表并落盘。

    返回的 DataFrame：index=交易日（UTC 零点，升序），columns=symbol，值=close。
    缺失保持 NaN，绝不 ffill。

    只读 cache_dir，从不写入 —— 「绝不删改行情缓存」是硬红线。
    """
    if symbols is None:
        symbols = discover_symbols(cache_dir)
    logger.info("构建 RS 宽表：%d 个 symbol，源 %s", len(symbols), cache_dir)

    def _load(sym: str) -> tuple[str, pd.Series] | None:
        s = _read_close_series(cache_dir / f"{sym}.pkl")
        return (sym, s) if s is not None else None

    series: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for got in pool.map(_load, symbols):
            if got is not None:
                series[got[0]] = got[1]

    if not series:
        raise RuntimeError(f"{cache_dir} 下没有可读的行情 pkl")

    wide = pd.DataFrame(series).sort_index()
    if tail_rows:
        wide = wide.iloc[-tail_rows:]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wide.to_pickle(out_path)

    logger.info(
        "宽表完成：%d 行 × %d 列 -> %s (%.1f MB)",
        wide.shape[0],
        wide.shape[1],
        out_path,
        out_path.stat().st_size / 1e6,
    )
    return wide


def load_universe_closes(path: Path = DEFAULT_CLOSES_PATH) -> pd.DataFrame | None:
    """读宽表；文件不存在或损坏时返回 None，调用方回退到逐只 pkl 的老路径。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        wide = pd.read_pickle(path)
    except Exception as exc:
        logger.warning("宽表 %s 读取失败，回退逐只 pkl：%s", path, exc)
        return None
    if not isinstance(wide, pd.DataFrame) or wide.empty:
        return None
    return wide


def market_reference_day(wide: pd.DataFrame, as_of=None) -> pd.Timestamp | None:
    """
    宽表里最后一个「**全市场都有数据**」的交易日。

    为什么必须有这个概念
    ------------------
    宽表由 `data/cache/*.pkl` 汇总而成，而 live run 每轮只刷新 **live pool**
    （6,317 只里约 5,155 只有盘）。剩下 4,377 只全市场票还停在上一交易日，
    于是宽表的**末行只覆盖约一半**（2026-10-26 实测 5,156/10,695 = 48%）。

    早期版本直接拿末行当「最新交易日」：排名池从 9,776 **静默腰斩**到 4,885，
    不报任何错，只是 RS Rating 的分位悄悄换了一个小得多的池子 ——
    而且换掉的还是「流动性最好的那一半」，百分位被系统性地抬高了。

    判定
    ----
    一行算「完整交易日」的条件是：覆盖率 ≥ 典型覆盖率 × MARKET_COVERAGE_RATIO。

    「典型覆盖率」取**看起来像完整交易日的那些行的中位数**（覆盖率 ≥ 全表最高
    覆盖率的一半）。这样长期停牌的少数票不会把门槛压低，节假日那种
    「只有零星标的有 bar」的行也不会被误当成完整交易日。

    返回 None 表示表里没有任何一行够格（退化表），调用方自行决定怎么办。

    as_of 给定时先切到该日期（含）为止 —— 回看历史日期时，参考日也必须
    是**那一天**的参考日，不能用今天宽表的末行。
    """
    if wide is None or wide.empty:
        return None
    if as_of is not None:
        wide = _slice_to_asof(wide, as_of)
        if wide.empty:
            return None
    coverage = wide.notna().sum(axis=1)
    if coverage.empty or coverage.max() == 0:
        return None

    best = int(coverage.max())
    dense = coverage[coverage >= best * DENSE_ROW_RATIO]
    typical = float(dense.median()) if len(dense) else float(best)
    threshold = typical * MARKET_COVERAGE_RATIO

    ok = coverage >= threshold
    if not ok.any():
        return None
    # 从尾部往前找第一个达标的行 = 最后一个达标的行
    return pd.Timestamp(ok[::-1].idxmax())


def trim_sparse_tail(wide: pd.DataFrame, min_ratio: float = 0.5) -> pd.DataFrame:
    """
    砍掉**尾部**「几乎没人有数据」的行。

    宽表索引是全体 symbol 的日期并集。哪天只有 SPY 抓到了一根当日 bar（盘中抓取、
    半个交易日、某只票的异常日历），那一行就只覆盖 1 列 —— 而它的存在会把
    「最后一个交易日」推到绝大多数票都没有的位置上，结果整个排名池塌成 1 只，
    **而且不报任何错**。

    只砍尾部：中间的稀疏行绝大多数是美股节假日（阵亡将士纪念日、感恩节…），
    那是真实信息 —— 有零星标的在这些日子有 bar，把它们删掉等于悄悄改数据。
    """
    if wide.empty:
        return wide
    coverage = wide.notna().sum(axis=1)
    median = coverage.median()
    if median == 0:
        return wide
    threshold = median * min_ratio

    # 从尾部往前找第一行「正常覆盖」，只在它之后动手
    cut = len(coverage)
    while cut > 0 and coverage.iloc[cut - 1] < threshold:
        cut -= 1
    if cut == 0 or cut == len(coverage):
        return wide

    logger.info(
        "宽表丢弃 %d 个稀疏尾部行（%s 起）", len(coverage) - cut, coverage.index[cut].date()
    )
    return wide.iloc[:cut]


def to_bars_back_frame(wide: pd.DataFrame) -> pd.DataFrame:
    """
    把「日期 × symbol」的日历宽表，转成「倒数第 k 根 bar × symbol」的对齐宽表。

    第 k 行 = 该 symbol **自己**的倒数第 k+1 根 bar。

    为什么要转：日历宽表的行是**并集**，每只票各有各的交易日历。直接在日历行上
    取 iloc[-253] 会踩空 —— 一只票休市一天、换个交易所日历、或者上市晚一点，
    位置就全错位。实测 6,303 只在日历宽表上算出 **0 只**。

    为什么按「倒数第 k 根」而不是「正数第 i 根」对齐：后者会让位置轴的末端由
    **bar 数最多的那只票**决定。中间休市一天的比它少一根 bar 的票，
    每一个 R* 都会整体错开一位 —— 而且数值看着完全正常，只是悄悄偏了一个交易日。

    ⚠️ 必须先 dropna：pandas 2.x 的 `stack()` 会把 NaN 行原样保留，
    不 dropna 的话数出来的是日历行数，这张表会退化成日历表本身。
    """
    long = (
        wide.rename_axis(index="date", columns="symbol")
        .stack()
        .dropna()
        .rename("close")
        .reset_index()
    )
    grouped = long.groupby("symbol", sort=False)["close"]
    long["k"] = grouped.transform("size") - 1 - grouped.cumcount()
    return long.pivot(index="k", columns="symbol", values="close").sort_index()


def _slice_to_asof(wide: pd.DataFrame, as_of) -> pd.DataFrame:
    """切到 as_of 当日（含）为止，跟标量 `_slice_to_date` 的口径一致。

    必须切：crossback 会按「信号当日」算 RS，而宽表是当前快照，
    最后一行永远是今天。不切的话，回看 3 天的信号会拿到今天的排名池。
    """
    if as_of is None:
        return wide
    ts = pd.Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    return wide.loc[wide.index <= ts.normalize()]


def _bars_back_rows(mask: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """
    mask: (T, N) 布尔矩阵，True = 该行该列有一根 bar。

    返回 (rows, ok)：
      rows[j] = 第 j 列「倒数第 window 根 bar」所在的行号（T-1 = 最后一行）
      ok[j]  = 该列是否至少有 window 根 bar
    """
    T = mask.shape[0]
    N = mask.shape[1]
    if T == 0:
        return np.zeros(N, dtype=int), np.zeros(N, dtype=bool)
    # 从底部往上数：rev_cum 到达 window 的那一行就是目标 bar
    rev_cum = np.cumsum(mask[::-1], axis=0, dtype=np.int32)
    ok = rev_cum[-1] >= window
    rev_idx = (rev_cum >= window).argmax(axis=0)
    return T - 1 - rev_idx, ok


def window_calendar_spans(
    wide: pd.DataFrame, window: int = DEEPEST_LOOKBACK + 1
) -> pd.Series:
    """
    每只票「最近 window 根 bar」横跨了多少个自然日。

    返回 Series（index=wide.columns），bar 数不足 window 的票记 0。

    为什么按「倒数第 k 根」量而不是按日历行量
    -------------------------------------------
    窗口是「这只票自己的最近 253 根 bar」。中间的空洞（停牌、数据缺口）
    正是要检出的东西，所以必须按 bar 数定位起止，再看这两根 bar 之间隔了
    多少个自然日。按日历行量的话，符号本身就被空洞抹平了。
    """
    if wide is None or wide.empty:
        return pd.Series(dtype=float)

    idx = wide.index
    if not isinstance(idx, pd.DatetimeIndex):
        return pd.Series(0.0, index=wide.columns)

    mask = wide.notna().values
    T, N = mask.shape[0], mask.shape[1]
    if T == 0:
        return pd.Series(0.0, index=wide.columns)

    rev = mask[::-1]
    has_any = rev.any(axis=0)
    last_rows = T - 1 - rev.argmax(axis=0)          # 最后一根 bar 所在的行
    del rev

    win_rows, ok = _bars_back_rows(mask, window)   # 倒数第 window 根 bar 所在的行

    out = np.zeros(N, dtype=float)
    valid = ok & has_any
    if valid.any():
        days = np.asarray((idx[last_rows] - idx[win_rows]).days)
        out[valid] = days[valid]
    return pd.Series(out, index=wide.columns)


def vectorized_weighted_return(
    wide: pd.DataFrame,
    as_of=None,
    max_price_span: float | None = None,
    max_window_span_days: int | None = None,
) -> pd.Series:
    """
    向量化算每列的 0.4*R63 + 0.2*R126 + 0.2*R189 + 0.2*R252。

    返回 Series（index=wide.columns），算不出的为 NaN。

    与标量 `_compute_weighted_return` 的**唯一**刻意差异：

      标量取「每只票自己的最后一根 bar」，所以一只停在三天前的票会带着
      陈旧的现价入池。宽表取「最后一个交易日有 bar 的票」，落后即落选。

      这是有意的：排名池是「今天可投资的股票」的横截面，拿三天前的价格
      当现价会系统性美化停牌/退市边缘票的百分位。

    除此之外（价格量级、bar 数不足、价格 <= 0）判定完全一致。

    排脏门槛是 `max_price_span`（整段窗口的 max/min 价格比），
    **不是**单日涨跌幅 —— 理由见 MAX_PRICE_SPAN 的注释。

    两个门槛的默认值在**调用时**从模块常量读，不在定义时绑定 ——
    排名池的缓存指纹（data/rs.py::_current_pool_signature）要跟这里的
    实际取值同源，改了常量签名必须跟着变，否则旧缓存会静默命中。
    """
    if max_price_span is None:
        max_price_span = MAX_PRICE_SPAN
    if max_window_span_days is None:
        max_window_span_days = MAX_WINDOW_SPAN_DAYS

    wide = _slice_to_asof(wide, as_of)
    # 参考日 = 最后一个「全市场都有数据」的交易日。末行往往只覆盖 live pool，
    # 直接拿末行当「最新交易日」会把排名池静默腰斩（详见 market_reference_day）。
    ref_day = market_reference_day(wide)
    if ref_day is not None:
        if ref_day != wide.index[-1]:
            logger.info(
                "参考日 %s（末行 %s 只覆盖 %.0f%%，不是全市场口径）",
                ref_day.date(),
                wide.index[-1].date(),
                wide.iloc[-1].notna().sum() / max(len(wide.columns), 1) * 100,
            )
        wide = wide.loc[:ref_day]
    else:
        logger.warning("宽表里没有任何一行达到全市场覆盖门槛，按原样计算")
    wide = trim_sparse_tail(wide)
    if len(wide) < 1:
        return pd.Series(float("nan"), index=wide.columns, dtype=float)

    # 已知历史断点：窗口跨过断点的票直接落选（断点前后的价格不可比）。
    # 必须和标量路径（data/rs.py 的 break_inside_window）做同一件事。
    broken = breaks_inside_wide_window(wide, DEEPEST_LOOKBACK + 1)

    # 过期判定走**日历**：最后一个交易日必须有 bar。落后一天的票在这里直接落选，
    # 不能拿三天前的收盘价当现价进排名。
    has_current_bar = wide.iloc[-1].notna()

    # 收益计算走**倒数第 k 根**对齐帧：每列都以自己的最后一根 bar 为第 0 行，
    # 与标量 _compute_weighted_return 逐字对应（不管中间休过几天市）。
    bars = to_bars_back_frame(wide)
    if len(bars) < DEEPEST_LOOKBACK + 1:
        return pd.Series(float("nan"), index=wide.columns, dtype=float)

    cur = bars.iloc[0]

    # 排脏门槛 = 整段 253 根窗口内的**价格量级**，不是涨跌幅。见 MAX_PRICE_SPAN。
    #
    # 窗口取 bars 前 DEEPEST_LOOKBACK + 1 行（正好是算 R252 用的那段），
    # 不能只看尾部：SXTC 是「早期价格极高、近期 0.05 元」的形状。
    # min/max 默认 skipna，缺口行（节假日、休市）自然跳过，不会被当成 0。
    win = bars.iloc[: DEEPEST_LOOKBACK + 1]
    win_lo = win.min()
    # 最低价 <= 0 时比值无意义，标成无穷大交给同一道门槛挡掉
    span = (win.max() / win_lo).where(win_lo > 0, np.inf)
    absurd = span > max_price_span

    # 第二道闸门：窗口横跨的日历天数。
    # 253 根 bar 摊在 400 天以上，说明中间有大空洞 —— 这只票的
    # 「12 个月相对强度」其实是「上一次有数据到现在」。见 MAX_WINDOW_SPAN_DAYS。
    spans = window_calendar_spans(wide, DEEPEST_LOOKBACK + 1)
    stretched = spans > max_window_span_days

    wr = pd.Series(0.0, index=wide.columns)
    valid = has_current_bar & cur.notna() & cur.gt(0)
    for weight, lookback in WEIGHTED_PARTS:
        past = bars.iloc[lookback]
        wr = wr + weight * ((cur / past) - 1.0)
        valid = valid & past.notna() & past.gt(0)

    out = wr.where(valid & ~absurd & ~stretched & ~broken)

    # 逐条记下落选原因。排名池一旦无声地缩水，RS Rating 会整体位移而没人发现，
    # 所以这里宁可啰嗦也要把「因窗口太短攒不满 253 根」这种**我们的**限制
    # 和「票本身太冷门」区分开。
    dropped = ~out.notna()
    logger.info(
        "RS 排名池：%d/%d 只可算（最新交易日 %s，窗口 %d 个日历行 / %d 根 bar 容量）",
        int((~dropped).sum()),
        len(out),
        wide.index[-1].date(),
        len(wide),
        len(bars),
    )
    if dropped.any():
        detail = {
            "落后于最新交易日（刻意排除）": int((dropped & ~has_current_bar).sum()),
            "窗口内攒不满 253 根 bar（低流动性）": int(
                (dropped & has_current_bar & (bars.notna().sum() < DEEPEST_LOOKBACK + 1)).sum()
            ),
            "价格量级荒谬（数据损坏）": int((dropped & has_current_bar & absurd).sum()),
            "窗口被空洞拉长（跨度过大）": int(
                (dropped & has_current_bar & ~absurd & stretched).sum()
            ),
            "回看基准价缺失或非正": int(
                (dropped & has_current_bar & ~absurd & ~stretched & ~broken
                 & (bars.notna().sum() >= DEEPEST_LOOKBACK + 1)).sum()
            ),
            "窗口跨越已知历史断点（重组/合并）": int(
                (dropped & has_current_bar & ~absurd & ~stretched & broken).sum()
            ),
        }
        logger.info("  落选明细：%s", "，".join(f"{k} {v}" for k, v in detail.items() if v))
    return out


def update_universe_store(
    market_data: dict[str, pd.DataFrame],
    path: Path = DEFAULT_CLOSES_PATH,
    tail_rows: int = TAIL_ROWS,
) -> pd.DataFrame:
    """
    用刚抓完数据的内存 market_data 增量更新宽表（live run 里 fetch() 之后调用）。

    数据源是内存里已经算好的 DataFrame，**额外网络成本为零**。
    只 upsert 这些 symbol 的行，其余列原样保留。
    """
    fresh = {
        sym: _read_close_series_from_df(df) for sym, df in market_data.items()
    }
    fresh = {sym: s for sym, s in fresh.items() if s is not None}
    if not fresh:
        existing = load_universe_closes(path)
        return existing if existing is not None else pd.DataFrame()

    # 首次运行时直接用内存里的这些序列建表 —— 不能退回 build_universe_closes，
    # 那是给「从磁盘扫全目录」用的，走那条路会把本轮没传进来的列全丢掉。
    existing = load_universe_closes(path)
    if existing is None:
        combined = pd.DataFrame(fresh).sort_index()
    else:
        # errors="ignore"：宽表里还没有的**新代码**不能让它炸。
        # 没有它时这只票永远进不了宽表（也就永远进不了排名池），
        # 而 live run 里这个 KeyError 会被 try/except 吞成一行警告 ——
        # 静默，而且丢的是永久性的（除非有人手动把宽表删了重建）。
        combined = pd.concat(
            [existing.drop(columns=list(fresh), errors="ignore"), pd.DataFrame(fresh)],
            axis=1,
        ).sort_index()
    if tail_rows:
        combined = combined.iloc[-tail_rows:]

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(out_path)
    logger.info("宽表增量更新：+%d 列 -> %d 列 × %d 行", len(fresh), combined.shape[1], combined.shape[0])
    return combined