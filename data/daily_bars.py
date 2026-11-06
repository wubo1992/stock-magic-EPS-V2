"""
data/daily_bars.py — 日线「同一交易日多根 bar」的去重

行情 pkl 里同一个交易日经常有**两根 bar**（同一场交易、两个数据源）：

    2026-10-22 00:00:00+00:00   344.940  348.360  342.280  348.220   47047124.0   ← 合并量
    2026-10-22 04:00:00+00:00   344.890  348.340  342.290  348.340     980486.0   ← IEX

OHLC 只差几分钱，但**量差 20~50 倍**（合并量是全市场，IEX 只是 IEX 一个交易所）。

实测（10,711 只日线 pkl）两种版式：

  版式 A（4,140 只，如 AAPL/SPY）：2016 起每天一根 00:00 合并量 bar；
      **2026-08-27 ~ 10-22** 期间每天多插一根 04:00 的 IEX bar；10-23 之后只剩 IEX bar。
  版式 B（5,329 只，如 DFIN）：04:00 的 IEX bar 与 13:30 的合并量 bar 并存到 2026-04-10；
      之后只剩 IEX bar。

后果（策略的窗口全是**行数**切片时）：

  - 「30 个交易日」的整理区高点只剩 20 天 → pivot 算低 → **突破更容易成立** → 假信号
  - 「20 日均量」只剩 15 天，且混进 20~50 倍的合并量 → 均量被抬高 → **量比被压低** → 真信号被误杀

统一规则：**同一 UTC 交易日只保留时间戳最晚的那根 bar**（= 与最近一根同源、量纲一致的 IEX bar）。
"""

import pandas as pd

__all__ = ["dedup_daily_bars"]


def dedup_daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """同一 UTC 交易日只保留**时间戳最晚**的那根 bar。

    - 保留原索引（不改成 00:00），下游「最后一根 K 线是哪天」的判断不受影响
    - 已经是「一天一根」的 df 原样返回（不复制）
    - 索引不是 DatetimeIndex / 空表 → 原样返回
    """
    if df is None or df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    days = df.index.normalize()
    if not days.duplicated().any():
        return df

    # kind="stable"：同一时间戳多行时保留文件里靠后的那行，顺序可复现
    out = df.sort_index(kind="stable")
    keep = ~out.index.normalize().duplicated(keep="last")
    return out.loc[keep]
