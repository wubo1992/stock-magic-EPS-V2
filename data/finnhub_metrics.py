"""
data/finnhub_metrics.py — Finnhub 财务指标模块

使用 Finnhub /stock/metric API 获取财务指标，作为深度价值筛选的代理。

注意：Finnhub 免费版不提供完整财务报表，但 /stock/metric 包含：
- cashRatio: 现金比率
- currentRatio: 流动比率
- quickRatio: 速动比率
- totalDebt/totalAssets: 负债率
- peBasicExclExtraTTM: P/E
- pbAnnual: P/B
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

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _metric_cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"finnhub_metric_{symbol}.pkl"


def _is_cache_valid(path: Path, max_age_days: int = 7) -> bool:
    """检查缓存是否有效（一周内的数据视为有效）"""
    if not path.exists():
        return False
    file_age = datetime.now().timestamp() - path.stat().st_mtime
    return file_age < (max_age_days * 86400)


def get_stock_metrics(symbol: str) -> dict:
    """
    获取股票财务指标

    返回：
        {
            'pe': float,           # P/E
            'pb': float,           # P/B
            'cash_ratio': float,   # 现金比率（现金/流动负债）
            'current_ratio': float, # 流动比率
            'quick_ratio': float,  # 速动比率
            'debt_to_assets': float, # 负债/资产
            'market_cap': float,   # 市值
            'error': str/None
        }
    """
    cache_path = _metric_cache_path(symbol)

    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass

    if not FINNHUB_API_KEY:
        return {'error': 'No API Key'}

    url = f"{FINNHUB_BASE_URL}/stock/metric"
    params = {
        "symbol": symbol,
        "metric": "all",
        "token": FINNHUB_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return {'error': f'HTTP {resp.status_code}'}

        data = resp.json()
        if data.get("error"):
            return {'error': data['error']}

        metric = data.get("metric", {})

        result = {
            'pe': metric.get("peBasicExclExtraTTM", 0),
            'pb': metric.get("pbAnnual", 0),
            'cash_ratio': metric.get("cashRatio", 0),
            'current_ratio': metric.get("currentRatio", 0),
            'quick_ratio': metric.get("quickRatio", 0),
            'total_liabilities': metric.get("totalLiabilities", 0),
            'total_assets': metric.get("totalAssets", 0),
            'market_cap': metric.get("marketCapitalization", 0),
            'pe_high': metric.get("peExclExtraTTMHigh", 0),
            'pe_low': metric.get("peExclExtraTTMLow", 0),
            'ps': metric.get("psAnnual", 0),
            'pcf': metric.get("pcfSharePrice", 0),
        }

        # 计算债务/资产
        if result['total_assets'] > 0:
            result['debt_to_assets'] = result['total_liabilities'] / result['total_assets']
        else:
            result['debt_to_assets'] = 0

        # 缓存
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
        except Exception:
            pass

        return result

    except Exception as e:
        return {'error': str(e)}


def calculate_ncav_estimate(metrics: dict) -> float:
    """
    用 Finnhub 指标估算 NCAV

    NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 流动负债

    Finnhub 没有直接的流动资产数据，但可以估算：
    - cashRatio = cash / currentLiabilities
    - currentRatio = currentAssets / currentLiabilities

    假设：流动资产 ≈ cashRatio / currentRatio × 流动负债
    这不够准确，但可以作为筛选依据。

    更实用的方法：直接用 cashRatio 估算
    NCAV ≈ cash - currentLiabilities ≈ cashRatio × currentLiabilities - currentLiabilities
                               = currentLiabilities × (cashRatio - 1)

    如果 cashRatio >= 1，则 NCAV > 0
    """
    if not metrics or metrics.get('error'):
        return 0

    cash_ratio = metrics.get('cash_ratio', 0)
    current_ratio = metrics.get('current_ratio', 0)

    # 简化：NCAV > 0 当且仅当现金比率 > 1
    # 这意味着现金 > 流动负债
    if cash_ratio >= 1.0:
        return 1.0  # NCAV 正
    elif cash_ratio >= 0.75:
        return 0.5  # NCAV 可能正（保守估计）
    else:
        return 0  # NCAV 负或极小


def estimate_p_ncav(metrics: dict, market_cap: float) -> float:
    """
    估算 P/NCAV

    由于无法精确计算 NCAV，我们用代理指标：
    - P/NCAV 低 = 股价相对于清算价值便宜
    - 用 P/B 和 P/E 的组合来估计

    启发式：
    - P/B < 1.0 且 P/E < 15 → 低估
    - P/B < 0.8 → 极度低估
    - 结合 cash_ratio 调整
    """
    if not metrics or metrics.get('error'):
        return float('inf')

    pb = metrics.get('pb', 0)
    pe = metrics.get('pe', 0)
    cash_ratio = metrics.get('cash_ratio', 0)
    debt_to_assets = metrics.get('debt_to_assets', 0)

    if pb <= 0:
        return float('inf')

    # 调整 P/B：负债率高的公司应该更便宜
    # 负债率 > 50% → P/B 额外折扣 20%
    adjusted_pb = pb
    if debt_to_assets > 0.5:
        adjusted_pb = pb * 1.2  # 负债高，应该更便宜，所以 P/B 要更高（更差）

    # 如果现金比率高，公司更健康，P/B 不应该打折
    # cash_ratio > 1 → 公司有足够现金，P/B 正常
    # cash_ratio < 0.5 → 现金紧张，P/B 打折

    # 最终评分：越低越好
    # 简单代理：adjusted_pb 越低越好
    # 如果现金比率 < 0.5，再加 20% 折扣

    if cash_ratio < 0.5:
        adjusted_pb = adjusted_pb * 1.3
    elif cash_ratio < 0.75:
        adjusted_pb = adjusted_pb * 1.1

    return adjusted_pb


if __name__ == "__main__":
    # 测试
    for symbol in ['AAPL', 'MSFT', 'GM']:
        print(f"\n--- {symbol} ---")
        metrics = get_stock_metrics(symbol)
        if metrics.get('error'):
            print(f"Error: {metrics['error']}")
        else:
            print(f"P/E: {metrics.get('pe', 0):.2f}")
            print(f"P/B: {metrics.get('pb', 0):.2f}")
            print(f"Cash Ratio: {metrics.get('cash_ratio', 0):.2f}")
            print(f"Current Ratio: {metrics.get('current_ratio', 0):.2f}")
            print(f"Debt/Assets: {metrics.get('debt_to_assets', 0):.2%}")
            print(f"Market Cap: ${metrics.get('market_cap', 0)/1e9:.2f}B")
            print(f"Est P/NCAV: {estimate_p_ncav(metrics, metrics.get('market_cap', 0)):.2f}")

        time.sleep(1)
