"""
data/polygon_financials.py — Polygon.io / Massive.com 财务数据模块

API Key 从 .env 的 POLYGON_API_KEY 读取（不要把密钥写进代码里）

数据端点：GET https://api.polygon.io/v2/reference/financials/{ticker}

返回完整财务数据：
- 资产负债表：现金、应收账款、存货、流动负债、总负债、总资产
- 利润表：净利润、营收
- 现金流量表：经营现金流

免费层级：5000次/月
"""

import os
import pickle
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(symbol: str, period: str = "annual") -> Path:
    """缓存文件路径"""
    return CACHE_DIR / f"polygon_financials_{symbol}_{period}.pkl"


def _is_cache_valid(path: Path, max_age_days: int = 7) -> bool:
    """财务数据缓存7天"""
    if not path.exists():
        return False
    file_age = datetime.now().timestamp() - path.stat().st_mtime
    return file_age < (max_age_days * 86400)


def get_financials(symbol: str, period: str = "quarterly") -> dict:
    """
    获取股票财务数据

    参数：
        symbol: 股票代码，如 "AAPL"
        period: "quarterly" 或 "annual"

    返回：
        {
            'balance_sheet': {
                'cash': float,
                'accounts_receivable': float,
                'inventory': float,
                'current_liabilities': float,
                'total_liabilities': float,
                'total_assets': float,
            },
            'income': {
                'net_income': float,
                'revenue': float,
            },
            'cash_flow': {
                'operating_cash_flow': float,
            },
            'ncav': float,
            'p_ncav': float,
            'market_cap': float,
            'pe_ratio': float,
            'pb_ratio': float,
            'error': str/None
        }
    """
    cache_path = _cache_path(symbol, period)

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"[Polygon] {symbol} 从缓存加载")
            return cached
        except Exception:
            pass

    if not POLYGON_API_KEY:
        return {'error': 'No API Key'}

    url = f"{POLYGON_BASE_URL}/v2/reference/financials/{symbol}"
    params = {
        "apiKey": POLYGON_API_KEY,
        "period": period,
        "pageSize": 5,  # 最近5期
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}

        data = resp.json()
        if data.get("status") != "OK":
            return {'error': f'API Error: {data.get("status")}'}

        results = data.get("results", [])
        if not results:
            return {'error': 'No financial data available'}

        # 取最新一期
        latest = results[0]

        # 提取资产负债表数据
        bs = {
            'cash': latest.get('cashAndEquivalents', 0) or 0,
            'accounts_receivable': latest.get('receivables', 0) or 0,
            'inventory': latest.get('inventory', 0) or 0,
            'current_liabilities': latest.get('liabilitiesCurrent', 0) or 0,
            'total_liabilities': latest.get('liabilities', 0) or 0,
            'total_assets': latest.get('assets', 0) or 0,
        }

        # 提取利润表数据
        income = {
            'net_income': latest.get('netIncome', 0) or 0,
            'revenue': latest.get('revenue', 0) or 0,
            'net_income_basic': latest.get('netIncomeBasic', 0) or latest.get('netIncome', 0) or 0,
        }

        # 提取现金流量表数据
        cf = {
            'operating_cash_flow': latest.get('operatingCashFlow', 0) or 0,
            'cashFlow': latest.get('cashFlow', 0) or 0,
        }

        # 计算 NCAV
        # NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 流动负债
        ncav = (bs['cash'] +
                0.75 * bs['accounts_receivable'] +
                0.5 * bs['inventory'] -
                bs['current_liabilities'])

        # 计算市值（需要从profile获取）
        market_cap = get_market_cap(symbol)
        if market_cap == 0:
            market_cap = bs['total_assets'] * 2  # 估算

        # 计算 P/NCAV
        p_ncav = market_cap / ncav if ncav > 0 else float('inf')

        # 计算 P/B
        pb_ratio = market_cap / bs['total_assets'] if bs['total_assets'] > 0 else 0

        # 计算 P/E
        pe_ratio = market_cap / income['net_income'] if income['net_income'] > 0 else 0

        result = {
            'balance_sheet': bs,
            'income': income,
            'cash_flow': cf,
            'ncav': ncav,
            'p_ncav': p_ncav,
            'pb_ratio': pb_ratio,
            'pe_ratio': pe_ratio,
            'market_cap': market_cap,
            'period': latest.get('period'),
            'report_date': latest.get('reportPeriod'),
            'fiscal_year': latest.get('fiscalYear'),
            'fiscal_period': latest.get('fiscalPeriod'),
            'error': None,
        }

        # 缓存
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
            print(f"[Polygon] {symbol} 已缓存")
        except Exception as e:
            print(f"[Polygon] {symbol} 缓存失败: {e}")

        return result

    except Exception as e:
        return {'error': str(e)}


def get_market_cap(symbol: str) -> float:
    """获取市值"""
    cache_path = _cache_path(symbol, "market_cap")

    if _is_cache_valid(cache_path, max_age_days=1):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            return cached.get('market_cap', 0)
        except Exception:
            pass

    if not POLYGON_API_KEY:
        return 0

    # 从 stocks ticker 获取
    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/prev"
    params = {"apiKey": POLYGON_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                # 用收盘价 * 流通股估算市值
                close_price = results[0].get('c', 0)
                # 获取流通股数
                shares = get_shares_outstanding(symbol)
                if shares > 0:
                    mcap = close_price * shares
                    with open(cache_path, "wb") as f:
                        pickle.dump({'market_cap': mcap, 'price': close_price, 'shares': shares}, f)
                    return mcap
    except Exception:
        pass

    return 0


def get_shares_outstanding(symbol: str) -> float:
    """获取流通股数"""
    cache_path = _cache_path(symbol, "shares")

    if _is_cache_valid(cache_path, max_age_days=7):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            return cached.get('shares', 0)
        except Exception:
            pass

    if not POLYGON_API_KEY:
        return 0

    # 从 financials 数据获取
    url = f"{POLYGON_BASE_URL}/v2/reference/financials/{symbol}"
    params = {"apiKey": POLYGON_API_KEY, "period": "quarterly", "pageSize": 1}

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                # weightedAverageByBasicShs 是流通股数
                shares = results[0].get('weightedAverageByBasicShs', 0) or results[0].get('weightedAverageShsOut', 0)
                if shares:
                    with open(cache_path, "wb") as f:
                        pickle.dump({'shares': shares}, f)
                    return shares
    except Exception:
        pass

    return 0


def get_full_financials(symbol: str) -> dict:
    """
    获取完整财务数据（季度 + 年度）
    """
    quarterly = get_financials(symbol, period="quarterly")
    if quarterly.get('error'):
        return quarterly

    # 计算 TTM
    annual = get_financials(symbol, period="annual")

    # TTM = 最近4个季度之和（需要从 quarterly 获取）
    # 这里简化：直接用最新的季度数据
    quarterly['ttm_note'] = "Using latest quarter as approximation"

    return quarterly


def calculate_ncav(balance_sheet: dict) -> float:
    """
    计算格雷厄姆 NCAV

    公式：NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 流动负债
    """
    cash = balance_sheet.get('cash', 0)
    ar = balance_sheet.get('accounts_receivable', 0)
    inventory = balance_sheet.get('inventory', 0)
    current_liabilities = balance_sheet.get('current_liabilities', 0)

    return cash + (0.75 * ar) + (0.5 * inventory) - current_liabilities


if __name__ == "__main__":
    # 测试
    for symbol in ['AAPL', 'MSFT', 'GM']:
        print(f"\n--- {symbol} ---")
        result = get_full_financials(symbol)
        if result.get('error'):
            print(f"Error: {result['error']}")
        else:
            bs = result['balance_sheet']
            print(f"Cash: ${bs.get('cash', 0):,.0f}")
            print(f"Accounts Receivable: ${bs.get('accounts_receivable', 0):,.0f}")
            print(f"Inventory: ${bs.get('inventory', 0):,.0f}")
            print(f"Current Liabilities: ${bs.get('current_liabilities', 0):,.0f}")
            print(f"Total Assets: ${bs.get('total_assets', 0):,.0f}")
            print(f"NCAV: ${result['ncav']:,.0f}")
            print(f"P/NCAV: {result['p_ncav']:.2f}")
            print(f"P/E: {result['pe_ratio']:.2f}")
            print(f"P/B: {result['pb_ratio']:.2f}")
            print(f"Market Cap: ${result['market_cap']/1e9:.2f}B")
            print(f"Period: {result.get('period')} | Report: {result.get('report_date')}")

        time.sleep(0.5)  # 防限流
