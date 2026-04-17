"""
precache_eps.py — EPS 数据批量预缓存

按优先级下载所有股票的 EPS 历史数据（近10年，约40个季度）。
优先顺序：美股 → 港股 → 台股

用法：
    uv run python precache_eps.py

Finnhub 免费版限制：60次/分钟
~773 只股票约需 13 分钟（每分钟处理 60 只）
"""

import os
import re
import sys
import time
import pickle
import requests
from pathlib import Path
from datetime import datetime, timezone

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

CACHE_DIR = Path(__file__).parent / "data" / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _eps_cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"eps_{symbol}.pkl"


def _is_cached(symbol: str) -> bool:
    return _eps_cache_path(symbol).exists()


def _fetch_eps(symbol: str) -> list:
    """从 Finnhub 获取 EPS 季度历史数据"""
    url = f"{FINNHUB_BASE_URL}/stock/metric"
    params = {"symbol": symbol, "metric": "eps", "token": FINNHUB_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("error"):
            return []
        series_quarterly = data.get("series", {}).get("quarterly", {})
        eps_data = series_quarterly.get("eps", [])
        if not eps_data:
            return []
        eps_history = []
        for item in eps_data:
            period_raw = item.get("period", "")
            eps_val = item.get("v")
            if not period_raw or eps_val is None:
                continue
            try:
                eps = float(eps_val)
                if eps == 0:
                    continue
                # 转换 "2025-12-27" -> "2025-Q4"
                parts = period_raw.split("-")
                year = parts[0]
                month = int(parts[1])
                quarter = (month - 1) // 3 + 1
                period_str = f"{year}-Q{quarter}"
                eps_history.append((period_str, eps))
            except (ValueError, TypeError, IndexError):
                continue
        # 按时间倒序
        def sort_key(item):
            try:
                y, q = item[0].split("-Q")
                return (int(y), int(q))
            except:
                return (0, 0)
        eps_history.sort(key=sort_key, reverse=True)
        return eps_history
    except Exception as e:
        return []


def _save_eps(symbol: str, eps_history: list) -> bool:
    try:
        with open(_eps_cache_path(symbol), "wb") as f:
            pickle.dump({"eps_history": eps_history}, f)
        return True
    except Exception:
        return False


def get_universe_symbols():
    """从 UNIVERSE.md 读取股票池，按优先级排序"""
    content = Path("UNIVERSE.md").read_text()
    HEADER_ROWS = {'CODE', 'STOCK', 'SYMBOL', 'TICKER', '板块', '名称', '标签'}

    us_stocks = []
    hk_stocks = []
    tw_stocks = []

    in_hk = False
    in_tw = False

    for line in content.split('\n'):
        if '## 港股' in line:
            in_hk = True
            in_tw = False
            continue
        if '## 台股' in line:
            in_tw = True
            in_hk = False
            continue
        if '## ' in line and line.startswith('## '):
            in_hk = False
            in_tw = False

        m = re.match(r'\|\s*([A-Za-z0-9.\-]+)\s*\|', line)
        if not m:
            continue
        code = m.group(1).strip()
        if not code or code in HEADER_ROWS or code.startswith('-'):
            continue

        if code.endswith('.HK'):
            hk_stocks.append(code)
        elif code.endswith('.TW'):
            tw_stocks.append(code)
        elif not any(c.isdigit() for c in code):
            us_stocks.append(code)

    # 优先级：美股 → 港股 → 台股
    return us_stocks + hk_stocks + tw_stocks


def main():
    print("=" * 60)
    print("EPS 批量预缓存（Finnhub）")
    print("=" * 60)

    if not FINNHUB_API_KEY:
        print("[错误] 未配置 FINNHUB_API_KEY")
        return

    symbols = get_universe_symbols()
    total = len(symbols)
    print(f"股票池共 {total} 只（美股 {len([s for s in symbols if not '.' in s])} + 港股 {len([s for s in symbols if '.HK' in s])} + 台股 {len([s for s in symbols if '.TW' in s])})")

    # 统计已缓存和待下载
    to_download = []
    already_cached = []
    for sym in symbols:
        if _is_cached(sym):
            already_cached.append(sym)
        else:
            to_download.append(sym)

    print(f"已缓存: {len(already_cached)} 只")
    print(f"待下载: {len(to_download)} 只")
    if not to_download:
        print("\n所有股票 EPS 已缓存完毕！")
        return

    print(f"\n开始下载（Finnhub 60次/分钟，约需 {len(to_download) // 60 + 1} 分钟）...")
    print("-" * 40)

    success = 0
    failed = 0
    no_data = 0
    last_log = time.time()

    for i, sym in enumerate(to_download):
        now = time.time()

        # 每分钟结束时短暂暂停（防止超过60次/分钟限制）
        # 我们每分钟处理约60只，所以每只间隔刚好约1秒
        time.sleep(1.1)  # 略多于1秒，留余量

        print(f"[{i+1}/{len(to_download)}] {sym}...", end=" ", flush=True)

        eps_history = _fetch_eps(sym)

        if not eps_history:
            print("无数据")
            no_data += 1
            continue

        if _save_eps(sym, eps_history):
            quarters = len(eps_history)
            years = quarters / 4
            print(f"✅ {quarters}季度(约{years:.1f}年)")
            success += 1
        else:
            print("❌ 保存失败")
            failed += 1

        # 每30秒打印进度
        if now - last_log >= 30:
            elapsed = now - (to_download.index(sym) if sym in to_download else 0)
            rate = (i + 1) / ((now - time.time() + now) / 60) if i > 0 else 0
            remaining = len(to_download) - i - 1
            mins_left = remaining / 60
            print(f"\n  进度: {i+1}/{len(to_download)} | 剩余约 {mins_left:.0f} 分钟")
            last_log = now

    print("\n" + "=" * 60)
    print(f"下载完成！")
    print(f"  成功: {success} 只")
    print(f"  无数据: {no_data} 只")
    print(f"  失败: {failed} 只")
    print(f"  已缓存: {len(already_cached)} 只")
    print("=" * 60)


if __name__ == "__main__":
    main()
