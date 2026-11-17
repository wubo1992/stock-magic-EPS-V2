"""
data/split_guard.py — 价格尺度断裂防护（防假穿回）

从母项目「信号系统克劳德V3.1」移植（signal_system/data/split_guard.py）。

背景：
反向拆股（reverse split）当天价格会机械地跳涨几倍到几十倍，
比如 1:30 反向拆股 → 收盘价直接变成原来的 30 倍。

EMA10 是**滚动**均线，拆股前后混了两个价格基准 →
「跌破 EMA10」「上穿 EMA10」变成跨尺度比较的假象 → 假信号。

判据为什么用「倍数」而不是「涨幅超过 X%」：
涨幅阈值会误伤真实暴涨（STAK 某天 +84%，比值 1.84，是真行情）。
而 8 倍以上（+700%）的日间跳空在真实市场里几乎不存在，必是机械事件。

为什么整段「跳过」而不是「复权后继续算」：
复权要把整条历史曲线按 ratio 缩放，而拆股前后还可能叠加多次事件。
宁可漏一个信号，不给假信号 —— 与项目「宁可少发信号，不能乱发信号」一致。
"""
from __future__ import annotations

import pandas as pd

# 8 倍以上的日间跳空不可能是真实行情（+700% / −89%）
MEGA_SPLIT_RATIO = 8.0

# 检查窗口：母项目 watchlist 第二层用 30 根 K 线
LOOKBACK = 30


def has_split_gap(df: pd.DataFrame, lookback: int = LOOKBACK) -> bool:
    """
    最近 lookback 根 K 线内是否存在 ≥8 倍（或 ≤1/8）的机械跳空。

    返回 True = 这只票的价格序列在窗口内发生过尺度断裂，
    跨断点的「跌破 / 穿回」比较没有意义，策略应跳过它。

    参数:
        df: 日线 DataFrame，需含 close 列
        lookback: 检查最近多少根 K 线，默认 30
    """
    if df is None or "close" not in df.columns:
        return False

    close = df["close"].dropna().tail(lookback + 1)
    if len(close) < 2:
        return False

    ratio = close / close.shift(1)
    ratio = ratio.replace([float("inf"), float("-inf")], pd.NA).dropna()
    if ratio.empty:
        return False

    return bool(
        ((ratio >= MEGA_SPLIT_RATIO) | (ratio <= 1.0 / MEGA_SPLIT_RATIO)).any()
    )
