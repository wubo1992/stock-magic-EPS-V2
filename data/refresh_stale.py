"""
data/refresh_stale.py
每小时 cron 调用，检查并补下载缺失/过期数据。

用法（手动）：
    uv run python data/refresh_stale.py

Crontab 设置（每小时执行）：
    0 * * * * cd /Users/wubo/Desktop/信号系统克劳德V3.1_Minimax支线/signal_system && uv run python data/refresh_stale.py >> data/refresh_stale.log 2>&1
"""

import sys
import time
import pickle
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 path 里
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.fetcher import _load_local, _save_local, _fetch_via_yahoo
from universe.manager import _read_universe_md


def get_stale_symbols():
    """返回所有数据不新鲜（>3天）或缺失的股票。"""
    symbols = _read_universe_md()
    stale = []
    now = datetime.now(tz=timezone.utc)
    for sym in symbols:
        df = _load_local(sym)
        if df is None or df.empty:
            stale.append(sym)
        else:
            last = df.index[-1]
            if last.tzinfo is None:
                last = last.tz_localize("UTC")
            age = (now - last).days
            if age > 3:
                stale.append(sym)
    return stale


def refresh_stale():
    """尝试补下载所有不新鲜的股票。"""
    stale = get_stale_symbols()
    if not stale:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 无需更新")
        return

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 发现 {len(stale)} 只股票数据不新鲜，尝试下载...")
    print(f"  列表: {stale}")

    # Yahoo Finance 目前对这个 IP 全面 403，换 alpaca 试试非美股
    # 先按 market_data 逻辑分离美股/非美股
    us_stale = [s for s in stale if "." not in s or s.endswith(".PK")]
    intl_stale = [s for s in stale if s not in us_stale]

    success = []
    failed = []

    # 非美股暂时无法下载（Yahoo 403），记录一下
    if intl_stale:
        print(f"  [警告] 非美股 {len(intl_stale)} 只暂时无法下载（Yahoo Finance 403限流）: {intl_stale}")

    # 尝试用 Alpaca 增量更新（美股用 Alpaca 已有缓存不会过期）
    from data.fetcher import _fetch_via_alpaca
    from datetime import timedelta

    end_date = datetime.now(tz=timezone.utc)
    start_date = end_date - timedelta(days=30)  # 只补最近30天

    if us_stale:
        result = _fetch_via_alpaca(us_stale, start_date, end_date)
        for sym, df in result.items():
            if df is not None and not df.empty:
                # 合并到本地
                existing = _load_local(sym)
                if existing is not None and not existing.empty:
                    merged = pd.concat([existing, df])
                    merged = merged[~merged.index.duplicated(keep="last")]
                    merged = merged.sort_index()
                    _save_local(sym, merged)
                else:
                    _save_local(sym, df)
                success.append(sym)
            else:
                failed.append(sym)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 完成: 成功 {len(success)}, 失败 {len(failed)}")
    if failed:
        print(f"  失败: {failed}")
    return success, failed


if __name__ == "__main__":
    refresh_stale()
