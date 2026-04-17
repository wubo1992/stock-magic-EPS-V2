"""
data/sec_edgar.py — SEC EDGAR 财务数据模块

SEC EDGAR 提供完全免费的上市公司财务报表数据：
- 10-K（年度报告）：完整的资产负债表、利润表、现金流量表
- 10-Q（季度报告）：同上

数据格式：XBRL/JSON

优点：完全免费，数据最权威
缺点：需要解析 XBRL 格式，API 较慢

适合回测使用，因为数据稳定不变
"""

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(symbol: str, cik: str, form_type: str, year: int) -> Path:
    return CACHE_DIR / f"sec_edgar_{symbol}_{form_type}_{year}.json"


def _get_cik(symbol: str) -> Optional[str]:
    """获取股票的 CIK"""
    cache_path = CACHE_DIR / f"sec_cik_{symbol}.json"

    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
                return data.get('cik')
        except Exception:
            pass

    # 从 SEC Ticker API 获取
    url = f"https://www.sec.gov/files/company_tickers.json"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            # 格式: {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
            for cik_str, info in data.items():
                if info.get('ticker', '').upper() == symbol.upper():
                    cik = str(info['cik_str']).zfill(10)
                    # 缓存
                    with open(cache_path, "w") as f:
                        json.dump({'cik': cik, 'symbol': symbol}, f)
                    return cik
    except Exception as e:
        print(f"[SEC] 获取CIK失败: {e}")

    return None


def get_company_facts(symbol: str) -> dict:
    """
    获取公司的所有财务数据（从 company facts JSON）

    SEC 提供 pre-built JSON 文件，包含所有财务数据
    """
    cache_path = CACHE_DIR / f"sec_company_facts_{symbol}.json"

    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass

    cik = _get_cik(symbol)
    if not cik:
        return {'error': 'CIK not found'}

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    headers = {'User-Agent': 'Research Bot research@university.edu'}

    try:
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}'}

        data = resp.json()

        # 缓存
        with open(cache_path, "w") as f:
            json.dump(data, f)

        return data

    except Exception as e:
        return {'error': str(e)}


def parse_balance_sheet(company_facts: dict, year: Optional[int] = None) -> dict:
    """
    从 SEC company facts 解析资产负债表

    返回：
        {
            'cash': float,
            'accounts_receivable': float,
            'inventory': float,
            'current_liabilities': float,
            'total_liabilities': float,
            'total_assets': float,
            'fiscal_year': str,
            'error': str/None
        }
    """
    try:
        us Gaap = company_facts.get('facts', {}).get('us-gaap', {})

        # 定义需要的字段和 SEC 标签
        fields = {
            'cash': ['CashAndCashEquivalentsAtCarryingValue', 'CashAndEquivalentsAtCarryingValue'],
            'accounts_receivable': ['AccountsReceivable', 'Receivables'],
            'inventory': ['Inventory', 'Inventories'],
            'current_liabilities': ['CurrentLiabilities'],
            'total_liabilities': ['Liabilities'],
            'total_assets': ['Assets'],
        }

        result = {}
        for field, tags in fields.items():
            value = None
            for tag in tags:
                if tag in us Gaap:
                    # 获取最新的值
                    data_points = us Gaap[tag].get('units', {}).get('USD', [])
                    if data_points:
                        # 取最后一个（最新的）
                        value = data_points[-1].get('val', 0)
                        break
            result[field] = value or 0

        # 计算 NCAV
        ncav = (result['cash'] +
                0.75 * result['accounts_receivable'] +
                0.5 * result['inventory'] -
                result['current_liabilities'])

        return {
            'cash': result['cash'],
            'accounts_receivable': result['accounts_receivable'],
            'inventory': result['inventory'],
            'current_liabilities': result['current_liabilities'],
            'total_liabilities': result['total_liabilities'],
            'total_assets': result['total_assets'],
            'ncav': ncav,
            'fiscal_year': str(year) if year else 'latest',
            'error': None,
        }

    except Exception as e:
        return {'error': str(e)}


def get_income_ttm(company_facts: dict) -> dict:
    """
    获取 TTM 净利润和营收
    """
    try:
        us Gaap = company_facts.get('facts', {}).get('us-gaap', {})

        # 净利润
        net_income_tag = 'NetIncomeLoss'
        net_income_values = []
        if net_income_tag in us Gaap:
            for point in us Gaap[net_income_tag].get('units', {}).get('USD', []):
                net_income_values.append(point.get('val', 0))

        # 营收
        revenue_tag = 'Revenues'
        revenue_values = []
        if revenue_tag in us Gaap:
            for point in us Gaap[revenue_tag].get('units', {}).get('USD', []):
                revenue_values.append(point.get('val', 0))

        # 计算 TTM（最近4个季度）
        net_income_ttm = sum(net_income_values[:4]) if len(net_income_values) >= 4 else sum(net_income_values)
        revenue_ttm = sum(revenue_values[:4]) if len(revenue_values) >= 4 else sum(revenue_values)

        return {
            'net_income_ttm': net_income_ttm,
            'revenue_ttm': revenue_ttm,
            'net_income_latest': net_income_values[0] if net_income_values else 0,
            'revenue_latest': revenue_values[0] if revenue_values else 0,
        }

    except Exception as e:
        return {'error': str(e)}


if __name__ == "__main__":
    # 测试
    print("Testing SEC EDGAR API with AAPL...")

    facts = get_company_facts('AAPL')
    if facts.get('error'):
        print(f"Error: {facts['error']}")
    else:
        bs = parse_balance_sheet(facts)
        if bs.get('error'):
            print(f"Balance Sheet Error: {bs['error']}")
        else:
            print(f"Cash: ${bs.get('cash', 0):,.0f}")
            print(f"Accounts Receivable: ${bs.get('accounts_receivable', 0):,.0f}")
            print(f"Inventory: ${bs.get('inventory', 0):,.0f}")
            print(f"Current Liabilities: ${bs.get('current_liabilities', 0):,.0f}")
            print(f"Total Assets: ${bs.get('total_assets', 0):,.0f}")
            print(f"NCAV: ${bs.get('ncav', 0):,.0f}")

        inc = get_income_ttm(facts)
        if not inc.get('error'):
            print(f"Net Income TTM: ${inc.get('net_income_ttm', 0):,.0f}")
            print(f"Revenue TTM: ${inc.get('revenue_ttm', 0):,.0f}")
