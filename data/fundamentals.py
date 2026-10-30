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

# EPS 缓存过期时间（天）。EPS 每月只多一个季度，30 天足够；
# 超过就必须重抓，否则永远看不到新季度（2026-10-30 实测：LUNG 的缓存
# 从 2026-04-13 用到 10-29，最新季度还停在 2025-Q4）。
EPS_CACHE_TTL_DAYS = 30


def _eps_cache_path(symbol: str) -> Path:
    """EPS 缓存文件路径（永久缓存）"""
    return CACHE_DIR / f"eps_{symbol}.pkl"


def _is_cache_valid(path: Path) -> bool:
    """检查缓存是否存在且未过期。

    历史季度数据本身永久有效；过期的只是「可能漏了新季度」这件事，
    所以超过 TTL 就要重新抓一次。抓不到时调用方会回退到旧缓存。
    """
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days <= EPS_CACHE_TTL_DAYS


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

    # 尝试从缓存加载。缓存过期（>TTL）时先留一份旧数据当兜底，
    # 刷新失败还能用 —— 宁可旧数据也不要没有数据。
    stale_cache = None
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if _is_cache_valid(cache_path):
                print(f"[EPS] {symbol} 从缓存加载")
                return _check_eps_growth(
                    cached, quarters_required, yoy_required, qoq_required
                )
            stale_cache = cached
            print(f"[EPS] {symbol} 缓存已过期（> {EPS_CACHE_TTL_DAYS} 天），尝试刷新...")
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
            if stale_cache is not None:
                print(f"[EPS] {symbol} 刷新失败，回退到过期缓存")
                return _check_eps_growth(
                    stale_cache, quarters_required, yoy_required, qoq_required
                )
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

    if stale_cache is not None:
        print(f"[EPS] {symbol} 无可用 API，回退到过期缓存")
        return _check_eps_growth(
            stale_cache, quarters_required, yoy_required, qoq_required
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

    # ── 详细指标（用于信号 reason 展示） ─────────────────────────
    # 连续环比增长数：从最新季度往前数，直到出现非增长或到达历史末尾
    consecutive_qoq_growth = 0
    for i in range(len(eps_history) - 1):
        if eps_history[i][1] > eps_history[i + 1][1]:
            consecutive_qoq_growth += 1
        else:
            break

    # 连续同比正增长数：最多 3 对（要求有 ≥7 季度数据）
    consecutive_yoy_growth = 0
    for i in range(min(3, len(eps_history) - 4)):
        if eps_history[i][1] > eps_history[i + 4][1]:
            consecutive_yoy_growth += 1
        else:
            break

    # 最新环比增速（用 abs(prev) 防止跨负数时符号错乱）
    latest_eps_val = eps_values[0]
    prev_q_eps = eps_values[1] if len(eps_values) >= 2 else None
    if prev_q_eps is not None and prev_q_eps != 0:
        qoq_growth_rate = (latest_eps_val - prev_q_eps) / abs(prev_q_eps)
    else:
        qoq_growth_rate = None

    # 最新同比增速
    year_ago_eps = eps_history[4][1] if len(eps_history) > 4 else None
    if year_ago_eps is not None and year_ago_eps != 0:
        yoy_growth_rate = (latest_eps_val - year_ago_eps) / abs(year_ago_eps)
    else:
        yoy_growth_rate = None

    return {
        'has_data': True,
        'quarters': len(eps_history),
        'yoy_growth': yoy_growth,
        'qoq_growth': qoq_growth,
        'consecutive_qoq_growth': consecutive_qoq_growth,
        'consecutive_yoy_growth': consecutive_yoy_growth,
        'qoq_growth_rate': qoq_growth_rate,
        'yoy_growth_rate': yoy_growth_rate,
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

    latest = result.get('latest_eps') or 0
    eps_negative = latest < 0
    eps_str = f"${latest:.2f}" if latest else "N/A"

    def _growth_phrase(rate, base_word, fail, eps_negative=False):
        """生成「比去年同期高 X.X%」类短语；rate=None 时只说方向。

        eps_negative=True 时改用「亏损收窄/扩大」描述（避免「亏损高 X%」误导）。
        """
        if fail:
            return f"{base_word}下降"
        if eps_negative:
            if rate is None:
                return f"{base_word}亏损"
            if rate >= 0:
                return f"{base_word}亏损收窄{abs(rate)*100:.1f}%"
            return f"{base_word}亏损扩大{abs(rate)*100:.1f}%"
        if rate is None:
            return f"{base_word}增长"
        verb = "高" if rate >= 0 else "低"
        return f"{base_word}{verb}{abs(rate)*100:.1f}%"

    yoy_phrase = _growth_phrase(
        result.get('yoy_growth_rate'),
        "比去年同期",
        require_yoy and not result['yoy_growth'],
        eps_negative,
    )
    qoq_phrase = _growth_phrase(
        result.get('qoq_growth_rate'),
        "比上季度",
        require_qoq and not result['qoq_growth'],
        eps_negative,
    )

    streak_parts = []
    conq = result.get('consecutive_qoq_growth', 0)
    cony = result.get('consecutive_yoy_growth', 0)
    if conq >= 2:
        streak_parts.append(f"最近{conq}个季度的EPS都比上季度高")
    if cony >= 2:
        streak_parts.append(f"最近{cony}个季度的EPS都比去年同期同季度高")

    passed = (
        (not require_yoy or result['yoy_growth'])
        and (not require_qoq or result['qoq_growth'])
    )

    base = f"最新季度EPS{eps_str}，{yoy_phrase}，{qoq_phrase}"
    reason = base + ("；" + "，".join(streak_parts) if streak_parts else "")

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
