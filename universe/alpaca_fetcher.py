"""
universe/alpaca_fetcher.py — Alpaca 新闻 API 股票提取器

职责：从 Alpaca 新闻 API 获取被财经媒体提及的股票代码。
- 首次运行：抓取过去 365 天的新闻，提取所有出现过的股票代码
- 后续运行：只抓取昨天的新闻，增量更新缓存
- Alpaca 新闻 API 的每篇文章都直接标注了相关股票代码，无需文本解析
"""

import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import NewsClient
from alpaca.data.requests import NewsRequest
from dotenv import load_dotenv

# 加载 .env 文件中的 API Key
load_dotenv()

_API_KEY = os.getenv("ALPACA_API_KEY")
_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def fetch_news_symbols(lookback_days: int) -> dict[str, str]:
    """
    从 Alpaca 新闻 API 获取过去 N 天内被提及的股票代码。

    参数：
        lookback_days: 回看多少天，如 365（首次）或 1（每日增量）

    返回：
        字典：{股票代码: 最后被提及的日期字符串}
        例如：{"NVDA": "2026-03-03", "AAPL": "2026-03-02"}

    如果 API Key 未配置，返回空字典（系统会降级到只用手动列表）。
    """
    if not _API_KEY or not _SECRET_KEY:
        print("[股票池] 警告：ALPACA_API_KEY 未配置，跳过自动股票池更新")
        return {}

    try:
        client = NewsClient(api_key=_API_KEY, secret_key=_SECRET_KEY)
    except Exception as e:
        print(f"[股票池] 警告：无法连接 Alpaca API，跳过自动更新。原因：{e}")
        return {}

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=lookback_days)

    print(f"[股票池] 正在从 Alpaca 获取 {lookback_days} 天的新闻股票数据...")
    symbol_last_seen: dict[str, str] = {}
    total_articles = 0
    page_token = None

    while True:
        try:
            request = NewsRequest(
                start=start_time,
                end=end_time,
                limit=50,           # 每页最多50篇
                page_token=page_token,
                exclude_contentless=True,  # 只要有内容的文章
            )
            news_set = client.get_news(request)
        except Exception as e:
            print(f"[股票池] 获取新闻时出错：{e}")
            break

        articles = news_set.data.get("news", []) if hasattr(news_set, "data") else []
        if not articles:
            break

        for article in articles:
            total_articles += 1
            # Alpaca 新闻 API 的每篇文章直接提供相关股票代码列表
            symbols = getattr(article, "symbols", []) or []
            article_date = getattr(article, "created_at", None)
            if article_date:
                date_str = article_date.strftime("%Y-%m-%d")
                for sym in symbols:
                    sym = sym.upper().strip()
                    # 过滤：只要纯字母的美股代码（1-5个字母）
                    if sym.isalpha() and 1 <= len(sym) <= 5:
                        # 保留最新的提及日期
                        if sym not in symbol_last_seen or date_str > symbol_last_seen[sym]:
                            symbol_last_seen[sym] = date_str

        # 翻页
        page_token = getattr(news_set, "next_page_token", None)
        if not page_token:
            break

    print(
        f"[股票池] Alpaca 获取完成：{total_articles} 篇文章，"
        f"提取到 {len(symbol_last_seen)} 个唯一股票代码"
    )
    return symbol_last_seen
