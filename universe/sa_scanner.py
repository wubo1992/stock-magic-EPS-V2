"""
universe/sa_scanner.py — Seeking Alpha Quant 评分查询器

功能：
- 对给定的股票代码列表，逐一查询 SA Quant Rating
- 返回评分 >= 阈值（默认 4.5 = Strong Buy）的股票
- 使用 SA 非官方 API（无需账号，免费访问）

SA Quant Rating 评级标准：
  4.50 ~ 5.00  →  Strong Buy  ★★★★★
  3.50 ~ 4.49  →  Buy
  2.50 ~ 3.49  →  Hold
  1.50 ~ 2.49  →  Sell
  1.00 ~ 1.49  →  Strong Sell

API 端点（无需认证）：
  GET https://seekingalpha.com/api/v3/symbols/{TICKER}/ratings
  响应：data[0].attributes.ratings.quantRating（浮点数，如 4.85）
"""

import time
from dataclasses import dataclass
from typing import Optional

# curl_cffi 已安装，模拟真实浏览器 TLS 指纹，绕过 Cloudflare 检测
from curl_cffi import requests as cffi_requests

SA_RATINGS_URL = "https://seekingalpha.com/api/v3/symbols/{ticker}/ratings"

_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://seekingalpha.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

# SA Quant 评级阈值
STRONG_BUY_THRESHOLD = 4.5


@dataclass
class SAResult:
    ticker: str
    quant_rating: Optional[float]      # None = 无评分（SA 没有覆盖此股票）
    sell_side_rating: Optional[float]  # 卖方分析师评分（附加参考）
    author_rating: Optional[float]     # SA 作者评分（附加参考）
    error: Optional[str] = None        # 如果请求失败，记录原因

    @property
    def is_strong_buy(self) -> bool:
        return (
            self.quant_rating is not None
            and self.quant_rating >= STRONG_BUY_THRESHOLD
        )

    @property
    def rating_label(self) -> str:
        if self.quant_rating is None:
            return "无评分"
        r = self.quant_rating
        if r >= 4.5:
            return "Strong Buy"
        if r >= 3.5:
            return "Buy"
        if r >= 2.5:
            return "Hold"
        if r >= 1.5:
            return "Sell"
        return "Strong Sell"


def fetch_quant_rating(ticker: str, timeout: int = 15) -> SAResult:
    """
    查询单只股票的 SA Quant Rating。

    参数：
        ticker: 股票代码（如 "AAPL"）
        timeout: 请求超时秒数

    返回：
        SAResult 对象，包含 quant_rating 和辅助信息
    """
    url = SA_RATINGS_URL.format(ticker=ticker.upper())
    try:
        resp = cffi_requests.get(
            url,
            headers=_HEADERS,
            impersonate="chrome120",   # 模拟 Chrome 120 浏览器指纹
            timeout=timeout,
        )

        if resp.status_code == 404:
            return SAResult(ticker=ticker, quant_rating=None,
                            sell_side_rating=None, author_rating=None,
                            error="404 not found (SA 未覆盖此股票)")

        if resp.status_code == 429:
            return SAResult(ticker=ticker, quant_rating=None,
                            sell_side_rating=None, author_rating=None,
                            error="429 rate limited")

        if resp.status_code != 200:
            return SAResult(ticker=ticker, quant_rating=None,
                            sell_side_rating=None, author_rating=None,
                            error=f"HTTP {resp.status_code}")

        data = resp.json()
        items = data.get("data", [])
        if not items:
            return SAResult(ticker=ticker, quant_rating=None,
                            sell_side_rating=None, author_rating=None,
                            error="响应中无数据")

        ratings = items[0].get("attributes", {}).get("ratings", {})
        return SAResult(
            ticker=ticker,
            quant_rating=ratings.get("quantRating"),
            sell_side_rating=ratings.get("sellSideRating"),
            author_rating=ratings.get("authorsRating"),
        )

    except Exception as e:
        return SAResult(ticker=ticker, quant_rating=None,
                        sell_side_rating=None, author_rating=None,
                        error=str(e))


def scan_tickers(
    tickers: list[str],
    min_quant_rating: float = STRONG_BUY_THRESHOLD,
    request_delay: float = 1.0,
    verbose: bool = True,
) -> list[SAResult]:
    """
    批量扫描一组股票，返回评分达标的结果列表。

    参数：
        tickers: 要扫描的股票代码列表
        min_quant_rating: 最低 Quant Rating 阈值（默认 4.5 = Strong Buy）
        request_delay: 每次请求之间的等待秒数（防止被限速）
        verbose: 是否打印进度

    返回：
        评分 >= min_quant_rating 的 SAResult 列表（按评分从高到低排序）
    """
    results: list[SAResult] = []
    total = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        if verbose:
            print(f"[SA扫描] ({i}/{total}) {ticker}...", end=" ", flush=True)

        result = fetch_quant_rating(ticker)

        if result.error:
            if verbose:
                print(f"跳过（{result.error}）")
        elif result.quant_rating is not None and result.quant_rating >= min_quant_rating:
            if verbose:
                print(f"✓ {result.rating_label} {result.quant_rating:.2f}")
            results.append(result)
        else:
            if verbose and result.quant_rating is not None:
                print(f"  {result.rating_label} {result.quant_rating:.2f}")
            elif verbose:
                print("  无评分")

        # 被限速时多等一会儿
        if result.error and "429" in (result.error or ""):
            if verbose:
                print("[SA扫描] 被限速，等待 30 秒...")
            time.sleep(30)
        else:
            time.sleep(request_delay)

    # 按评分从高到低排序
    results.sort(key=lambda r: r.quant_rating or 0, reverse=True)
    return results
