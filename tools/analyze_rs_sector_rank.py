#!/usr/bin/env python3
"""
tools/analyze_rs_sector_rank.py — RS/SPY + 同板块排名批量回算 CLI

用法：
  # 单只或多只 ticker
  uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --tickers WOLF,AMD,ADI

  # 从 signals.csv 读取当日 ticker
  uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --from-csv

  # 仅 RS/SPY，不写缓存（dry run）
  uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --tickers WOLF,AMD --no-cache

数据源：
- 价格：data/cache/{ticker}.pkl
- SPY：data/cache/SPY.pkl
- 板块：data/sector_cache.json（Finnhub 缓存）
- RS 日级缓存：data/rs_daily_cache.json

输出：
- 控制台表格：ticker | RS/SPY | sector | rank/total
"""
import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT))

from data.rs import compute_rs_for_tickers, DEFAULT_SECTOR_CACHE, DEFAULT_RS_DAILY_CACHE
from data.fetcher import fetch  # noqa: E402

CACHE_DIR = ROOT / "data" / "cache"
SIGNALS_CSV = ROOT / "output" / "v_eps_v2" / "signals.csv"


def load_pkl(ticker: str, history_days: int = 730) -> pd.DataFrame | None:
    """从 pkl 缓存读 ticker 的日线数据，缺失时用 fetch 补。"""
    pkl_path = CACHE_DIR / f"{ticker}.pkl"
    if not pkl_path.exists():
        # Fallback: 用 fetcher 拉
        data = fetch([ticker], history_days=history_days, live_mode=True)
        if data and ticker in data:
            return data[ticker]
        return None
    try:
        return pd.read_pickle(pkl_path)
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RS/SPY + 同板块排名批量回算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date", required=True, help="回算日期 YYYY-MM-DD")
    p.add_argument(
        "--tickers",
        default=None,
        help="逗号分隔的 ticker 列表（如 WOLF,AMD,ADI）",
    )
    p.add_argument(
        "--from-csv",
        action="store_true",
        help=f"从 {SIGNALS_CSV} 读取当日 ticker",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="跳过 rs_daily_cache.json 写入",
    )
    p.add_argument(
        "--history-days",
        type=int,
        default=730,
        help="拉取历史数据的天数（默认 730，覆盖 252 + buffer）",
    )
    return p.parse_args()


def load_tickers_from_csv(date_str: str) -> list[str]:
    """从 signals.csv 读取指定日期的 ticker 列表。"""
    if not SIGNALS_CSV.exists():
        print(f"[错误] {SIGNALS_CSV} 不存在")
        return []
    tickers = set()
    with open(SIGNALS_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("日期", "") == date_str and row.get("信号", "") == "买入":
                t = row.get("股票", "").strip()
                if t:
                    tickers.add(t)
    return sorted(tickers)


def main() -> None:
    args = parse_args()
    date_str = args.date

    # 1. 解析 ticker 列表
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    elif args.from_csv:
        tickers = load_tickers_from_csv(date_str)
        if not tickers:
            print(f"[错误] {date_str} 在 {SIGNALS_CSV} 中无买入信号")
            sys.exit(1)
    else:
        print("[错误] 必须指定 --tickers 或 --from-csv")
        sys.exit(1)

    print(f"[1/4] {len(tickers)} 只 ticker：{', '.join(tickers)}")
    print(f"      回算日期：{date_str}")

    # 2. 加载 SPY + ticker 行情
    market_data = {"SPY": load_pkl("SPY", args.history_days)}
    for t in tickers:
        market_data[t] = load_pkl(t, args.history_days)

    if market_data["SPY"] is None:
        print("[错误] 无法加载 SPY 数据")
        sys.exit(1)

    print(f"[2/4] 数据加载完成（{len([k for k in market_data if market_data[k] is not None])}/{len(market_data)}）")

    # 3. 计算 RS
    rs_cache_path = None if args.no_cache else DEFAULT_RS_DAILY_CACHE
    sector_cache_path = DEFAULT_SECTOR_CACHE

    print(f"[3/4] 计算 RS...")
    results = compute_rs_for_tickers(
        tickers=tickers,
        as_of_date=date_str,
        market_data=market_data,
        sector_cache_path=sector_cache_path,
        rs_daily_cache_path=rs_cache_path,
    )

    # 4. 输出
    print()
    print("=" * 100)
    print(f"RS/SPY + 同板块排名 — {date_str}")
    print(f"入场计算：0.4×R63 + 0.2×R126 + 0.2×R189 + 0.2×R252")
    print("=" * 100)
    print(f"{'#':<3} {'Ticker':<7} {'RS/SPY':>8} {'板块':<32} {'排名':>10}")
    print("-" * 100)

    # 按 RS/SPY 降序
    sortable = []
    for t in tickers:
        r = results.get(t)
        sortable.append((t, r))
    sortable.sort(key=lambda x: -(x[1].rs_spy if x[1] else -999))

    for i, (t, r) in enumerate(sortable, 1):
        if r is None:
            print(f"{i:<3} {t:<7} {'-':>8} {'-':<32} {'-':>10}")
        else:
            rank_str = f"{r.sector_rank}/{r.sector_total}" if r.sector_rank else "-"
            sector_str = r.sector[:30] if r.sector else "-"
            print(f"{i:<3} {t:<7} {r.rs_spy:>8.2f} {sector_str:<32} {rank_str:>10}")

    # 5. 统计
    valid = [r for r in results.values() if r is not None]
    if valid:
        avg = sum(r.rs_spy for r in valid) / len(valid)
        print()
        print(f"完整计算：{len(valid)}/{len(tickers)}，平均 RS/SPY={avg:.2f}")
        best = max(valid, key=lambda r: r.rs_spy)
        worst = min(valid, key=lambda r: r.rs_spy)
        print(f"最强：{best.sector if best else '?'}  {best.rs_spy:.2f}")
        print(f"最弱：{worst.sector if worst else '?'}  {worst.rs_spy:.2f}")


if __name__ == "__main__":
    main()