"""
download_universe.py
将 UNIVERSE.md 里的所有股票下载到本地缓存。

用法：
    uv run python download_universe.py

只下载本地缺失的股票，已有本地数据的直接跳过。
"""

import sys
import time
import pprint
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 path 里
sys.path.insert(0, str(Path(__file__).parent))

from data.fetcher import fetch, _load_local, _save_local
from universe.manager import _read_universe_md


def main():
    end_date = datetime(2026, 3, 25, tzinfo=timezone.utc)
    # 历史数据需要拉约 2300 天，确保覆盖 2020-2026
    history_days = 2300

    symbols = _read_universe_md()
    print(f"UNIVERSE.md 共 {len(symbols)} 只股票")
    print(f"目标区间: 2020-01-01 ~ 2026-03-25")

    us_stocks = [s for s in symbols if not s.endswith(".HK") and not s.endswith(".TW")]
    hk_stocks = [s for s in symbols if s.endswith(".HK")]
    tw_stocks = [s for s in symbols if s.endswith(".TW")]

    print(f"\n美股: {len(us_stocks)} 只")
    print(f"港股: {len(hk_stocks)} 只")
    print(f"台股: {len(tw_stocks)} 只")

    # 分批处理，每批 50 只
    batch_size = 50
    all_stocks = us_stocks + hk_stocks + tw_stocks

    start_date = datetime(2020, 1, 1, tzinfo=timezone.utc)

    # 先统计缺失
    missing = []
    for sym in all_stocks:
        df = _load_local(sym)
        if df is None:
            missing.append(sym)

    print(f"\n本地缺失需下载: {len(missing)} 只")
    print(f"  美股缺: {len([s for s in missing if not s.endswith(('.HK', '.TW'))])}")
    print(f"  港股缺: {len([s for s in missing if s.endswith('.HK')])}")
    print(f"  台股缺: {len([s for s in missing if s.endswith('.TW')])}")

    if not missing:
        print("\n全部股票本地已有数据，无需下载！")
        return

    print(f"\n开始分批下载({batch_size}只/批)...")

    success = 0
    failed = []
    skip_already = 0

    for i in range(0, len(missing), batch_size):
        batch = missing[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(missing) + batch_size - 1) // batch_size
        print(f"\n批 {batch_num}/{total_batches}: {len(batch)} 只...", end="", flush=True)

        result = fetch(batch, history_days=history_days, end_date=end_date)

        for sym in batch:
            if sym in result and result[sym] is not None and not result[sym].empty:
                success += 1
            else:
                failed.append(sym)

        print(f"  成功 {len([s for s in batch if s in result])}, 失败 {len([s for s in batch if s not in result])}")

        # Yahoo Finance 有请求限制，港股台股批量下载要慢
        if i + batch_size < len(missing):
            time.sleep(2)  # 避免触发 403

    print(f"\n{'='*50}")
    print(f"下载完成:")
    print(f"  成功: {success} 只")
    print(f"  失败: {len(failed)} 只")
    if failed:
        print(f"  失败股票: {failed[:20]}{'...' if len(failed)>20 else ''}")

    # 保存失败列表供后续重试
    if failed:
        with open("data/download_failed.txt", "w") as f:
            f.write("\n".join(failed))
        print(f"\n失败列表已保存: data/download_failed.txt")


if __name__ == "__main__":
    main()
