"""
fetch_russell2000_complete.py — Russell 2000 完整数据获取脚本

功能：
1. 批量获取 SEC EDGAR 财务数据（NCAV计算用）
2. 批量获取 NASDAQ 市值数据
3. 批量获取 Polygon 价格数据
4. 缓存所有数据供回测使用

运行时间：预计 2-4 小时（取决于网络和API限制）
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# 设置路径
CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(exist_ok=True)

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "tnCPh87aCFFoePifS6310VxRKTBiZUPO")


def load_russell2000() -> list[str]:
    """加载 Russell 2000 股票列表"""
    stocks = []
    with open("UNIVERSE_RUSSELL2000.md", "r") as f:
        for line in f:
            if line.startswith("|") and not line.startswith("|---"):
                parts = line.split("|")
                if len(parts) >= 3:
                    code = parts[1].strip()
                    if code and code.isupper() and 2 <= len(code) <= 5 and code.isalpha():
                        stocks.append(code)
    return stocks


def load_ticker_cik_map() -> dict:
    """加载 ticker -> CIK 映射"""
    cik_path = Path("/tmp/ticker_cik_map.json")
    if cik_path.exists():
        with open(cik_path) as f:
            return json.load(f)
    return {}


def fetch_sec_edgar(symbol: str, cik: str) -> dict:
    """获取单只股票的 SEC EDGAR 数据"""
    cache_file = CACHE_DIR / f"sec_edgar_{symbol}.json"
    if cache_file.exists():
        return {"symbol": symbol, "status": "cached"}

    headers = {"User-Agent": "Research Bot research@university.edu"}
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            data["symbol"] = symbol
            with open(cache_file, "w") as f:
                json.dump(data, f)
            return {"symbol": symbol, "status": "success"}
        else:
            return {"symbol": symbol, "status": "error", "code": resp.status_code}
    except Exception as e:
        return {"symbol": symbol, "status": "error", "msg": str(e)[:50]}


def fetch_nasdaq_market_caps_batch(symbols: list[str]) -> dict:
    """批量获取 NASDAQ 市值数据"""
    market_caps = {}

    # 尝试 NASDAQ API
    try:
        url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=60)

        if resp.status_code == 200:
            data = resp.json()
            for row in data.get("data", {}).get("rows", []):
                sym = row.get("symbol", "")
                mcap_str = row.get("marketCap", "0")

                # 转换市值字符串
                try:
                    if mcap_str.endswith("B"):
                        mcap = float(mcap_str[:-1]) * 1e9
                    elif mcap_str.endswith("M"):
                        mcap = float(mcap_str[:-1]) * 1e6
                    elif mcap_str.endswith("K"):
                        mcap = float(mcap_str[:-1]) * 1e3
                    else:
                        mcap = float(mcap_str)
                    market_caps[sym] = mcap
                except:
                    pass

            print(f"[NASDAQ] 成功获取 {len(market_caps)} 只股票市值")
            return market_caps

    except Exception as e:
        print(f"[NASDAQ] API 失败: {e}")

    return market_caps


def fetch_polygon_price(symbol: str, start_date: str, end_date: str) -> dict:
    """获取单只股票的价格数据"""
    cache_file = CACHE_DIR / f"{symbol}.pkl"

    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_KEY}"

    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                import pandas as pd
                df = pd.DataFrame(results)
                df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                df.set_index("date", inplace=True)
                df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
                df = df[["open", "high", "low", "close", "volume", "vwap"]]
                df.to_pickle(cache_file)
                return {"symbol": symbol, "status": "success", "bars": len(results)}
    except Exception as e:
        pass

    return {"symbol": symbol, "status": "error"}


def main():
    print("=" * 60)
    print("Russell 2000 完整数据获取")
    print("=" * 60)

    # 1. 加载股票列表
    stocks = load_russell2000()
    print(f"\n[1] Russell 2000: {len(stocks)} 只股票")

    # 2. 加载 CIK 映射
    ticker_cik = load_ticker_cik_map()
    print(f"[2] CIK 映射: {len(ticker_cik)} 条")

    # 3. 检查已有 SEC 缓存
    existing_sec = set(f.stem.replace("sec_edgar_", "") for f in CACHE_DIR.glob("sec_edgar_*.json"))
    print(f"[3] 已有 SEC 缓存: {len(existing_sec)} 只")

    # 4. 确定需要获取的股票
    to_fetch_sec = [s for s in stocks if s not in existing_sec and s in ticker_cik]
    print(f"[4] 需要获取 SEC 数据: {len(to_fetch_sec)} 只")

    # 5. 批量获取 SEC 数据
    print(f"\n[5] 开始获取 SEC EDGAR 数据...")
    print(f"    预计时间: {len(to_fetch_sec) * 0.2 / 60:.1f} 分钟 (限流 5 req/sec)")

    success = 0
    errors = 0

    for i, symbol in enumerate(to_fetch_sec):
        cik = ticker_cik.get(symbol, "")
        if not cik:
            errors += 1
            continue

        result = fetch_sec_edgar(symbol, cik)
        if result["status"] == "success":
            success += 1
        elif result["status"] == "error":
            errors += 1

        if (i + 1) % 100 == 0:
            print(f"    进度: {i+1}/{len(to_fetch_sec)}, 成功: {success}, 失败: {errors}")

        time.sleep(0.2)  # SEC 限流: 10req/sec, 我们用 5req/sec 留余量

    print(f"\n[5] SEC 获取完成: 成功 {success}, 失败 {errors}")
    print(f"    总 SEC 缓存: {len(list(CACHE_DIR.glob('sec_edgar_*.json')))} 只")

    # 6. 获取 NASDAQ 市值
    print(f"\n[6] 获取 NASDAQ 市值数据...")
    market_caps = fetch_nasdaq_market_caps_batch(stocks)
    print(f"    获取到 {len(market_caps)} 只股票市值")

    # 保存市值数据
    mcap_file = CACHE_DIR / "nasdaq_market_caps.json"
    with open(mcap_file, "w") as f:
        json.dump(market_caps, f, indent=2)
    print(f"    已保存到 {mcap_file}")

    # 7. 计算 NCAV 并筛选候选
    print(f"\n[7] 计算 NCAV...")

    def calc_ncav_from_sec(symbol: str) -> dict:
        """从 SEC 数据计算 NCAV"""
        cache_file = CACHE_DIR / f"sec_edgar_{symbol}.json"
        if not cache_file.exists():
            return None

        try:
            with open(cache_file) as f:
                data = json.load(f)

            us_gaap = data.get("facts", {}).get("us-gaap", {})

            def get_latest(tag):
                if tag in us_gaap:
                    units = us_gaap[tag].get("units", {})
                    if "USD" in units and units["USD"]:
                        return units["USD"][-1]["val"]
                return None

            cash = get_latest("CashAndCashEquivalentsAtCarryingValue") or get_latest("Cash")
            ar = get_latest("AccountsReceivable") or get_latest("AccountsReceivableNetCurrent")
            inventory = get_latest("Inventory") or get_latest("InventoryNet")
            curr_liab = get_latest("LiabilitiesCurrent")

            if cash and curr_liab and cash > 0 and curr_liab > 0:
                ncav = cash + 0.75 * (ar or 0) + 0.5 * (inventory or 0) - curr_liab
                if ncav > 0:
                    mcap = market_caps.get(symbol, 0)
                    if mcap > 0:
                        return {
                            "symbol": symbol,
                            "ncav": ncav,
                            "market_cap": mcap,
                            "p_ncav": mcap / ncav,
                        }
        except:
            pass
        return None

    ncav_candidates = []
    for symbol in list(market_caps.keys())[:500]:  # 先处理有市值的
        result = calc_ncav_from_sec(symbol)
        if result:
            ncav_candidates.append(result)

    # 按 P/NCAV 排序
    ncav_candidates.sort(key=lambda x: x["p_ncav"])

    print(f"    找到 {len(ncav_candidates)} 只有效 NCAV 候选")

    # 打印 Top 20
    print(f"\n    Top 20 深度价值候选:")
    for i, c in enumerate(ncav_candidates[:20]):
        print(f"    {i+1}. {c['symbol']}: P/NCAV={c['p_ncav']:.2f}, NCAV=${c['ncav']/1e9:.2f}B, MCap=${c['market_cap']/1e9:.2f}B")

    # 8. 获取价格数据（仅针对候选股票）
    print(f"\n[8] 获取价格数据（候选股票）...")

    # 候选股票：P/NCAV < 2.0 的深度价值股
    deep_value = [c["symbol"] for c in ncav_candidates if c["p_ncav"] < 2.0]
    print(f"    深度价值候选: {len(deep_value)} 只")

    start_date = "2025-01-01"  # 回测需要更长历史
    end_date = "2026-04-08"

    price_success = 0
    for i, symbol in enumerate(deep_value):
        cache_file = CACHE_DIR / f"{symbol}.pkl"
        if cache_file.exists():
            price_success += 1
            continue

        result = fetch_polygon_price(symbol, start_date, end_date)
        if result["status"] == "success":
            price_success += 1

        if (i + 1) % 50 == 0:
            print(f"    进度: {i+1}/{len(deep_value)}, 成功: {price_success}")

        time.sleep(0.05)  # Polygon 限流

    print(f"\n[8] 价格数据完成: {price_success}/{len(deep_value)} 只")

    # 9. 保存候选列表
    print(f"\n[9] 保存候选列表...")

    candidates_file = CACHE_DIR / "ncav_candidates.json"
    with open(candidates_file, "w") as f:
        json.dump(ncav_candidates, f, indent=2)

    summary_file = CACHE_DIR / "russell2000_summary.json"
    with open(summary_file, "w") as f:
        json.dump({
            "total_stocks": len(stocks),
            "sec_cached": len(list(CACHE_DIR.glob("sec_edgar_*.json"))),
            "with_market_cap": len(market_caps),
            "with_ncav": len(ncav_candidates),
            "deep_value_count": len(deep_value),
            "fetch_time": datetime.now().isoformat(),
        }, f, indent=2)

    print(f"\n{'='*60}")
    print("完成!")
    print(f"{'='*60}")
    print(f"Russell 2000 总数: {len(stocks)}")
    print(f"SEC 缓存: {len(list(CACHE_DIR.glob('sec_edgar_*.json')))}")
    print(f"市值数据: {len(market_caps)}")
    print(f"NCAV 候选: {len(ncav_candidates)}")
    print(f"深度价值股: {len(deep_value)}")
    print(f"\n候选文件: {candidates_file}")
    print(f"摘要文件: {summary_file}")


if __name__ == "__main__":
    main()
