"""
data/fundamentals.py — 基本面数据获取模块

职责：获取股票基本面数据（EPS、ROE、营收等），用于基本面因子筛选。

数据来源：Alpha Vantage API（需 API Key）
缓存策略：本地 pickle 文件，90 天过期

EPS 增长筛选：
- 过去 N 个季度
- 同比（YoY）增长：当前季度 EPS > 去年同期
- 环比（QoQ）增长：当前季度 EPS > 上季度
"""

import os
import pickle
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pandas as pd
import requests
from dotenv import load_dotenv

# 加载 .env 文件
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Alpha Vantage API 配置
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

# Finnhub API 配置（EPS 备选数据源，60次/分钟免费额度）
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

# ─── 路径配置 ──────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _eps_cache_path(symbol: str) -> Path:
    """EPS 缓存文件路径（永久缓存）"""
    return CACHE_DIR / f"eps_{symbol}.pkl"


def _is_cache_valid(path: Path) -> bool:
    """检查缓存是否存在（历史 EPS 数据永久有效，不需要过期机制）"""
    return path.exists()


def get_eps_growth(
    symbol: str,
    quarters_required: int = 3,
    yoy_required: bool = True,
    qoq_required: bool = True,
) -> Dict:
    """
    获取 EPS 增长数据

    参数：
        symbol: 股票代码
        quarters_required: 需要几个季度的数据
        yoy_required: 同比是否必须增长
        qoq_required: 环比是否必须增长

    返回：
        {
            'has_data': bool,           # 是否有足够数据
            'quarters': int,           # 有数据的季度数
            'yoy_growth': bool,        # 同比是否连续增长
            'qoq_growth': bool,        # 环比是否连续增长
            'latest_eps': float,       # 最新季度 EPS
            'eps_history': list,      # EPS 历史 [(quarter, eps), ...]
            'error': str/None          # 错误信息
        }
    """
    if not ALPHA_VANTAGE_API_KEY:
        return {
            'has_data': False,
            'quarters': 0,
            'yoy_growth': False,
            'qoq_growth': False,
            'latest_eps': None,
            'eps_history': [],
            'error': 'Alpha Vantage API Key not configured'
        }

    cache_path = _eps_cache_path(symbol)

    # 尝试从缓存加载
    if _is_cache_valid(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"[EPS] {symbol} 从缓存加载")
            return _check_eps_growth(
                cached, quarters_required, yoy_required, qoq_required
            )
        except Exception as e:
            print(f"[EPS] {symbol} 缓存读取失败: {e}")

    # 尝试 Finnhub（优先，免费额度充足：60次/分钟）
    if FINNHUB_API_KEY:
        print(f"[EPS] {symbol} 从 Finnhub 获取数据...")
        eps_history = _fetch_eps_from_finnhub(symbol)
        if eps_history:
            # 缓存数据
            try:
                with open(cache_path, "wb") as f:
                    pickle.dump({'eps_history': eps_history}, f)
                print(f"[EPS] {symbol} 已缓存")
            except Exception as e:
                print(f"[EPS] {symbol} 缓存失败: {e}")
            return _check_eps_growth(
                {'eps_history': eps_history},
                quarters_required,
                yoy_required,
                qoq_required
            )

    # Finnhub 失败，尝试 Alpha Vantage
    if ALPHA_VANTAGE_API_KEY:
        print(f"[EPS] {symbol} 从 Alpha Vantage 获取数据...")
        eps_history = _fetch_eps_from_av(symbol)
        if not eps_history:
            return {
                'has_data': False,
                'quarters': 0,
                'yoy_growth': False,
                'qoq_growth': False,
                'latest_eps': None,
                'eps_history': [],
                'error': 'Failed to fetch EPS data'
            }
        # 缓存数据
        try:
            with open(cache_path, "wb") as f:
                pickle.dump({'eps_history': eps_history}, f)
            print(f"[EPS] {symbol} 已缓存")
        except Exception as e:
            print(f"[EPS] {symbol} 缓存失败: {e}")
        return _check_eps_growth(
            {'eps_history': eps_history},
            quarters_required,
            yoy_required,
            qoq_required
        )

    return {
        'has_data': False,
        'quarters': 0,
        'yoy_growth': False,
        'qoq_growth': False,
        'latest_eps': None,
        'eps_history': [],
        'error': 'No EPS API configured (set FINNHUB_API_KEY or ALPHA_VANTAGE_API_KEY)'
    }

def _fetch_eps_from_finnhub(symbol: str) -> list:
    """从 Finnhub 获取 EPS 数据（季度历史）"""
    if not FINNHUB_API_KEY:
        return []

    url = f"{FINNHUB_BASE_URL}/stock/metric"
    params = {
        "symbol": symbol,
        "metric": "eps",  # 季度 EPS 历史
        "token": FINNHUB_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("error"):
            print(f"[Finnhub EPS] {symbol} 错误: {data['error']}")
            return []

        # Finnhub 返回: {"series": {"quarterly": {"eps": [{"period": "2025-12-27", "v": 2.8424}, ...]}}}
        series_quarterly = data.get("series", {}).get("quarterly", {})
        eps_data = series_quarterly.get("eps", [])
        if not eps_data:
            return []

        eps_history = []
        for item in eps_data:
            period_raw = item.get("period", "")  # "2025-12-27" 格式
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

        # 按时间倒序（最新在前）
        def sort_key(item):
            try:
                year_str, q_str = item[0].split("-Q")
                return (int(year_str), int(q_str))
            except:
                return (0, 0)

        eps_history.sort(key=sort_key, reverse=True)
        return eps_history

    except Exception as e:
        print(f"[Finnhub EPS] {symbol} 获取失败: {e}")
        return []


def _fetch_eps_from_av(symbol: str, max_retries: int = 3) -> list:
    """从 Alpha Vantage 获取 EPS 数据（带重试机制）"""
    import time

    for attempt in range(max_retries):
        try:
            # 使用 earnings 函数获取季度盈利数据
            params = {
                "function": "EARNINGS",
                "symbol": symbol,
                "apikey": ALPHA_VANTAGE_API_KEY,
            }

            resp = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"[EPS] {symbol} API 错误: {resp.status_code}")
                # 非暂时性错误，不重试
                return []

            data = resp.json()

            # 检查是否有 quarterly earnings
            if "quarterlyEarnings" not in data:
                print(f"[EPS] {symbol} 无季度盈利数据: {data.get('Note', data.get('Error Message', 'unknown'))}")
                return []

            quarterly = data.get("quarterlyEarnings", [])
            if not quarterly:
                return []

            # 提取 EPS 数据
            eps_history = []
            for item in quarterly:
                # Alpha Vantage 返回的字段：reportedEPS, estimatedEPS, surprise, surprisePercent, fiscalDateEnding
                eps_str = item.get("reportedEPS")
                if eps_str is None or eps_str == "None":
                    continue

                try:
                    eps = float(eps_str)
                    # EPS = 0 可能是未公布季度的占位符（如 Alpha Vantage 对未发布季度返回 $0）
                    # 视为无效数据，跳过
                    if eps == 0:
                        continue
                    fiscal_date = item.get("fiscalDateEnding", "")
                    # 格式化为季度标识
                    if fiscal_date:
                        # "2025-03-31" -> "2025-Q1"
                        year, month, _ = fiscal_date.split("-")
                        quarter = (int(month) - 1) // 3 + 1
                        quarter_str = f"{year}-Q{quarter}"
                    else:
                        quarter_str = "Unknown"

                    eps_history.append((quarter_str, eps))
                except (ValueError, TypeError):
                    continue

            # 成功，获取到数据
            return eps_history

        except Exception as e:
            error_str = str(e)
            if "SSL" in error_str or "Connection" in error_str:
                # 网络错误，等待后重试
                wait_time = (attempt + 1) * 5  # 5, 10, 15 秒
                print(f"[EPS] {symbol} 网络错误 (尝试 {attempt+1}/{max_retries}): {error_str[:50]}... 等待 {wait_time}s")
                time.sleep(wait_time)
            else:
                # 其他错误，不重试
                print(f"[EPS] {symbol} 获取失败: {e}")
                return []

    # 重试次数用完
    print(f"[EPS] {symbol} 获取失败，已重试 {max_retries} 次")
    return []


def _check_eps_growth(
    data: Dict,
    quarters_required: int,
    yoy_required: bool,
    qoq_required: bool,
) -> Dict:
    """检查 EPS 增长是否满足条件"""
    eps_history = data.get('eps_history', [])

    # 需要足够的季度数据：qoq需要quarters_required，yoy需要7个季度（3组同比比较）
    min_required = max(quarters_required, 7) if yoy_required else quarters_required

    if len(eps_history) < min_required:
        return {
            'has_data': False,
            'quarters': len(eps_history),
            'yoy_growth': False,
            'qoq_growth': False,
            'latest_eps': eps_history[0][1] if eps_history else None,
            'eps_history': eps_history,
            'error': f'Only {len(eps_history)} quarters available, need {min_required}'
        }

    # 取需要的季度数用于 qoq 判断
    recent = eps_history[:quarters_required]
    eps_values = [e[1] for e in recent]

    # 检查同比增长（连续3个季度同比提升）
    # eps_history[0] = 最新季度，需要比较：
    #   [0] vs [4]  → 最近季度 vs 去年同期
    #   [1] vs [5]  → 上季度   vs 上年度同期
    #   [2] vs [6]  → 上上季度 vs 上上年度同期
    full_eps = [e[1] for e in eps_history[:7]]
    yoy_growth = True
    if yoy_required and len(full_eps) >= 7:
        for i in range(3):
            if full_eps[i] <= full_eps[i + 4]:
                yoy_growth = False
                break

    # 检查环比增长（连续增长）
    qoq_growth = True
    if qoq_required and len(eps_values) >= 2:
        for i in range(len(eps_values) - 1):
            if eps_values[i] <= eps_values[i + 1]:
                qoq_growth = False
                break

    return {
        'has_data': True,
        'quarters': len(eps_history),
        'yoy_growth': yoy_growth,
        'qoq_growth': qoq_growth,
        'latest_eps': eps_values[0],
        'eps_history': eps_history,
        'error': None
    }


def check_eps_filter(
    symbol: str,
    quarters: int = 3,
    require_yoy: bool = True,
    require_qoq: bool = True,
) -> tuple[bool, str]:
    """
    便捷函数：检查股票是否通过 EPS 筛选

    返回：(通过筛选, 原因描述)
    """
    result = get_eps_growth(
        symbol,
        quarters_required=quarters,
        yoy_required=require_yoy,
        qoq_required=require_qoq
    )

    if not result['has_data']:
        return False, f"EPS数据不足({result['quarters']}季度)"

    passed = True
    reasons = []

    if require_yoy and not result['yoy_growth']:
        passed = False
        reasons.append("同比未增长")
    else:
        reasons.append("同比+")

    if require_qoq and not result['qoq_growth']:
        passed = False
        reasons.append("环比未增长")
    else:
        reasons.append("环比+")

    eps_str = f"${result['latest_eps']:.2f}" if result['latest_eps'] else "N/A"
    reason = f"EPS{eps_str}，" + "/".join(reasons)

    return passed, reason


if __name__ == "__main__":
    # 测试
    print("=== EPS Growth Test ===")

    for symbol in ['AAPL', 'MSFT', 'NVDA']:
        print(f"\n--- {symbol} ---")
        result = get_eps_growth(symbol, quarters_required=3)
        print(f"Has data: {result['has_data']}")
        print(f"Quarters: {result['quarters']}")
        print(f"YoY growth: {result['yoy_growth']}")
        print(f"QoQ growth: {result['qoq_growth']}")
        print(f"Latest EPS: {result['latest_eps']}")
        print(f"History: {result['eps_history'][:6]}")
        print(f"Error: {result['error']}")
        time.sleep(12)  # Alpha Vantage 免费版 5 requests/min
