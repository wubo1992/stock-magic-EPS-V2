"""
data/fmp_financials.py — Financial Modeling Prep API 财务数据模块

FMP API 提供完整的财务报表数据：
- 资产负债表：现金、应收账款、存货、流动负债、总负债、总资产
- 利润表：净利润、营收
- 现金流量表：经营现金流

免费层限制：每天 250 次请求，足够回测使用

API Key: acLxQjyVgy8PE5cZNxsMxQYcXMrCmBoc
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

FMP_API_KEY = os.getenv("FMP_API_KEY", "acLxQjyVgy8PE5cZNxsMxQYcXMrCmBoc")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(symbol: str, data_type: str) -> Path:
    """缓存文件路径"""
    return CACHE_DIR / f"fmp_{data_type}_{symbol}.pkl"


def _is_cache_valid(path: Path, max_age_days: int = 30) -> bool:
    """财务数据相对稳定，缓存30天有效"""
    if not path.exists():
        return False
    file_age = datetime.now().timestamp() - path.stat().st_mtime
    return file_age < (max_age_days * 86400)


def get_balance_sheet(symbol: str, year: Optional[int] = None) -> dict:
    """
    获取资产负债表

    返回：
        {
            'cash': float,
            'accounts_receivable': float,
            'inventory': float,
            'current_liabilities': float,
            'total_liabilities': float,
            'total_assets': float,
            'fiscal_year': str,
            'fiscal_end_month': int,
            'error': str/None
        }
    """
    cache_path = _cache_path(symbol, "balance_sheet")

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"[FMP BS] {symbol} 从缓存加载")
            return cached
        except Exception:
            pass

    # 从 FMP API 获取
    url = f"{FMP_BASE_URL}/balance-sheet-statement/{symbol}"
    params = {
        "apikey": FMP_API_KEY,
        "limit": 5,  # 最近5年
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}'}

        data = resp.json()
        if not data or isinstance(data, dict) and data.get("Error"):
            err_msg = data.get("Error", "No data") if isinstance(data, dict) else "No data"
            return {'error': err_msg}

        # 取最新一年的数据
        latest = data[0]

        result = {
            'cash': float(latest.get('cashAndCashEquivalents', 0) or 0),
            'accounts_receivable': float(latest.get('netReceivables', 0) or 0),
            'inventory': float(latest.get('inventory', 0) or 0),
            'current_liabilities': float(latest.get('currentLiabilities', 0) or 0),
            'total_liabilities': float(latest.get('totalLiabilities', 0) or 0),
            'total_assets': float(latest.get('totalAssets', 0) or 0),
            'fiscal_year': latest.get('fillingDate', '')[:4] if latest.get('fillingDate') else str(year or datetime.now().year),
            'fiscal_end_month': int(latest.get('date', '')[:2]) if latest.get('date') else 12,
            'is_liquidating': False,
            'has_audit_issues': False,
            'error': None,
        }

        # 缓存
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
            print(f"[FMP BS] {symbol} 已缓存")
        except Exception as e:
            print(f"[FMP BS] {symbol} 缓存失败: {e}")

        return result

    except Exception as e:
        return {'error': str(e)}


def get_income_statement(symbol: str) -> dict:
    """
    获取利润表（最近4季度 TTM）

    返回：
        {
            'net_income': float,
            'revenue': float,
            'net_income_ttm': float,
            'revenue_ttm': float,
            'pe_ratio': float,
            'error': str/None
        }
    """
    cache_path = _cache_path(symbol, "income")

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"[FMP Income] {symbol} 从缓存加载")
            return cached
        except Exception:
            pass

    url = f"{FMP_BASE_URL}/income-statement/{symbol}"
    params = {
        "apikey": FMP_API_KEY,
        "limit": 4,  # 最近4季度
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}'}

        data = resp.json()
        if not data or isinstance(data, dict) and data.get("Error"):
            err_msg = data.get("Error", "No data") if isinstance(data, dict) else "No data"
            return {'error': err_msg}

        # 计算 TTM（最近4个季度之和）
        net_income_ttm = 0
        revenue_ttm = 0
        for item in data[:4]:
            ni = item.get('netIncome', 0) or 0
            rev = item.get('revenue', 0) or 0
            if ni:
                net_income_ttm += ni
            if rev:
                revenue_ttm += rev

        # 最新一年的数据
        latest = data[0]

        # 计算 P/E（需要市值）
        # 市值需要单独获取
        market_cap = get_market_cap(symbol)
        pe_ratio = 0
        if market_cap > 0 and net_income_ttm > 0:
            pe_ratio = market_cap / net_income_ttm

        result = {
            'net_income': float(latest.get('netIncome', 0) or 0),
            'revenue': float(latest.get('revenue', 0) or 0),
            'net_income_ttm': net_income_ttm,
            'revenue_ttm': revenue_ttm,
            'pe_ratio': pe_ratio,
            'error': None,
        }

        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
            print(f"[FMP Income] {symbol} 已缓存")
        except Exception as e:
            print(f"[FMP Income] {symbol} 缓存失败: {e}")

        return result

    except Exception as e:
        return {'error': str(e)}


def get_cash_flow(symbol: str) -> dict:
    """
    获取现金流量表

    返回：
        {
            'operating_cash_flow': float,
            'operating_cash_flow_ttm': float,
            'error': str/None
        }
    """
    cache_path = _cache_path(symbol, "cash_flow")

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"[FMP CF] {symbol} 从缓存加载")
            return cached
        except Exception:
            pass

    url = f"{FMP_BASE_URL}/cash-flow-statement/{symbol}"
    params = {
        "apikey": FMP_API_KEY,
        "limit": 4,
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}'}

        data = resp.json()
        if not data or isinstance(data, dict) and data.get("Error"):
            err_msg = data.get("Error", "No data") if isinstance(data, dict) else "No data"
            return {'error': err_msg}

        # 计算 TTM
        operating_cf_ttm = 0
        for item in data[:4]:
            ocf = item.get('operatingCashFlow', 0) or 0
            if ocf:
                operating_cf_ttm += ocf

        result = {
            'operating_cash_flow': float(data[0].get('operatingCashFlow', 0) or 0),
            'operating_cash_flow_ttm': operating_cf_ttm,
            'error': None,
        }

        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
            print(f"[FMP CF] {symbol} 已缓存")
        except Exception as e:
            print(f"[FMP CF] {symbol} 缓存失败: {e}")

        return result

    except Exception as e:
        return {'error': str(e)}


def get_market_cap(symbol: str) -> float:
    """获取市值"""
    cache_path = _cache_path(symbol, "market_cap")

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            return cached.get('market_cap', 0)
        except Exception:
            pass

    url = f"{FMP_BASE_URL}/profile/{symbol}"
    params = {"apikey": FMP_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return 0

        data = resp.json()
        if not data or isinstance(data, dict) and data.get("Error"):
            return 0

        mcap = data[0].get('mktCap', 0)
        if mcap:
            try:
                with open(cache_path, "wb") as f:
                    pickle.dump({'market_cap': float(mcap)}, f)
            except Exception:
                pass
            return float(mcap)

    except Exception:
        pass

    return 0


def get_full_financials(symbol: str) -> dict:
    """
    获取完整财务数据

    返回：
        {
            'balance_sheet': dict,
            'income': dict,
            'cash_flow': dict,
            'ncav': float,
            'p_ncav': float,
            'market_cap': float,
            'error': str/None
        }
    """
    bs = get_balance_sheet(symbol)
    if bs.get('error'):
        return {'error': bs['error']}

    inc = get_income_statement(symbol)
    cf = get_cash_flow(symbol)
    market_cap = get_market_cap(symbol)

    # 计算 NCAV
    ncav = calculate_ncav(bs)
    p_ncav = market_cap / ncav if ncav > 0 else float('inf')

    # 计算 P/B
    pb = market_cap / bs['total_assets'] if bs['total_assets'] > 0 else 0

    # 合并数据
    result = {
        'balance_sheet': bs,
        'income': inc,
        'cash_flow': cf,
        'ncav': ncav,
        'p_ncav': p_ncav,
        'pb_ratio': pb,
        'pe_ratio': inc.get('pe_ratio', 0),
        'market_cap': market_cap,
        'net_income_ttm': inc.get('net_income_ttm', 0),
        'operating_cash_flow_ttm': cf.get('operating_cash_flow_ttm', 0),
        'revenue_ttm': inc.get('revenue_ttm', 0),
        'error': None,
    }

    return result


def calculate_ncav(balance_sheet: dict) -> float:
    """
    计算格雷厄姆 NCAV

    公式：NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 流动负债
    """
    cash = balance_sheet.get('cash', 0)
    ar = balance_sheet.get('accounts_receivable', 0)
    inventory = balance_sheet.get('inventory', 0)
    current_liabilities = balance_sheet.get('current_liabilities', 0)

    ncav = cash + (0.75 * ar) + (0.5 * inventory) - current_liabilities
    return ncav


if __name__ == "__main__":
    # 测试
    for symbol in ['AAPL', 'MSFT']:
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
            print(f"NCAV: ${result['ncav']:,.0f}")
            print(f"P/NCAV: {result['p_ncav']:.2f}")
            print(f"P/E: {result['pe_ratio']:.2f}")
            print(f"P/B: {result['pb_ratio']:.2f}")
            print(f"Market Cap: ${result['market_cap']/1e9:.2f}B")
            print(f"Net Income TTM: ${result['net_income_ttm']/1e9:.2f}B")
            print(f"OCF TTM: ${result['operating_cash_flow_ttm']/1e9:.2f}B")

        time.sleep(0.5)  # 防限流
