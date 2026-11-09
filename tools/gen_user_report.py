"""用户版日报生成器（tools/gen_user_report.py）

把「手工核对 19 只票 × 17 个字段」变成一条命令：

    uv run python tools/gen_user_report.py --date 2026-11-09

读
    output/v_eps_v2/signals.csv        信号流水（主策略 + EMA10 回踩）
    output/v_eps_v2/watchlist.json     监控池
    data/cache/<SYM>.pkl               行情（**按交易日去重**后计算窗口）
    data/rs_universe.json              名称 / 板块
    data/sector_cache.json             Finnhub 行业（用于交叉校验板块归属）
    data/rs_daily_cache.json           RS 排名池（数量 + 签名，写进 caveat）

写
    output/v_eps_v2/<date>/报告_用户版_Weinstein + EPS_<date>.md

stdout
    可直接贴进对话的三样：信号表 / 三档建议 / 大盘一行

口径约定（与策略一致，改这里等于改策略口径，勿随手改）
    * 所有窗口按**交易日**计数，不是行数 —— 见 data/daily_bars.dedup_daily_bars
    * 量比 = 当日量 ÷ 前 20 个交易日均量（不含当日）
    * 枢轴 = 前 30 个交易日最高收盘（不含当日）
    * 今日行的量比**重算**；历史行沿用当时入库的触发原因里的量比（未按新口径重算）
    * RS 四列 / EPS 字段取自 signals.csv 与触发原因串，不重算
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.daily_bars import dedup_daily_bars  # noqa: E402

MINUS = "\u2212"  # 减号，比 ASCII '-' 排版好看
MAIN_TYPE = "v_eps_v2_buy"
CROSSBACK_TYPE = "crossback_buy"
BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA")


# --------------------------------------------------------------------------
# 数值格式化
# --------------------------------------------------------------------------
def _f(value, nd: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        out = f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return dash
    return out.replace("-", MINUS)


def _money(value, dash: str = "—") -> str:
    """金额用 ASCII 减号（与 EPS / 止损价的历史写法一致）；比例与百分比用 U+2212。"""
    if value is None:
        return dash
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return dash


def _pct(value, dash: str = "—", nd: int = 2) -> str:
    if value is None:
        return dash
    return f"{float(value):+.{nd}f}%".replace("-", MINUS)


def _x(value, dash: str = "—") -> str:
    return dash if value is None else f"{_f(value)}x"


def _stars(n: int) -> str:
    n = max(0, min(5, int(n or 0)))
    return "★" * n + "☆" * (5 - n)


# --------------------------------------------------------------------------
# 行情指标
# --------------------------------------------------------------------------
@dataclass
class Metrics:
    """一只标的的日线指标（全部按交易日计算）。"""

    symbol: str
    px: float
    prev_px: float
    chg: float
    r5: float
    r10: float
    r20: float
    vol: float
    avg20: float
    vol_ratio: float
    pivot: float
    breakout: float
    up_days: int
    low30: float
    px_vs_low30: float
    low60: float
    hi20: float
    sma150: float | None
    sma150_30ago: float | None
    px_vs_sma: float | None
    last_bar: str
    last_day: str
    bars: int
    index: pd.DatetimeIndex = field(repr=False, default_factory=pd.DatetimeIndex)


def _load_bars(cache_dir: Path, symbol: str) -> pd.DataFrame | None:
    path = Path(cache_dir) / f"{symbol}.pkl"
    if not path.exists():
        return None
    try:
        df = pd.read_pickle(path)
    except Exception:
        return None
    if not isinstance(df, pd.DataFrame) or df.empty or "close" not in df.columns:
        return None
    return df


def _compute_metrics(df: pd.DataFrame, symbol: str) -> Metrics | None:
    """把日线 df 折成一组指标。窗口一律按**交易日**（先 dedup）。"""
    d = dedup_daily_bars(df)
    if d is None or d.empty or "close" not in d.columns:
        return None

    closes = d["close"].astype(float)
    vols = d["volume"].astype(float) if "volume" in d.columns else None
    n = len(closes)
    px = float(closes.iloc[-1])
    prev_px = float(closes.iloc[-2]) if n >= 2 else px

    def _ret(offset: int) -> float | None:
        if n < offset + 1:
            return None
        base = float(closes.iloc[-(offset + 1)])
        return (px / base - 1.0) * 100.0 if base else None

    vol = avg20 = vol_ratio = None
    if vols is not None and n >= 21:
        vol = float(vols.iloc[-1])
        avg20 = float(vols.iloc[-21:-1].mean())
        vol_ratio = (vol / avg20) if avg20 else None

    pivot = breakout = None
    if n >= 31:
        pivot = float(closes.iloc[-31:-1].max())
        breakout = (px / pivot - 1.0) * 100.0 if pivot else None

    up_days = 0
    for i in range(n - 1, 0, -1):
        if float(closes.iloc[i]) > float(closes.iloc[i - 1]):
            up_days += 1
        else:
            break

    low30 = float(closes.iloc[-30:].min()) if n >= 30 else float(closes.min())
    low60 = float(closes.iloc[-60:].min()) if n >= 60 else low30
    hi20 = float(closes.iloc[-21:-1].max()) if n >= 21 else float(closes.max())

    sma150 = sma150_30ago = px_vs_sma = None
    if n >= 150:
        sma = closes.rolling(150).mean()
        sma150 = float(sma.iloc[-1])
        sma150_30ago = float(sma.iloc[-30])
        px_vs_sma = (px / sma150 - 1.0) * 100.0 if sma150 else None

    last = pd.Timestamp(d.index[-1])
    return Metrics(
        symbol=symbol,
        px=px,
        prev_px=prev_px,
        chg=(px / prev_px - 1.0) * 100.0 if prev_px else 0.0,
        r5=_ret(5),
        r10=_ret(10),
        r20=_ret(20),
        vol=vol,
        avg20=avg20,
        vol_ratio=vol_ratio,
        pivot=pivot,
        breakout=breakout,
        up_days=up_days,
        low30=low30,
        px_vs_low30=(px / low30 - 1.0) * 100.0 if low30 else None,
        low60=low60,
        hi20=hi20,
        sma150=sma150,
        sma150_30ago=sma150_30ago,
        px_vs_sma=px_vs_sma,
        last_bar=str(last),
        last_day=str(last.date()),
        bars=n,
        index=d.index,
    )


def _ema10_at(df: pd.DataFrame, before: str) -> float | None:
    """某个信号日**之前**最后一根 bar 上的 EMA10（信号标签日 = 运行日，触发 bar 是再前一根）。"""
    if df is None or df.empty or "close" not in df.columns:
        return None
    d = dedup_daily_bars(df)
    mask = d.index.normalize() < pd.Timestamp(before, tz="UTC")
    if not mask.any():
        return None
    ema = d["close"].astype(float).ewm(span=10).mean()
    return float(ema[mask].iloc[-1])


# --------------------------------------------------------------------------
# 触发原因解析
# --------------------------------------------------------------------------
_RE_EPS = re.compile(r"最新季度EPS\$(-?[\d.]+)")
_RE_YOY = re.compile(r"比去年同期([^；，|]+)")
_RE_QOQ = re.compile(r"比上季度([^；，|]+)")
_RE_QOQ_STREAK = re.compile(r"最近(\d+)个季度的EPS都比上季度高")
_RE_YOY_STREAK = re.compile(r"最近(\d+)个季度的EPS都比去年同期同季度高")
_RE_VOL = re.compile(r"\[量能\]\s*([\d.]+)x")
_RE_RS_RATING = re.compile(r"RS-Rating=(\d+)")
_RE_RS_SPY = re.compile(r"RS/SPY=(-?[\d.]+)x")
_RE_RS_10W = re.compile(r"RS-10W=(-?[\d.]+)x")
_RE_RS_10D = re.compile(r"RS-10D=(-?[\d.]+)x")
_RE_SECTOR_RANK = re.compile(r"\(([^()]+?)\s*排名(\d+)/(\d+)\)")
_RE_CB_CLOSE = re.compile(r"上穿EMA10\(\$([\d.]+)")
_RE_CB_PCT = re.compile(r"上穿EMA10\(\$[\d.]+,\s*([+\-−\d.]+)%")
_RE_CB_VOL = re.compile(r"量能([\d.]+)x")


def _fmt_change(raw: str | None) -> str | None:
    """'高166.0%' → '高 166.0%'；'亏损收窄64.6%' → '亏损收窄 64.6%'。"""
    if not raw:
        return None
    text = raw.strip()
    m = re.match(r"^(.*?)(-?[\d.]+)\s*%?$", text)
    if not m:
        return text
    prefix = m.group(1).strip()
    number = m.group(2)
    return f"{prefix} {number}%" if prefix else f"{number}%"


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    text = str(text).strip().replace(MINUS, "-").replace("$", "").replace("x", "")
    if text in ("", "—", "-", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_reason(reason: str) -> dict:
    """把触发原因串拆成结构化字段。"""
    out: dict = {}
    if not reason:
        return out

    m = _RE_EPS.search(reason)
    out["eps"] = float(m.group(1)) if m else None

    m = _RE_YOY.search(reason)
    out["yoy"] = _fmt_change(m.group(1)) if m else None

    m = _RE_QOQ.search(reason)
    out["qoq"] = _fmt_change(m.group(1)) if m else None

    m = _RE_YOY_STREAK.search(reason)
    out["yoy_streak"] = int(m.group(1)) if m else None

    m = _RE_QOQ_STREAK.search(reason)
    out["qoq_streak"] = int(m.group(1)) if m else None

    m = _RE_VOL.search(reason)
    out["vol_reason"] = float(m.group(1)) if m else None

    m = _RE_RS_RATING.search(reason)
    out["rs_rating_reason"] = int(m.group(1)) if m else None

    m = _RE_RS_SPY.search(reason)
    out["rs_spy_reason"] = float(m.group(1)) if m else None

    m = _RE_RS_10W.search(reason)
    out["rs_10w_reason"] = float(m.group(1)) if m else None

    m = _RE_RS_10D.search(reason)
    out["rs_10d_reason"] = float(m.group(1)) if m else None

    m = _RE_SECTOR_RANK.search(reason)
    if m:
        out["sector"] = m.group(1).strip()
        out["sector_rank"] = f"{m.group(2)}/{m.group(3)}"

    m = _RE_CB_CLOSE.search(reason)
    out["cb_close"] = float(m.group(1)) if m else None
    m = _RE_CB_PCT.search(reason)
    out["cb_pct"] = float(m.group(1).replace("−", "-")) if m else None
    m = _RE_CB_VOL.search(reason)
    out["cb_vol"] = float(m.group(1)) if m else None
    return out


# --------------------------------------------------------------------------
# 输入加载
# --------------------------------------------------------------------------
def _load_signals(csv_path: Path) -> list[dict]:
    path = Path(csv_path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _load_json(path: Path | None, default):
    if path is None:
        return default
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _num(record: dict, key: str) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    return _to_float(value)


# --------------------------------------------------------------------------
# 报告数据组装
# --------------------------------------------------------------------------
@dataclass
class SignalView:
    """一条信号（主策略或回踩）渲染所需的全部内容。"""

    date: str
    symbol: str
    signal_type: str
    strength: int
    reason: str
    stop: float | None
    parsed: dict
    metrics: Metrics | None
    rs_rating: float | None
    rs_spy: float | None
    rs_10w: float | None
    rs_10d: float | None
    sector: str | None
    sector_rank: str | None
    name: str | None
    is_today: bool = False
    superseded_by: str | None = None
    stop_pct: float | None = None


@dataclass
class WatchView:
    symbol: str
    px: float | None
    stop: float | None
    rel: float | None
    bucket: str
    crossback_count: int
    last_crossback_date: str | None
    expires_at: str | None
    expires_in_days: int | None


@dataclass
class ReportData:
    date_str: str
    data_day: str | None
    today_main: list[SignalView] = field(default_factory=list)
    history_main: list[SignalView] = field(default_factory=list)
    today_cross: list[SignalView] = field(default_factory=list)
    history_cross: list[SignalView] = field(default_factory=list)
    watch: list[WatchView] = field(default_factory=list)
    market: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    sectors: dict = field(default_factory=dict)
    rs_pool: dict = field(default_factory=dict)
    stale_symbols: list[str] = field(default_factory=list)


def _build_signal_views(
    records: list[dict],
    date_str: str,
    cache_dir: Path,
    universe: dict,
) -> list[SignalView]:
    views: list[SignalView] = []
    for rec in records:
        symbol = (rec.get("股票") or "").strip()
        if not symbol:
            continue
        parsed = parse_reason(rec.get("触发原因", ""))
        df = _load_bars(cache_dir, symbol)
        metrics = _compute_metrics(df, symbol) if df is not None else None
        info = universe.get(symbol, {}) if isinstance(universe, dict) else {}

        rs_rating = _num(rec, "RS相对强度")
        if rs_rating is None:
            rs_rating = parsed.get("rs_rating_reason")
        rs_spy = _num(rec, "RS/SPY")
        if rs_spy is None:
            rs_spy = parsed.get("rs_spy_reason")
        rs_10w = _num(rec, "RS-10W")
        if rs_10w is None:
            rs_10w = parsed.get("rs_10w_reason")
        rs_10d = _num(rec, "RS-10D")
        if rs_10d is None:
            rs_10d = parsed.get("rs_10d_reason")

        sector = parsed.get("sector") or info.get("gics_sector")
        sector_rank = (rec.get("板块排名") or "").strip() or parsed.get("sector_rank")
        if sector_rank in ("", "—"):
            sector_rank = None

        stop = _to_float(rec.get("参考止损"))
        views.append(
            SignalView(
                date=rec.get("日期", ""),
                symbol=symbol,
                signal_type=rec.get("信号类型", ""),
                strength=int(_num(rec, "强度(1-5)") or 0),
                reason=rec.get("触发原因", ""),
                stop=stop,
                parsed=parsed,
                metrics=metrics,
                rs_rating=rs_rating,
                rs_spy=rs_spy,
                rs_10w=rs_10w,
                rs_10d=rs_10d,
                sector=sector,
                sector_rank=sector_rank,
                name=info.get("name"),
                stop_pct=(stop / metrics.px - 1.0) * 100.0 if stop and metrics else None,
            )
        )
    return views


def _build_watch_views(watchlist: dict, cache_dir: Path, today: date) -> list[WatchView]:
    out: list[WatchView] = []
    for symbol, entry in watchlist.items():
        if not isinstance(entry, dict):
            continue
        df = _load_bars(cache_dir, symbol)
        metrics = _compute_metrics(df, symbol) if df is not None else None
        stop = _to_float(entry.get("stage2_stop"))
        px = metrics.px if metrics else None
        if px is None:
            bucket = "nodata"
            rel = None
        elif stop is None:
            bucket = "nostop"
            rel = None
        else:
            rel = (px / stop - 1.0) * 100.0
            bucket = "green" if rel >= 3.0 else ("red" if rel <= 0 else "yellow")

        expires_at = entry.get("expires_at")
        expires_in = None
        if expires_at:
            try:
                expires_in = (date.fromisoformat(str(expires_at)[:10]) - today).days
            except ValueError:
                expires_in = None

        out.append(
            WatchView(
                symbol=symbol,
                px=px,
                stop=stop,
                rel=rel,
                bucket=bucket,
                crossback_count=int(entry.get("crossback_count") or 0),
                last_crossback_date=entry.get("last_crossback_date"),
                expires_at=expires_at,
                expires_in_days=expires_in,
            )
        )
    return out


def _market_snapshot(cache_dir: Path) -> dict:
    out: dict = {"benchmarks": {}, "spy": None}
    for sym in BENCHMARKS:
        df = _load_bars(cache_dir, sym)
        m = _compute_metrics(df, sym) if df is not None else None
        if m:
            out["benchmarks"][sym] = m
            if sym == "SPY":
                out["spy"] = m
    return out


def _rs_pool_info(project_root: Path, date_str: str) -> dict:
    cache = _load_json(project_root / "data/rs_daily_cache.json", {})
    if not isinstance(cache, dict):
        return {}
    day = cache.get(date_str)
    if not isinstance(day, dict):
        days = sorted(cache)
        day = cache.get(days[-1]) if days else None
    if not isinstance(day, dict):
        return {}
    uni = day.get("__UNIVERSE__")
    if not isinstance(uni, dict):
        return {}
    return {
        "count": uni.get("count"),
        "pool_sig": uni.get("pool_sig"),
        "source": uni.get("source"),
    }


def _collect(date_str: str, signals_csv: Path, watchlist_path: Path, cache_dir: Path,
             universe_path: Path | None, days_back: int, today: date) -> ReportData:
    records = _load_signals(signals_csv)
    universe_raw = _load_json(universe_path, {})
    universe = universe_raw.get("symbols", {}) if isinstance(universe_raw, dict) else {}
    sectors_raw = _load_json(PROJECT_ROOT / "data/sector_cache.json", {})
    sectors = sectors_raw if isinstance(sectors_raw, dict) else {}
    watchlist = _load_json(watchlist_path, {})

    start = (date.fromisoformat(date_str) - timedelta(days=days_back)).isoformat()

    def _in_window(rec: dict) -> bool:
        d = rec.get("日期", "")
        return start <= d <= date_str

    main = [r for r in records if r.get("信号类型") == MAIN_TYPE and _in_window(r)]
    cross = [r for r in records if r.get("信号类型") == CROSSBACK_TYPE and _in_window(r)]

    main_views = _build_signal_views(main, date_str, cache_dir, universe)
    cross_views = _build_signal_views(cross, date_str, cache_dir, universe)

    for v in main_views:
        v.is_today = v.date == date_str
    for v in cross_views:
        v.is_today = v.date == date_str

    # 同标的被更新的信号取代 → 老行标记「已被取代」
    newest: dict[str, str] = {}
    for v in sorted(main_views, key=lambda x: x.date):
        newest[v.symbol] = v.date
    for v in main_views:
        if not v.is_today and newest.get(v.symbol, v.date) != v.date:
            v.superseded_by = newest.get(v.symbol)

    data = ReportData(
        date_str=date_str,
        data_day=None,
        today_main=[v for v in main_views if v.is_today],
        history_main=[v for v in main_views if not v.is_today],
        today_cross=[v for v in cross_views if v.is_today],
        history_cross=[v for v in cross_views if not v.is_today],
        watch=_build_watch_views(watchlist, cache_dir, today),
        market=_market_snapshot(cache_dir),
        universe=universe,
        sectors=sectors,
        rs_pool=_rs_pool_info(PROJECT_ROOT, date_str),
    )
    data.today_main.sort(key=lambda v: -(v.rs_rating or 0))
    data.history_main.sort(key=lambda v: v.date, reverse=True)
    data.today_cross.sort(key=lambda v: -(v.rs_rating or 0))
    data.history_cross.sort(key=lambda v: v.date, reverse=True)

    days = {v.metrics.last_day for v in data.today_main + data.today_cross if v.metrics}
    data.data_day = max(days) if days else None
    data.stale_symbols = [
        v.symbol for v in data.today_main
        if v.metrics and data.data_day and v.metrics.last_day != data.data_day
    ]
    return data


# --------------------------------------------------------------------------
# 渲染：字段说明（固定文案）
# --------------------------------------------------------------------------
FIELD_GLOSSARY = [
    ("信号日期", "策略检测到条件的日期（UTC 运行日）", "**不等于**执行日；下一个美股开盘执行"),
    ("股票", "美股代码", "先看板块，再看公司名（名称来自 Nasdaq screener）"),
    ("强度", "信号强度星级（1-5）", "★ 越多 = 技术面 + 基本面同时越干净"),
    ("参考止损", "入场后立即挂的止损价", "= 信号日收盘 × 0.92（**−8%**）；破位离场，不扛单"),
    ("最新季度 EPS", "最近一个财季的每股收益", "**为负说明还在亏损**，\"亏损收窄\"≠ 增长"),
    ("EPS 同比 (YoY)", "本季 EPS vs 去年同期", "高 = 盈利质量提升；**低基数会让数字虚高**"),
    ("EPS 环比 (QoQ)", "本季 EPS vs 上一季", "连续环比为正 = 加速，是最干净的加速证据"),
    ("连续同比季度", "EPS 连续同比增长的季度数", "≥2 季度 = 基本面连续性较强（CAN SLIM 要求）"),
    ("连续环比季度", "EPS 连续环比增长的季度数", "比同比更严格；**空白 = 没有连续增长**，不是数据缺失"),
    ("量比 (vs/20日均量)", "当日成交量 ÷ 前 20 个**交易日**均量", "≥1.5x 是入场门槛；>2x 记 ⚡（强势放量）"),
    ("RS Rating", "O'Neil RS Rating，全市场 1-99 百分位", "≥90 = 市场 leaders；**— = 算不出（不是弱）**"),
    ("RS/SPY (长期)", "加权 63/126/189/252 日收益 ÷ SPY 同期", ">1 跑赢大盘；比值无上限，只说强弱不说名次"),
    ("RS-10W (10周)", "近 50 个交易日涨幅 ÷ SPY 同期", "中期动能；**<1 说明中期跑输大盘**"),
    ("RS-10D (10日)", "近 10 个交易日涨幅 ÷ SPY 同期", "短期动能，最贴近当下盘面"),
    ("板块", "GICS 一级行业（Nasdaq screener 分类）", "用来看轮动；归类可疑的票看 caveat"),
    ("板块排名", "该股在全市场同行业里的 RS 强弱名次", "分母是全市场该行业总数（RS 排名池）"),
    ("状态", "执行建议 + 现价 + 当日涨跌", "🟢 可执行 / 🟡 未执行 / ⬛ 已被新信号取代 / 🔴 已破止损"),
]

MONITOR_LEGEND = (
    "**监控池颜色**：🟢 现价高于止损 3% 以上（安全）｜🟡 贴近止损（3% 以内，随时可能被扫）｜"
    "🔴 已破止损（按各自止损价执行）｜⚪ 无行情数据"
)

RS_BANDS = [
    ("90-99", "极强", "全市场前 10%，市场 leaders 级别"),
    ("80-89", "强", "全市场前 20%，符合 CAN SLIM 对 RS 的基本要求"),
    ("70-79", "中强", "全市场前 30%，可关注但不是首选"),
    ("50-69", "中性", "与市场平均无明显差异"),
    ("1-49", "弱", "跑输市场中位，趋势疲软，不建议追"),
]


def _rs_band(rating: float | None) -> str:
    if rating is None:
        return "—"
    if rating >= 90:
        return "90-99"
    if rating >= 80:
        return "80-89"
    if rating >= 70:
        return "70-79"
    if rating >= 50:
        return "50-69"
    return "1-49"


# --------------------------------------------------------------------------
# 渲染：状态列
# --------------------------------------------------------------------------
def _status_cell(v: SignalView) -> str:
    m = v.metrics
    parts: list[str] = []
    if v.is_today:
        if v.rs_rating is not None and v.rs_rating < 50:
            parts.append("⚠️ 今日待买入但 RS 弱")
        else:
            parts.append("🟢 今日待买入")
    elif v.superseded_by:
        parts.append(f"⬛ 已被 {v.superseded_by} 新信号取代")
    elif m and v.stop and m.px < v.stop:
        parts.append(f"🔴 已破止损（现价 {_money(m.px)}，低于 {_money(v.stop)}）")
    else:
        parts.append(f"🟡 {v.date} 信号仍未执行")

    if m:
        parts.append(f"现价 {_money(m.px)}（{_pct(m.chg)}）")
        if v.is_today and m.px_vs_sma is not None and m.px_vs_sma < 2.0:
            parts.append(f"仅高于 SMA150 {m.px_vs_sma:.2f}%")
    elif v.stop:
        parts.append(f"现价 —（参考止损 {_money(v.stop)}）")

    warn: list[str] = []
    eps = v.parsed.get("eps")
    if eps is not None and eps < 0:
        warn.append("EPS 仍为负")
    yoy = v.parsed.get("yoy")
    if yoy:
        num = _to_float(yoy.replace("高", "").replace("低", ""))
        if num is not None and num > 300:
            warn.append("低基数")
    if v.rs_rating is not None and 50 <= v.rs_rating < 70:
        warn.append(f"RS {int(v.rs_rating)} 中性")
    if v.rs_rating is not None and v.rs_rating < 50:
        warn.append(f"RS {int(v.rs_rating)} 弱")
    if v.rs_spy is not None and 0 <= v.rs_spy < 0.5:
        warn.append(f"RS/SPY 仅 {v.rs_spy:.2f}x")
    if m and v.is_today and m.chg >= 15:
        warn.append(f"单日暴涨 {_pct(m.chg)}")
    if warn:
        parts.append("⚠️ " + "、".join(warn))
    return "｜".join(parts)


def _vol_cell(v: SignalView, recompute: bool, bold: bool = True) -> str:
    ratio = v.metrics.vol_ratio if (recompute and v.metrics) else v.parsed.get("vol_reason")
    if ratio is None:
        return "—"
    text = f"{ratio:.1f}x"
    if bold:
        text = f"**{text}**"
    # 阈值按**显示值**判定：1.98x 显示成 2.0x 就该带 ⚡，否则表里看着自相矛盾
    shown = round(ratio, 1)
    if shown >= 4.0:
        return text + " ⚡🔥"
    if shown >= 2.0:
        return text + " ⚡"
    return text


def _streak_cell(n: int | None, bold: bool = True) -> str:
    """连续季度数：≥4 季度是基本面最干净的一档，标 🔥。"""
    if not n:
        return "—"
    text = f"{n} 季度"
    if bold:
        text = f"**{text}**"
    return text + (" 🔥" if n >= 4 else "")


def _main_row(v: SignalView) -> str:
    p = v.parsed
    cells = [
        f"**{v.date}**",
        f"**{v.symbol}**",
        _stars(v.strength),
        f"**{_money(v.stop)}**",
        f"**{_money(p.get('eps'))}**",
        f"**{p.get('yoy') or '—'}**",
        f"**{p.get('qoq') or '—'}**",
        _streak_cell(p.get("yoy_streak")),
        _streak_cell(p.get("qoq_streak")),
        _vol_cell(v, recompute=True),
        f"**{int(v.rs_rating)}**" if v.rs_rating is not None else "—",
        f"**{_x(v.rs_spy)}**" if v.rs_spy is not None else "—",
        f"**{_x(v.rs_10w)}**" if v.rs_10w is not None else "—",
        f"**{_x(v.rs_10d)}**" if v.rs_10d is not None else "—",
        f"**{v.sector}**" if v.sector else "—",
        f"**{v.sector_rank}**" if v.sector_rank else "—",
        _status_cell(v),
    ]
    return "| " + " | ".join(cells) + " |"


def _history_row(v: SignalView) -> str:
    """历史行：量比沿用当时入库的值（未按新口径重算）。"""
    p = v.parsed
    cells = [
        v.date,
        v.symbol,
        _stars(v.strength),
        _money(v.stop),
        _money(p.get("eps")),
        p.get("yoy") or "—",
        p.get("qoq") or "—",
        _streak_cell(p.get("yoy_streak"), bold=False),
        _streak_cell(p.get("qoq_streak"), bold=False),
        _vol_cell(v, recompute=False, bold=False),
        f"{int(v.rs_rating)}" if v.rs_rating is not None else "—",
        _x(v.rs_spy),
        _x(v.rs_10w),
        _x(v.rs_10d),
        v.sector or "—",
        v.sector_rank or "—",
        _status_cell(v),
    ]
    return "| " + " | ".join(cells) + " |"


# --------------------------------------------------------------------------
# 渲染：回踩表
# --------------------------------------------------------------------------
def _cross_status(v: SignalView) -> str:
    m = v.metrics
    if m and v.stop:
        rel = (m.px / v.stop - 1.0) * 100.0
        if rel <= 0:
            return f"🔴 已破止损（{_money(m.px)}，低于 {_money(v.stop)} {_f(abs(rel))}%）"
        if rel < 3:
            return f"🟡 贴止损（{_money(m.px)}，{_pct(rel)}）"
        return f"🟢 持有（{_money(m.px)}，{_pct(rel)}）"
    if m:
        return f"🟢 持有（{_money(m.px)}）"
    return "—"


def _cross_today_row(v: SignalView) -> str:
    p = v.parsed
    cells = [
        f"**{v.date}**",
        f"**{v.symbol}**",
        _stars(v.strength),
        _money(v.metrics.px if v.metrics else None),
        _money(p.get("cb_close")),
        _pct(p.get("cb_pct")),
        f"**{_x(p.get('cb_vol'))}**" if p.get("cb_vol") is not None else "—",
        f"**{_money(v.stop)}**" if v.stop else "—",
        _cross_status(v),
    ]
    return "| " + " | ".join(cells) + " |"


def _cross_history_row(v: SignalView) -> str:
    p = v.parsed
    cells = [
        v.date,
        v.symbol,
        _stars(v.strength),
        _money(p.get("cb_close")),
        _pct(p.get("cb_pct")),
        _x(p.get("cb_vol")),
        _money(v.stop),
        _money(v.metrics.px if v.metrics else None),
        _cross_status(v),
    ]
    return "| " + " | ".join(cells) + " |"


# --------------------------------------------------------------------------
# 渲染：段 3
# --------------------------------------------------------------------------
def _strong_combo(data: ReportData) -> list[SignalView]:
    out = []
    for v in data.today_main:
        if v.rs_rating is None or v.rs_rating < 80:
            continue
        if not (v.rs_spy and v.rs_spy > 0 and v.rs_10w and v.rs_10w > 0 and v.rs_10d and v.rs_10d > 0):
            continue
        yoy = _to_float((v.parsed.get("yoy") or "").replace("高", "").replace("低", ""))
        if yoy is not None and yoy > 300:
            continue  # 低基数虚高
        if v.metrics and v.metrics.chg >= 15:
            continue  # 单日已过热
        out.append(v)
    return out


def _attention_rows(data: ReportData) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for v in data.today_main:
        p = v.parsed
        m = v.metrics
        if v.rs_rating is not None and v.rs_rating < 50:
            problems = [f"**RS Rating {int(v.rs_rating)}**（全市场后段）"]
            if v.rs_spy is not None and v.rs_spy < 0:
                problems.append(f"RS/SPY **{_x(v.rs_spy)}**（长期跑输大盘）")
            if v.sector_rank:
                problems.append(f"板块排名 **{v.sector_rank}**")
            if m and m.px_vs_sma is not None:
                problems.append(f"价格仅高于 SMA150 **{m.px_vs_sma:.2f}%**")
            rows.append((v.symbol, "；".join(problems), "🛑 技术形态成立但**全市场强度垫底** → 小仓 ≤ 总资金 3%，或不参与"))
        elif v.rs_rating is not None and v.rs_rating < 70:
            rows.append((v.symbol, f"RS Rating **{int(v.rs_rating)}**（中性档）"
                        + (f"，RS/SPY {_x(v.rs_spy)}" if v.rs_spy is not None else "")
                        + (f"，RS-10W {_x(v.rs_10w)}" if v.rs_10w is not None else ""),
                        "🟡 相对强度平庸 → 小仓 ≤5%"))

    for v in data.today_main:
        yoy = _to_float((v.parsed.get("yoy") or "").replace("高", "").replace("低", ""))
        if yoy is not None and 0 < yoy < 10:
            rows.append((v.symbol, f"EPS 同比只 **+{yoy:.1f}%**（正增长里最低的一档）",
                         "🟡 基本面与强度背离 → 仓位取中间档"))
        if v.parsed.get("eps") is not None and v.parsed["eps"] < 0:
            rows.append((v.symbol, f"EPS **{_money(v.parsed['eps'])} 仍亏损**，\"亏损收窄\" ≠ 盈利增长",
                         "⚠️ 强度可以，但**不是基本面驱动** → 强势股里风险最低的一档"))
        if v.metrics and v.metrics.chg >= 10:
            rows.append((v.symbol, f"当日已 **{_pct(v.metrics.chg)}**，偏离均线较多"
                         + (f"（高于 SMA150 {v.metrics.px_vs_sma:.2f}%）" if v.metrics.px_vs_sma else ""),
                         "🟡 **不要在次日开盘直接追**，等回踩不破止损再进，或改用小仓（≤5%）分批"))

    for v in data.history_cross + data.history_main:
        m = v.metrics
        if m and v.stop and m.px < v.stop:
            rows.append((f"{v.symbol}（{v.date}）",
                         f"现价 **{_money(m.px)}**，已低于 {_money(v.stop)} 止损 **{_f(abs((m.px / v.stop - 1) * 100))}%**",
                         "🛑 **信号已失效** —— 不要按原止损挂单，也不要因为 RS 强就重新抄底"))
    return rows


def _watch_attention(data: ReportData) -> list[WatchView]:
    return [w for w in data.watch if w.bucket in ("red", "yellow", "nodata")]


def _expiring(data: ReportData, within_days: int = 7) -> list[WatchView]:
    return [
        w for w in data.watch
        if w.expires_in_days is not None and 0 <= w.expires_in_days <= within_days
    ]


def _position_grade(v: SignalView) -> tuple[str, str]:
    """返回 (档位标记, 建议)。规则写在报告里，用户能对着看。"""
    m = v.metrics
    chg = m.chg if m else 0.0
    if v.rs_rating is None:
        return "⚠️", "RS 算不出 → 小仓 ≤5%"
    if v.rs_rating < 50:
        return "⚠️", "RS 弱 → 小仓 ≤3%，或不参与"
    if v.rs_rating < 70:
        return "⚠️", "RS 中性 → 小仓 ≤5%"
    if v.rs_rating >= 85 and chg <= 5.0 and (v.parsed.get("eps") or 0) > 0:
        return "⭐⭐⭐", "RS ≥85 且当日涨幅小 → 标准仓"
    if v.rs_rating >= 80:
        return "⭐⭐", "RS ≥80 → 标准仓以下"
    if chg >= 8.0:
        return "⭐", f"已涨 {_pct(chg)} → 回踩进"
    return "⭐", "RS 中强 → 标准仓以下"


# --------------------------------------------------------------------------
# 主渲染
# --------------------------------------------------------------------------
def build_user_report(
    date_str: str,
    signals_csv: Path,
    watchlist_path: Path,
    cache_dir: Path,
    universe_path: Path | None = None,
    days_back: int = 7,
    today: date | None = None,
) -> str:
    """生成用户版日报（Markdown 全文）。

    date_str       标日 YYYY-MM-DD
    signals_csv    信号流水 CSV
    watchlist_path 监控池 JSON
    cache_dir      行情 pkl 目录
    universe_path  rs_universe.json（公司名 / 板块），可为 None
    days_back      「过去 N 天」窗口
    today          用于算监控池到期天数，默认取 date_str
    """
    today = today or date.fromisoformat(date_str)
    data = _collect(date_str, signals_csv, watchlist_path, Path(cache_dir),
                    universe_path, days_back, today)
    return render_markdown(data, today)


def render_markdown(data: ReportData, today: date) -> str:
    d = data
    out: list[str] = []
    w = out.append

    n_today = len(d.today_main)
    n_cross = len(d.today_cross)
    data_day = d.data_day or "—"
    weekday = "一二三四五六日"[today.weekday()]

    w(f"# Weinstein + EPS 信号日报 — {d.date_str}（用户版）")
    w("")
    w(f"> **生成时间**：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}（本地时区）")
    w(f"> **报告范围**：今日 ({d.date_str}) + 过去 {7} 天")
    w(f"> **策略**：Weinstein Stage 2 + EPS 季度同比/环比 双确认")
    w(f"> **基准**：SPY（长期 RS 加权 63/126/189/252 日）")
    w(f"> **数据状态**：行情至 **{data_day}** 收盘；标日 {d.date_str} = 执行日（周{weekday}）")
    if d.rs_pool.get("count"):
        w(f"> **RS 排名池**：{d.rs_pool.get('count')} 只"
          + (f"（签名 `{d.rs_pool.get('pool_sig')}`）" if d.rs_pool.get("pool_sig") else ""))
    w("")
    w("---")
    w("")

    # ---------------- 今日概览 ----------------
    w("## 🚨 今日概览")
    w("")
    syms = " / ".join(v.symbol for v in d.today_main)
    w(f"**{d.date_str}（周{weekday}）{n_today} 个新信号（Weinstein + EPS 主策略），{n_cross} 个 EMA10 回踩。**")
    w("")
    if d.today_main:
        w(f"- ✅ **v_eps_v2 主策略**：**{n_today} 个新增** —— {syms}")
    else:
        w("- ✅ **v_eps_v2 主策略**：**0 个新增**")
    if d.today_cross:
        w(f"- 🟤 **EMA10 回踩子策略**：**{n_cross} 个新增** —— "
          + " / ".join(f"{v.symbol} ★{v.strength}" for v in d.today_cross))
    else:
        last = d.history_cross[0].date if d.history_cross else "—"
        w(f"- 🟤 **EMA10 回踩子策略**：**0 个新增**（最近一次回踩是 {last}）")
    if d.stale_symbols:
        w(f"- ⚠️ **数据滞后**：{len(d.stale_symbols)} 只标的最后一根 K 线不是 {data_day}："
          + " / ".join(d.stale_symbols))
    else:
        w(f"- ✅ **数据完整性**：{n_today} 只标的最后一根 K 线均为 **{data_day}**，**无滞后**")
    buckets = _bucket_counts(d.watch)
    w(f"- 🔄 **监控池**：{len(d.watch)} 只 —— 🟢 {buckets['green']} / 🟡 {buckets['yellow']} / "
      f"🔴 {buckets['red']}" + (f" / ⚪ {buckets['nodata']}" if buckets.get("nodata") else ""))
    w("")
    w("---")
    w("")

    # ---------------- 段 1 ----------------
    w("## 📊 段 1：完整信号表（v_eps_v2 主策略，今日 + 过去 7 天）")
    w("")
    w("| 信号日期 | 股票 | 强度 | 参考止损 | 最新季度 EPS | EPS 同比 (YoY) | EPS 环比 (QoQ) "
      "| 连续同比季度 | 连续环比季度 | 量比 (vs/20日均量) | RS Rating | RS/SPY (长期) "
      "| RS-10W (10周) | RS-10D (10日) | 板块 | 板块排名 | 状态 |")
    w("|---------|------|------|----------|-------------|---------------|---------------|-------------|"
      "-------------|------------------|-----------|----------------|----------------|---------------|"
      "--------|---------|------|")
    for v in d.today_main:
        w(_main_row(v))
    for v in d.history_main:
        w(_history_row(v))
    if not d.today_main and not d.history_main:
        w("| — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **窗口内无信号** |")
    w("")
    w("**汇总**：")
    by_date: dict[str, int] = {}
    for v in d.today_main + d.history_main:
        by_date[v.date] = by_date.get(v.date, 0) + 1
    w("- " + "｜".join(f"{k}：**{v} 个**" for k, v in sorted(by_date.items(), reverse=True))
      if by_date else "- 窗口内无信号")
    w("- **今日待执行清单**："
      + (f"**{len(d.today_main)} 只全列**（见「开盘操作建议」）" if d.today_main else "**0 只**"))
    w("")
    w("> **注 1**：「连续环比季度」为空 = 该股**没有连续 2 个季度环比增长**（只有单季环比为正），不代表数据缺失。")
    w("> **注 2**：今日行的**量比是按交易日重算的**；历史行的量比沿用当时入库的值（未按新口径重算），"
      "两行不可直接比。")
    w("> **注 3**：RS 四列全是「—」= **算不出来**（不是弱）—— 通常是可用日线不足 253 根门槛，没进 RS 排名池。")
    w("")

    # ---------------- 段 1.5 ----------------
    w("## 📊 段 1.5：EMA10 回踩子策略（watchlist）")
    w("")
    w("### 今日新增")
    w("")
    w("| 信号日期 | 股票 | 强度 | 现价 | 触发收盘 | 上穿涨幅 | 量比 | 参考止损 | 状态 |")
    w("|---------|------|------|------|----------|----------|------|----------|------|")
    if d.today_cross:
        for v in d.today_cross:
            w(_cross_today_row(v))
    else:
        w(f"| **{d.date_str}** | **— 无新增 —** | — | — | — | — | — | — | ⚪ 今日无回踩 |")
    w("")
    if d.history_cross:
        w("### 过去 7 天 watchlist 回踩")
        w("")
        w("| 回踩日 | 股票 | 强度 | 触发收盘 | 上穿涨幅 | 量比 | 参考止损 | 现价 | 状态 |")
        w("|--------|------|------|----------|----------|------|----------|------|------|")
        for v in d.history_cross:
            w(_cross_history_row(v))
        w("")

    # ---------------- 段 2 ----------------
    w("## 📚 段 2：字段说明")
    w("")
    w("| 字段 | 含义 | 怎么看 |")
    w("|------|------|--------|")
    for name, meaning, usage in FIELD_GLOSSARY:
        w(f"| **{name}** | {meaning} | {usage} |")
    w("")
    w(MONITOR_LEGEND)
    w("")
    w("### RS Rating 判读标准（IBD 口径）")
    w("")
    w("| 区间 | 等级 | 含义 | 今日命中 |")
    w("|------|------|------|----------|")
    for band, level, meaning in RS_BANDS:
        hits = [v.symbol for v in d.today_main if _rs_band(v.rs_rating) == band]
        hit_text = "、".join(hits) + (f"（**{len(hits)} 只**）" if hits else "（0 只）")
        w(f"| {band} | {level} | {meaning} | {hit_text} |")
    w("")
    w("> **RS Rating ≠ RS/SPY**：RS/SPY 只跟 SPY 比（比值无上限），RS Rating 跟全市场比（封顶 99），"
      "后者才说明\"排第几\"。")
    w("")

    # ---------------- 段 3 ----------------
    w("## 🚦 段 3：看盘建议")
    w("")
    w(f"> **今日 {n_today} 个信号，按\"能不能打\"分三档。**")
    w("")

    strong = _strong_combo(d)
    w("### 🟢 **最强组合**（RS ≥ 80 且三 RS 全面为正）")
    w("")
    w("| 股票 | 信号日 | RS Rating | RS/SPY | RS-10W | RS-10D | 板块排名 | 量比 | 现价 | 建议 |")
    w("|------|--------|-----------|--------|--------|--------|----------|------|------|------|")
    if strong:
        for v in strong:
            m = v.metrics
            note = []
            if (v.parsed.get("eps") or 0) < 0:
                note.append("EPS 仍为负 → 标准仓以下")
            if m and m.chg >= 5:
                note.append(f"当日已 {_pct(m.chg)} → 等回踩")
            if v.parsed.get("qoq_streak") and v.parsed["qoq_streak"] >= 4:
                note.append(f"连续 {v.parsed['qoq_streak']} 季 EPS 环比增长 = 基本面最干净")
            grade, advice = _position_grade(v)
            w(f"| **{v.symbol}** | **{v.date}** | **{int(v.rs_rating)}** | **{_x(v.rs_spy)}** | "
              f"**{_x(v.rs_10w)}** | **{_x(v.rs_10d)}** | {v.sector} **{v.sector_rank}** | "
              f"{_vol_cell(v, recompute=True)} | **{_money(m.px if m else None)}** | "
              f"{grade} {advice}" + ("｜" + "；".join(note) if note else "") + " |")
    else:
        w("| — | — | — | — | — | — | — | — | — | 今日无 |")
    w("")

    attention = _attention_rows(d)
    w("### ⚠️ **注意**（需谨慎对待）")
    w("")
    w("| 股票 | 问题 | 建议 |")
    w("|------|------|------|")
    for sym, problem, advice in attention:
        w(f"| **{sym}** | {problem} | {advice} |")
    if not attention:
        w("| — | 今日无特别需要注意的标的 | — |")
    w("")

    ranked = sorted(
        [v for v in d.today_main if v.metrics and v.metrics.vol_ratio],
        key=lambda v: -v.metrics.vol_ratio,
    )[:8]
    w("### ⚡ **量能最强**（按量比排序）")
    w("")
    w("| 排名 | 股票 | 量比 | 当日涨幅 | RS Rating | 备注 |")
    w("|------|------|------|----------|-----------|------|")
    for i, v in enumerate(ranked, 1):
        m = v.metrics
        note = []
        if v.rs_rating is not None and v.rs_rating < 60:
            note.append("量能达标但强度弱")
        if m.chg >= 10:
            note.append("已大涨，追高风险大")
        if v.parsed.get("qoq") and "+" in str(v.parsed["qoq"]):
            note.append(f"EPS 环比 {v.parsed['qoq']} 配合放量")
        w(f"| {i} | **{v.symbol}** | **{m.vol_ratio:.1f}x** | {_pct(m.chg)} | "
          f"{int(v.rs_rating) if v.rs_rating is not None else '—'} | {'；'.join(note) or '—'} |")
    if not ranked:
        w("| — | — | — | — | — | 今日无信号 |")
    w("")

    w("### 📉 大盘背景（仅报告，不拦截信号）")
    w("")
    w("| 指标 | 数值 | 较昨日 |")
    w("|------|------|----------|")
    spy = d.market.get("spy")
    for sym in BENCHMARKS:
        m = d.market["benchmarks"].get(sym)
        if m:
            w(f"| {sym} 收盘（{m.last_day}） | **{_money(m.px)}** | **{_pct(m.chg)}** |")
    if spy:
        w(f"| SPY 5 日 | **{_pct(spy.r5)}** | |")
        w(f"| SPY 10 日 | **{_pct(spy.r10)}** | |")
        w(f"| SPY 20 日 | **{_pct(spy.r20)}** | |")
        w(f"| SPY 量比 | **{spy.vol_ratio:.2f}x** | "
          f"{'放量杀跌' if spy.chg < 0 else '放量上涨' if spy.chg > 0 else ''} |")
        if spy.sma150:
            w(f"| SPY vs SMA150（{_money(spy.sma150)}） | **{_pct(spy.px_vs_sma)}** | "
              f"{'已跌破均线' if spy.px_vs_sma < 0 else '仍在均线上'} |")
        at_low = abs(spy.px - spy.low30) < 0.01 and abs(spy.px - spy.low60) < 0.01
        w(f"| SPY 位置 | **{'30 日最低 = 60 日最低 = 今日收盘' if at_low else f'30 日低 {_money(spy.low30)}（{_pct((spy.px / spy.low30 - 1) * 100)}）'}** | "
          f"{'收在最低点' if at_low else ''} |")
    w("")
    w("> 这是**背景信息，不是开关** —— 按既定原则，任何市场状态都不拦截、不抑制信号。")
    w("")

    # ---------------- 监控池 ----------------
    w("## 📋 监控池状态（Watchlist）")
    w("")
    buckets = _bucket_counts(d.watch)
    w("| 指标 | 数值 |")
    w("|------|------|")
    w(f"| 当前监控股票数 | **{len(d.watch)}** |")
    w(f"| 🟢 现价高于止损 3% 以上 | **{buckets['green']}** |")
    w(f"| 🟡 贴近止损（3% 内） | **{buckets['yellow']}** |")
    w(f"| 🔴 已破止损 | **{buckets['red']}** |")
    if buckets.get("nodata"):
        w(f"| ⚪ 无行情数据 | **{buckets['nodata']}** |")
    xb = [v for v in d.watch if v.crossback_count > 0]
    w(f"| 有过 EMA10 回踩的股票 | **{len(xb)}** |")
    w(f"| 今日新触发回踩 | **{len(d.today_cross)}** |")
    w("")
    attention_watch = _watch_attention(d)
    if attention_watch:
        w("### 需要处理的监控条目")
        w("")
        w("| 股票 | 现价 | 参考止损 | 相对止损 | 回踩次数 | 最近回踩 | 到期 | 状态 |")
        w("|--------|------|----------|----------|----------|----------|------|------|")
        order = {"red": 0, "yellow": 1, "nodata": 2}
        for v in sorted(attention_watch, key=lambda x: (order.get(x.bucket, 3), x.rel if x.rel is not None else 0)):
            if v.bucket == "red":
                state = "🔴 已破止损"
            elif v.bucket == "yellow":
                state = "🟡 贴止损"
            else:
                state = "⚪ 无行情数据"
            w(f"| **{v.symbol}** | {_money(v.px)} | {_money(v.stop)} | "
              f"{_pct(v.rel) if v.rel is not None else '—'} | {v.crossback_count} | "
              f"{v.last_crossback_date or '—'} | {v.expires_at[:10] if v.expires_at else '—'} | {state} |")
        w("")
    expiring = _expiring(d)
    if expiring:
        w("### 7 天内到期")
        w("")
        for v in sorted(expiring, key=lambda x: x.expires_in_days):
            w(f"- **{v.symbol}** — {v.expires_at[:10]}（剩 {v.expires_in_days} 天）")
        w("")

    # ---------------- 操作建议 ----------------
    w(f"## 🔔 {d.date_str} 美股开盘操作建议")
    w("")
    if d.today_main:
        w("> 规则透明：**RS ≥85 且当日涨幅 ≤5% 且 EPS 为正 → 标准仓（⭐⭐⭐）**；"
          "**RS ≥80 → 标准仓以下（⭐⭐）**；**RS 70~79 → 标准仓以下 / 回踩进（⭐）**；"
          "**RS <70 → 小仓 ≤5%（⚠️）**；**RS <50 → 小仓 ≤3% 或不参与（⚠️）**。")
        w("")
        w("| 优先级 | 股票 | 现价 | 参考止损 | 止损幅度 | 量比 | RS | 建议 |")
        w("|--------|------|------|----------|----------|------|----|------|")
        for v in d.today_main:
            m = v.metrics
            grade, advice = _position_grade(v)
            if m and m.chg >= 8 and grade.startswith("⭐"):
                advice = f"已涨 {_pct(m.chg)} → 等回踩不破止损再进"
            w(f"| {grade} | **{v.symbol}** | {_money(m.px if m else None)} | {_money(v.stop)} | "
              f"{_f(v.stop_pct) + '%' if v.stop_pct is not None else '—'} | "
              f"{_vol_cell(v, recompute=True)} | "
              f"{int(v.rs_rating) if v.rs_rating is not None else '—'} | {advice} |")
        w("")
    for v in d.history_main:
        m = v.metrics
        if m and v.stop and m.px < v.stop:
            w(f"| 🛑 | **{v.symbol}**（{v.date} 未执行） | {_money(m.px)} | {_money(v.stop)} | — | — | "
              f"{int(v.rs_rating) if v.rs_rating is not None else '—'} | **已破止损，信号作废，不要抄** |")
    w("")

    # ---------------- caveat ----------------
    w("## ⚠️ 数据 caveat（重要）")
    w("")
    caveats: list[str] = []
    caveats.append(
        "**⚠️ 口径变更 —— 信号数量不可与历史直接比较**：行情 pkl 里同一个交易日经常有两根 bar"
        "（00:00 UTC 合并量 + 05:00 UTC IEX）。原实现按**行数**切 30 日整理区、20 日均量、SMA150，"
        "导致均量被合并量抬高近 20 倍（**量比被压到 0.8x → 真信号被误杀**）、枢轴点被压低（**假突破混入**）。"
        "现已全部改为**按交易日**计算（保留每天时间戳最晚的那根 bar）。"
        "→ **信号数量暴增是修复的结果，不是行情突然变好。**"
    )
    if data_day != d.date_str:
        caveats.append(
            f"**信号日期 ≠ 数据日期**：本次行情截止 **{data_day}** 收盘，标日是 UTC 运行日 {d.date_str}。"
            "下一个美股开盘即可执行。"
        )
    if d.rs_pool.get("count"):
        caveats.append(
            f"**RS 排名池**：{d.rs_pool.get('count')} 只"
            + (f"，签名 `{d.rs_pool.get('pool_sig')}`" if d.rs_pool.get("pool_sig") else "")
            + "。RS 只做**强度参考**，不做择时。"
        )
    mismatches = _sector_mismatches(d)
    if mismatches:
        caveats.append(
            "**⚠️ 板块归属来自 Nasdaq screener 分类，不是严格 GICS**：与 Finnhub 行业交叉校验后，"
            + "、".join(f"{s}（报 {old}，实为 {new}）" for s, old, new in mismatches)
            + " 归类可疑。**板块排名的分母按 screener 分类统计**，这几只的\"板块排名\"要打折看。"
        )
    else:
        caveats.append(
            "**板块归属来自 Nasdaq screener 分类**，不是严格 GICS；板块排名的分母按这个分类统计。"
        )
    caveats.append("**成交量是 IEX-only**：量比是**相对指标**可用，绝对股数不能当流动性依据。")
    vix_path = PROJECT_ROOT / "data/cache/^VIX.pkl"
    if vix_path.exists():
        try:
            vix_df = dedup_daily_bars(pd.read_pickle(vix_path))
            vix_day = str(pd.Timestamp(vix_df.index[-1]).date())
            caveats.append(f"**VIX 数据陈旧**：`data/cache/^VIX.pkl` 最后 K 线停在 {vix_day}，本报告**不报 VIX 数值**。")
        except Exception:
            pass
    else:
        caveats.append("**VIX 不可用**：本地没有可用的 VIX 行情，本报告**不报 VIX 数值**。")
    caveats.append("**「连续环比季度」空白 ≠ 数据缺失**：表示该股没有连续 2 季环比增长，只有单季为正。")
    caveats.append(
        "**历史行未按新口径重算**：段 1 里今日行的量比/均线/突破是按交易日重算的；"
        "历史行沿用当时入库的值，两种口径不可直接比。"
    )
    for i, text in enumerate(caveats, 1):
        w(f"{i}. {text}")
    w("")

    # ---------------- 风险提示 ----------------
    w("---")
    w("")
    w(f"> **风险提示**：以上均为系统自动识别的技术信号，不构成投资建议。信号日期为策略检测日，"
      f"建议**{d.date_str} 美股开盘**执行。**止损单务必在买入后立即挂好**"
      f"（参考止损 = 信号日收盘 × 0.92）。单笔仓位不超过总资金 20%，破止损立即离场，"
      f"不要因为 RS 强而扛单。")
    w("")
    return "\n".join(out)


def _bucket_counts(watch: list[WatchView]) -> dict:
    counts = {"green": 0, "yellow": 0, "red": 0, "nodata": 0, "nostop": 0}
    for v in watch:
        counts[v.bucket] = counts.get(v.bucket, 0) + 1
    return counts


def _sector_mismatches(data: ReportData) -> list[tuple[str, str, str]]:
    """用 Finnhub 行业（sector_cache）交叉校验 Nasdaq screener 的板块归属。"""
    try:
        from data.rs import INDUSTRY_TO_GICS  # noqa: PLC0415
    except Exception:
        return []
    out: list[tuple[str, str, str]] = []
    for v in data.today_main:
        info = data.sectors.get(v.symbol)
        if not isinstance(info, dict):
            continue
        industry = info.get("industry")
        if not industry:
            continue
        mapped = INDUSTRY_TO_GICS.get(industry)
        if mapped and v.sector and mapped != v.sector:
            out.append((v.symbol, v.sector, mapped))
    return out


# --------------------------------------------------------------------------
# 可直接贴进对话的精简版
# --------------------------------------------------------------------------
def build_paste_block(data: ReportData) -> str:
    """只给「信号表 + 三档建议 + 大盘一行」。"""
    lines: list[str] = []
    lines.append(f"**信号表（今日 {len(data.today_main)} 个）**")
    lines.append("")
    lines.append("| 股票 | 强度 | 止损 | EPS | YoY | QoQ | 量比 | RS | RS/SPY | 10W | 10D | 板块 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in data.today_main:
        p = v.parsed
        ratio = f"{v.metrics.vol_ratio:.1f}x" if v.metrics and v.metrics.vol_ratio else "—"
        lines.append(
            f"| **{v.symbol}** | {_stars(v.strength)} | {_money(v.stop)} | {_money(p.get('eps'))} | "
            f"{p.get('yoy') or '—'} | {p.get('qoq') or '—'} | {ratio} | "
            f"{int(v.rs_rating) if v.rs_rating is not None else '—'} | {_x(v.rs_spy)} | "
            f"{_x(v.rs_10w)} | {_x(v.rs_10d)} | {v.sector or '—'} |"
        )
    lines.append("")
    strong = _strong_combo(data)
    lines.append("**最强组合**：" + ("、".join(f"{v.symbol}（RS {int(v.rs_rating)}）" for v in strong)
                                    if strong else "无"))
    lines.append("")
    attention = _attention_rows(data)
    lines.append("**⚠️ 注意**：" + ("；".join(f"{s}：{p}" for s, p, _ in attention[:6])
                                    if attention else "无"))
    lines.append("")
    spy = data.market.get("spy")
    if spy:
        lines.append(
            f"**大盘**：SPY {_money(spy.px)}（{_pct(spy.chg)}，{spy.last_day}）"
            f"，量比 {spy.vol_ratio:.2f}x"
            + (f"，距 SMA150 {_pct(spy.px_vs_sma)}" if spy.px_vs_sma is not None else "")
        )
    else:
        lines.append("**大盘**：SPY 行情不可用")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _default_paths() -> dict:
    out = PROJECT_ROOT / "output/v_eps_v2"
    return {
        "signals_csv": out / "signals.csv",
        "watchlist_path": out / "watchlist.json",
        "cache_dir": PROJECT_ROOT / "data/cache",
        "universe_path": PROJECT_ROOT / "data/rs_universe.json",
        "out_dir": out,
    }


def main(argv: list[str] | None = None) -> int:
    defaults = _default_paths()
    parser = argparse.ArgumentParser(description="生成用户版日报")
    parser.add_argument("--date", default=date.today().isoformat(), help="标日 YYYY-MM-DD")
    parser.add_argument("--signals", default=str(defaults["signals_csv"]))
    parser.add_argument("--watchlist", default=str(defaults["watchlist_path"]))
    parser.add_argument("--cache-dir", default=str(defaults["cache_dir"]))
    parser.add_argument("--universe", default=str(defaults["universe_path"]))
    parser.add_argument("--out-dir", default=str(defaults["out_dir"]))
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--quiet", action="store_true", help="不打印可贴片段落")
    args = parser.parse_args(argv)

    date_str = args.date
    today = date.fromisoformat(date_str)
    data = _collect(date_str, Path(args.signals), Path(args.watchlist), Path(args.cache_dir),
                    Path(args.universe), args.days_back, today)
    md = render_markdown(data, today)

    out_dir = Path(args.out_dir) / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"报告_用户版_Weinstein + EPS_{date_str}.md"
    out_file.write_text(md, encoding="utf-8")
    print(f"✅ 已写出：{out_file}")
    print(f"   今日主策略 {len(data.today_main)} 个 / 回踩 {len(data.today_cross)} 个 / "
          f"监控池 {len(data.watch)} 只")
    if not args.quiet:
        # 每天只贴三样 —— 直接打印，字段说明/caveat/监控池明细留在 md 里
        print()
        print(build_paste_block(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
