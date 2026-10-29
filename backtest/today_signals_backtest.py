"""
today_signals_backtest.py — 针对今日（2026-08-14）触发的 10 个买入信号，
对每只股票独立做"同类信号历史回测"，统计 1d/5d/10d/20d 的胜率与平均收益。

逻辑：
  1. 加载今日 10 个信号
  2. 对每只股票，调出全历史 OHLCV（data/cache/{symbol}.pkl）
  3. 按 v_eps_v2 策略的逐日条件（SMA150上升 + 价格 > SMA + 突破30日 + 量1.5x + EPS条件）扫描
  4. 对每个历史触发日，计算 1d/5d/10d/20d 的前向收益（次日开盘价 / N日后收盘价）
  5. 汇总：信号数、各周期胜率（>0 为胜）、平均收益、最大回撤

注：EPS 条件使用缓存中"最近一次报告"的 EPS 数据为代理（简化处理）；
    如需严格 point-in-time，需重写 EPS 历史时序模块。
"""
import sys
sys.path.insert(0, '.')

import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ──────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
EPS_CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

# 今日（2026-08-14）触发的 10 个买入信号
TODAY_SIGNALS = [
    ("CIX",  29.41),
    ("CRDL",  1.57),
    ("FDBC", 50.67),
    ("FMNB", 15.08),
    ("MLSS",  0.50),
    ("MNSB", 23.18),
    ("RBRK", 96.68),
    ("RRGB",  9.12),
    ("VABK", 45.92),
    ("VEEV", 232.32),
]

# Stage 2 参数（来自 config.yaml）
SMA_LONG = 150
TREND_LOOKBACK = 30
PIVOT_LOOKBACK = 30
MIN_BREAKOUT_PCT = 0.005
VOLUME_MULT = 1.5

# 前向收益周期
HORIZONS = [1, 5, 10, 20]

# 回测区间（避免把刚上市的早期冷启动算进来；至少要 SMA_LONG + PIVOT_LOOKBACK 天）
START_DATE = "2020-01-01"


# ──────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────
def load_ohlcv(symbol: str) -> pd.DataFrame | None:
    pkl = CACHE_DIR / f"{symbol}.pkl"
    if not pkl.exists():
        return None
    try:
        with open(pkl, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    # 标准化：索引为 UTC DatetimeIndex，列小写
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    cols = {c: c.lower() for c in df.columns}
    df = df.rename(columns=cols)
    needed = {"open", "high", "low", "close", "volume"}
    if not needed.issubset(df.columns):
        return None
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def load_eps_filter(symbol: str) -> tuple[bool, dict]:
    """
    检查该股票今日是否能通过 EPS 增长筛选。
    返回 (是否通过, 详细)。
    """
    pkl = EPS_CACHE_DIR / f"eps_{symbol}.pkl"
    if not pkl.exists():
        return False, {"reason": "no_eps_cache"}
    try:
        with open(pkl, "rb") as f:
            data = pickle.load(f)
    except Exception:
        return False, {"reason": "eps_cache_read_error"}
    # 实际格式：{'eps_history': [(quarter_str, eps_value), ...]}
    if not isinstance(data, dict):
        return False, {"reason": "unexpected_eps_format"}
    hist = data.get("eps_history", [])
    if not hist or len(hist) < 2:
        return False, {"reason": "insufficient_eps_history"}
    # eps_history 倒序存放：[0]=最新季度, [-1]=最旧季度
    # 取最新 4 个季度：用索引 [0..3]（最新）, 去年同季 [4], 上季度 [1]
    if len(hist) >= 5:
        latest = float(hist[0][1])
        yoy_ago = float(hist[4][1])
        qoq_ago = float(hist[1][1])
        yoy_pass = (yoy_ago < 0 and latest < 0 and abs(latest) < abs(yoy_ago)) or \
                   (yoy_ago < 0 and latest >= 0) or \
                   (yoy_ago >= 0 and latest > yoy_ago)
        qoq_pass = (qoq_ago < 0 and latest < 0 and abs(latest) < abs(qoq_ago)) or \
                   (qoq_ago < 0 and latest >= 0) or \
                   (qoq_ago >= 0 and latest > qoq_ago)
        return (yoy_pass and qoq_pass), {
            "latest_eps": latest,
            "yoy_eps": yoy_ago,
            "qoq_eps": qoq_ago,
            "yoy_pass": yoy_pass,
            "qoq_pass": qoq_pass,
        }
    elif len(hist) >= 2:
        latest = float(hist[0][1])
        qoq_ago = float(hist[1][1])
        qoq_pass = (qoq_ago < 0 and latest < 0 and abs(latest) < abs(qoq_ago)) or \
                   (qoq_ago < 0 and latest >= 0) or \
                   (qoq_ago >= 0 and latest > qoq_ago)
        return qoq_pass, {
            "latest_eps": latest,
            "qoq_eps": qoq_ago,
            "qoq_pass": qoq_pass,
            "yoy_pass": None,
        }
    return False, {"reason": "no_yoy_data"}


# ──────────────────────────────────────────────────────────────
# Stage 2 触发判定（向量化）
# ──────────────────────────────────────────────────────────────
def find_stage2_signals(df: pd.DataFrame) -> pd.DatetimeIndex:
    """
    在全历史 OHLCV 上向量化找出所有 Stage 2 触发日。
    返回 DatetimeIndex。
    """
    close = df["close"]
    volume = df["volume"]

    sma150 = close.rolling(SMA_LONG).mean()

    # 1. SMA150 当前 > 30天前（向上趋势确认）
    sma_past = sma150.shift(TREND_LOOKBACK)
    cond_trend = sma150 > sma_past

    # 2. 价格 > SMA150
    cond_above = close > sma150

    # 3. 突破 30 日整理区高点
    rolling_max = close.shift(1).rolling(PIVOT_LOOKBACK).max()
    cond_breakout = close >= rolling_max * (1 + MIN_BREAKOUT_PCT)

    # 4. 量能 >= 1.5x 20日均量
    avg_vol = volume.shift(1).rolling(20).mean()
    cond_vol = (volume >= avg_vol * VOLUME_MULT) & (avg_vol > 0)

    all_pass = cond_trend & cond_above & cond_breakout & cond_vol
    return df.index[all_pass.fillna(False)]


# ──────────────────────────────────────────────────────────────
# 单只股票回测
# ──────────────────────────────────────────────────────────────
def backtest_one(symbol: str, reference_stop: float) -> dict:
    df = load_ohlcv(symbol)
    if df is None:
        return {"symbol": symbol, "error": "no_ohlcv"}

    # 区间过滤：2020-01-01 起；并预留足够的回看窗口
    cutoff = pd.Timestamp(START_DATE, tz="UTC")
    df = df[df.index >= cutoff]
    if len(df) < SMA_LONG + PIVOT_LOOKBACK + 25:
        return {"symbol": symbol, "error": "insufficient_history"}

    eps_pass, eps_info = load_eps_filter(symbol)
    if not eps_pass:
        return {
            "symbol": symbol,
            "error": f"eps_fail: {eps_info.get('reason', eps_info)}",
        }

    # 找出所有 Stage 2 触发日
    sig_dates = find_stage2_signals(df)
    sig_dates = sig_dates[sig_dates >= cutoff]
    # 排除今天（避免 look-ahead）
    today = pd.Timestamp("2026-08-14", tz="UTC")
    sig_dates = sig_dates[sig_dates < today]

    # 过滤：距上一信号至少 5 个交易日（避免同一突破连触）
    sig_dates_filt = []
    last_dt = None
    for d in sig_dates:
        if last_dt is None or (d - last_dt).days >= 7:
            sig_dates_filt.append(d)
            last_dt = d
    sig_dates = pd.DatetimeIndex(sig_dates_filt)

    if len(sig_dates) == 0:
        return {"symbol": symbol, "error": "no_historical_signals"}

    # 计算每个触发日的 1d/5d/10d/20d 收益
    rows = []
    for d in sig_dates:
        idx = df.index.get_loc(d)
        entry = float(df["close"].iloc[idx])
        row = {"date": d.date().isoformat(), "entry": entry}
        for h in HORIZONS:
            target_idx = idx + h
            if target_idx >= len(df):
                row[f"ret_{h}d"] = None
                row[f"win_{h}d"] = None
                row[f"mae_{h}d"] = None
                continue
            # T+1 开盘价买入（更现实）；持有 h 天后 T+h 收盘价卖出
            if h == 1:
                buy_price = float(df["open"].iloc[idx + 1])
            else:
                buy_price = float(df["open"].iloc[idx + 1])
            sell_price = float(df["close"].iloc[idx + h])
            ret = sell_price / buy_price - 1
            # 期间最大不利变动（从买入后的最高价到最低价的最大跌幅）
            window = df.iloc[idx + 1: idx + h + 1]
            if len(window) > 0:
                high_in = float(window["high"].max())
                low_in = float(window["low"].min())
                mae = buy_price / high_in - 1  # 期间最大浮亏
            else:
                mae = 0
            row[f"ret_{h}d"] = ret
            row[f"win_{h}d"] = int(ret > 0)
            row[f"mae_{h}d"] = mae
        rows.append(row)

    df_res = pd.DataFrame(rows)
    result = {
        "symbol": symbol,
        "reference_stop": reference_stop,
        "signal_count": len(df_res),
        "first_signal": df_res["date"].iloc[0],
        "last_signal": df_res["date"].iloc[-1],
        "eps_info": eps_info,
    }
    for h in HORIZONS:
        col = f"ret_{h}d"
        valid = df_res[col].dropna()
        if len(valid) == 0:
            result[f"win_rate_{h}d"] = None
            result[f"avg_ret_{h}d"] = None
            result[f"median_ret_{h}d"] = None
            result[f"best_{h}d"] = None
            result[f"worst_{h}d"] = None
            result[f"avg_mae_{h}d"] = None
            continue
        result[f"win_rate_{h}d"] = float((valid > 0).mean())
        result[f"avg_ret_{h}d"] = float(valid.mean())
        result[f"median_ret_{h}d"] = float(valid.median())
        result[f"best_{h}d"] = float(valid.max())
        result[f"worst_{h}d"] = float(valid.min())
        mae_col = f"mae_{h}d"
        result[f"avg_mae_{h}d"] = float(df_res[mae_col].dropna().mean())
    return result


# ──────────────────────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────────────────────
def main():
    print(f"回测区间：{START_DATE} ~ 2026-08-13")
    print(f"今日触发：{len(TODAY_SIGNALS)} 个信号\n")

    results = []
    for symbol, ref_stop in TODAY_SIGNALS:
        print(f"  → {symbol} ... ", end="", flush=True)
        r = backtest_one(symbol, ref_stop)
        if "error" in r:
            print(f"✗ {r['error']}")
        else:
            print(f"✓ {r['signal_count']} 个历史信号")
        results.append(r)

    print()
    print("=" * 120)
    print(f"{'股票':<6} {'信号数':>5} {'首触日':<10} {'末触日':<10} "
          f"{'WR 1d':>7} {'AvgR 1d':>9} {'WR 5d':>7} {'AvgR 5d':>9} "
          f"{'WR 10d':>8} {'AvgR 10d':>10} {'WR 20d':>8} {'AvgR 20d':>10}")
    print("-" * 120)
    for r in results:
        if "error" in r:
            print(f"{r['symbol']:<6} ✗ {r['error']}")
            continue
        def fmt(v, p=4):
            return f"{v*100:>{p+2}.1f}%" if v is not None else "  N/A"
        def fmt_n(v, p=1):
            return f"{v:>{p+1}.1f}" if v is not None else " N/A"
        print(
            f"{r['symbol']:<6} {r['signal_count']:>5} {r['first_signal']:<10} {r['last_signal']:<10} "
            f"{fmt(r.get('win_rate_1d')):>7} {fmt(r.get('avg_ret_1d')):>9} "
            f"{fmt(r.get('win_rate_5d')):>7} {fmt(r.get('avg_ret_5d')):>9} "
            f"{fmt(r.get('win_rate_10d')):>8} {fmt(r.get('avg_ret_10d')):>10} "
            f"{fmt(r.get('win_rate_20d')):>8} {fmt(r.get('avg_ret_20d')):>10}"
        )
    print("=" * 120)


if __name__ == "__main__":
    main()