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

import numpy as np
import pandas as pd

__all__ = ["dedup_daily_bars", "same_regime_sample_count", "same_regime_volume_ma",
           "volume_regimes", "REGIME_CONSOLIDATED", "REGIME_IEX"]


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


# --------------------------------------------------------------------------
# 量能口径（IEX vs 合并量）
# --------------------------------------------------------------------------

REGIME_IEX = "iex"
REGIME_CONSOLIDATED = "consolidated"


def volume_regimes(index: pd.DatetimeIndex) -> list[str]:
    """每根 bar 的**量能口径**。

    沿用 `data/stage.py` 的周线约定（那边早就在按同口径比量比）：

        04:00 / 05:00 UTC → IEX（美东当日零点，夏令时差一小时）
        其余（00:00 UTC）→ 合并量（Yahoo）

    IEX 的量只有合并量的 1/20~1/50，两者**不能平均**。
    """
    return [REGIME_IEX if hour in (4, 5) else REGIME_CONSOLIDATED
            for hour in index.hour]


def same_regime_volume_ma(
    df: pd.DataFrame,
    window: int = 20,
    min_samples: int = 4,
) -> pd.Series:
    """每根 bar 的「**同口径**前 window 根均量」（不含当日）。

    为什么不能直接用 `volume.rolling(window).mean()`
    ----------------------------------------
    2026-10-23 前后行情 pkl 换过数据源（Yahoo 合并量 → Alpaca IEX）。
    文件里前段是合并量、后段是 IEX，量差 20~50 倍。把两者放进同一个窗口
    求平均 → 均量被合并量抬高一个数量级 → 量比被压到 0.0x → **放量门槛
    永远不成立，真信号被误杀**（实测 ATER 0.27x、LOBO 0.002x）。

    规则：每根 bar 只跟**跟自己同口径**的历史比。

    - 同口径样本 ≥ min_samples → 用同口径均量
    - 同口径样本不足 → 退回普通窗口均量（而不是 NaN：
      NaN 会让状态机跳过这些 bar，可能漏掉上穿事件）
    - 普通窗口本身不满（历史不足 window 根）→ 仍然 NaN（与修复前一致）

    单一口径的文件（绝大多数）结果与 `rolling(window).mean().shift(1)`
    **完全一致** —— 这条路径不改变任何没出过问题的票的行为。
    """
    if df is None or df.empty or "volume" not in df.columns:
        return pd.Series(dtype=float)
    if not isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(dtype=float)

    volume = df["volume"].astype(float)
    regimes = np.asarray(volume_regimes(df.index))
    plain = volume.rolling(window).mean().shift(1)

    same_sum = pd.Series(np.nan, index=df.index, dtype=float)
    same_count = pd.Series(np.nan, index=df.index, dtype=float)
    for regime in (REGIME_IEX, REGIME_CONSOLIDATED):
        mask = pd.Series((regimes == regime).astype(float), index=df.index)
        summed = (volume * mask).rolling(window).sum().shift(1)
        counted = mask.rolling(window).sum().shift(1)
        take = regimes == regime
        same_sum = same_sum.mask(take, summed)
        same_count = same_count.mask(take, counted)

    usable = same_count.where(same_count > 0)
    same_avg = same_sum.where(same_count > 0) / usable
    return same_avg.where(same_count >= min_samples, plain)


def same_regime_sample_count(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """每根 bar 的前 window 根窗口里，有多少根**同口径**的 bar。

    少于 window 根 → 说明这个文件跨过了数据源换挡（合并量 ↔ IEX），
    它的量比是「用不足 window 根同口径 bar 算出来的」，读数时要打折看。
    """
    if df is None or df.empty or "volume" not in df.columns:
        return pd.Series(dtype=float)
    if not isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(dtype=float)

    regimes = np.asarray(volume_regimes(df.index))
    counts = pd.Series(np.nan, index=df.index, dtype=float)
    for regime in (REGIME_IEX, REGIME_CONSOLIDATED):
        mask = pd.Series((regimes == regime).astype(float), index=df.index)
        counted = mask.rolling(window).sum().shift(1)
        counts = counts.mask(regimes == regime, counted)
    return counts
