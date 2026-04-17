#!/usr/bin/env python3
"""
批量获取 EPS 缓存

用法:
    uv run python scripts/batch_fetch_eps.py

说明:
    - Alpha Vantage 免费版: 500 次/天
    - 每次获取一只股票的全部历史 EPS（只算 1 次请求）
    - 976 只股票约需 2 天完成
"""

import time
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.fundamentals import get_eps_growth, _eps_cache_path

# 读取股票池
UNIVERSE_FILE = Path(__file__).parent.parent / "UNIVERSE.md"


def get_universe_symbols():
    """从 UNIVERSE.md 提取股票代码"""
    symbols = []
    with open(UNIVERSE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 匹配表格行: | AAPL | Apple Inc. | ...
            if line.startswith("| ") and not line.startswith("|---"):
                parts = line.split("|")
                if len(parts) >= 3:
                    symbol = parts[1].strip()
                    if symbol and symbol not in ["股票代码", "Symbol", "代码"]:
                        symbols.append(symbol)
    return symbols


def get_missing_symbols(all_symbols: list) -> list:
    """获取缺少 EPS 缓存的股票"""
    cached = set()
    for symbol in all_symbols:
        if _eps_cache_path(symbol).exists():
            cached.add(symbol)
    return [s for s in all_symbols if s not in cached]


def main():
    print("=" * 60)
    print("EPS 批量获取脚本")
    print("=" * 60)

    # 获取股票列表
    all_symbols = get_universe_symbols()
    print(f"股票池共 {len(all_symbols)} 只")

    missing = get_missing_symbols(all_symbols)
    print(f"缺少 EPS 缓存: {len(missing)} 只")

    if not missing:
        print("所有股票都已获取 EPS 缓存！")
        return

    print(f"\n开始获取 EPS 数据...")
    print(f"Alpha Vantage 限制: 5 次/分钟, 500 次/天")
    print(f"预计需要: {len(missing)} 次请求")
    print("-" * 60)

    success = 0
    failed = 0

    for i, symbol in enumerate(missing):
        print(f"[{i+1}/{len(missing)}] 获取 {symbol}...", end=" ")

        result = get_eps_growth(symbol, quarters_required=3)

        if result['has_data']:
            print(f"✓ EPS ${result['latest_eps']:.2f}, {result['quarters']}季度")
            success += 1
        elif result['error']:
            print(f"✗ {result['error']}")
            failed += 1
        else:
            print(f"✗ 无数据")
            failed += 1

        # Alpha Vantage 限制: 5 次/分钟
        # 每 12 秒请求一次，留出余量
        if i < len(missing) - 1:
            time.sleep(12)

    print("-" * 60)
    print(f"完成! 成功: {success}, 失败: {failed}")
    print(f"剩余未获取: {len(missing) - success - failed} 只")


if __name__ == "__main__":
    main()
