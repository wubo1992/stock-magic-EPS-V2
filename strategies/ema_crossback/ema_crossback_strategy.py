"""
strategies/ema_crossback/ema_crossback_strategy.py — EMA 10 crossback 检测

核心算法（spec §6.1）：
- 在 df[signal_date..today] 范围内扫描
- state 初值根据 signal_date 当日 close vs EMA10 决定（spec Iteration 1 #3 修复）
- close < EMA10 → state = "below"
- state == "below" 且 close > EMA10 → crossback 候选
- volume >= vol_mult × avg_vol_20 → emit signal
- state = "above"（无论是否触发）

数据来源优先级（spec §6.1.1）：
1. in-memory market_data[sym]
2. data/cache/{sym}.pkl
3. fetch() 重新拉取
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data.fetcher import fetch
from data.rs import compute_rs
from events import SignalEvent
from events.base import CROSSBACK_BUY_SIGNAL_TYPE

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache")

REASON_PREFIX_CROSSBACK = "[EMA10回踩]"


def detect_crossback(
    df: pd.DataFrame,
    signal_date: pd.Timestamp,
    ema_period: int = 10,
    vol_mult: float = 1.5,
    vol_lookback: int = 20,
) -> list[dict[str, Any]]:
    """
    在 df[signal_date..today] 范围内检测 EMA crossback + 量能确认。

    Returns:
        list of dict: [{date, close, ema10, volume, avg_vol_20, volume_ratio, strength}]
        失败/数据不足时返回 []
    """
    if df is None or len(df) < ema_period + vol_lookback:
        return []

    df = df.copy()
    df["ema10"] = df["close"].ewm(span=ema_period, adjust=False).mean()
    # shift(1)：今日成交量 ÷ 过去 20 日均量（不含今日）
    df["avg_vol_20"] = df["volume"].rolling(vol_lookback).mean().shift(1)

    # tz-aware fix: signal_date 可能是 tz-naive Timestamp（如 YYYY-MM-DD 字符串），
    # 但 df.index 可能是 tz-aware UTC（如 fetch 拿到的实盘数据）。比较前需统一。
    if df.index.tz is not None and signal_date.tz is None:
        signal_date = signal_date.tz_localize("UTC")

    df = df[df.index >= signal_date]
    if df.empty:
        return []

    # Issue 3 修复：初值根据 signal_date 当日状态决定
    first_row = df.iloc[0]
    # "<=" so equal-to-EMA10 day counts as "below" (avoids spurious crossback on rounding ties).
    # Loop body uses strict ">" so equal-day does NOT emit a signal.
    state = "below" if first_row["close"] <= first_row["ema10"] else "above"

    signals = []
    for date, row in df.iterrows():
        if pd.isna(row["avg_vol_20"]):
            continue

        if row["close"] < row["ema10"]:
            state = "below"
        elif state == "below" and row["close"] > row["ema10"]:
            vol_ratio = row["volume"] / row["avg_vol_20"]
            if vol_ratio >= vol_mult:
                signals.append({
                    "date": date,
                    "close": row["close"],
                    "ema10": row["ema10"],
                    "volume": row["volume"],
                    "avg_vol_20": row["avg_vol_20"],
                    "volume_ratio": vol_ratio,
                    "strength": _crossback_strength(vol_ratio),
                })
            state = "above"

    return signals


def _crossback_strength(vol_ratio: float) -> int:
    """
    根据量能倍数计算强度等级（3-5）：
    - 1.5 - 2.0x: ★★★☆☆ (3)
    - 2.0 - 3.0x: ★★★★☆ (4)
    - >= 3.0x: ★★★★★ (5)

    Raises:
        ValueError: 如果 vol_ratio < 1.5（caller 应在调用前过滤）

    Returns:
        int: 强度等级 3-5
    """
    if vol_ratio < 1.5:
        raise ValueError(f"vol_ratio must be >= 1.5, got {vol_ratio}")
    if vol_ratio >= 3.0:
        return 5
    if vol_ratio >= 2.0:
        return 4
    return 3  # 1.5 <= vol_ratio < 2.0


def _get_df_for_symbol(
    sym: str,
    market_data: dict[str, pd.DataFrame] | None,
    cache_dir: Path,
    today: datetime | None = None,
) -> pd.DataFrame | None:
    """
    三级数据查找：in-memory → cache pkl → fetch fallback（修复 spec §6.1.1）。

    Returns:
        DataFrame 或 None（拉取失败时返回 None）
    """
    # 优先级 1：内存
    if market_data and sym in market_data:
        return market_data[sym]

    # 优先级 2：cache pkl
    pkl_path = cache_dir / f"{sym}.pkl"
    if pkl_path.exists():
        try:
            return pd.read_pickle(pkl_path)
        except Exception as e:
            logger.warning(f"[crossback] {sym} cache read failed: {e}")

    # 优先级 3：fetch 重新拉取（spec §6.1.1 + Iteration 4 #17）
    try:
        df_new = fetch(
            [sym],
            history_days=60,
            live_mode=False,
            end_date=today,
        )
        if df_new and sym in df_new:
            return df_new[sym]
    except Exception as e:
        logger.warning(f"[crossback] {sym} fetch failed: {e}")

    return None


def run_ema_crossback_strategy(
    watchlist: dict[str, dict[str, Any]],
    market_data: dict[str, pd.DataFrame] | None = None,
    today: datetime | None = None,
    cache_dir: Path | None = None,
    ema_period: int = 10,
    vol_mult: float = 1.5,
    vol_lookback: int = 20,
) -> list[SignalEvent]:
    """
    遍历 watchlist，对每只股票检测 crossback（spec §6.1.1 + §6.2.4）。

    Returns:
        list[SignalEvent]: 含 signal_type="crossback_buy"
    Side effect:
        更新入参 watchlist 中触发的 symbol 的 crossback_count / last_crossback_date
    """
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    if today is None:
        today = datetime.now(tz=timezone.utc)

    events = []
    for sym, entry in watchlist.items():
        signal_date = pd.Timestamp(entry["signal_date"])
        df = _get_df_for_symbol(sym, market_data, cache_dir, today=today)
        if df is None:
            logger.warning(f"[crossback] {sym} no data, skipping")
            continue

        crossbacks = detect_crossback(
            df, signal_date, ema_period=ema_period,
            vol_mult=vol_mult, vol_lookback=vol_lookback,
        )
        for cb in crossbacks:
            # 计算 RS（10D/10W/SPY + O'Neil RS Rating）—— spec §6.2.4，使用正确签名
            rs_result = compute_rs(sym, cb["date"], market_data)
            rs_segment = ""
            rs_data = None
            if rs_result is not None:
                rs_segment = " | " + rs_result.format_reason_segment()
                rs_data = rs_result.to_column_dict()

            # 防御性检查（spec Iteration 4 #16）：必须先于除法
            if cb["ema10"] <= 0:
                continue

            crossback_pct = (cb["close"] - cb["ema10"]) / cb["ema10"] * 100

            # 判断是否为新增事件（未被记录过），避免历史事件重复累加 crossback_count
            cb_date_str = cb["date"].strftime("%Y-%m-%d")
            is_new = True
            last_cb = entry.get("last_crossback_date")
            if last_cb:
                try:
                    last_cb_dt = datetime.strptime(last_cb, "%Y-%m-%d").date()
                    if cb["date"].date() <= last_cb_dt:
                        is_new = False
                except ValueError:
                    pass

            signal = SignalEvent.create(
                symbol=sym,
                timestamp=cb["date"],
                direction="buy",
                strength=cb["strength"],
                reason=(
                    f"{REASON_PREFIX_CROSSBACK} 上穿EMA10(${cb['ema10']:.2f}, +{crossback_pct:.1f}%) "
                    f"+ 量能{cb['volume_ratio']:.1f}x均量"
                    f"{rs_segment}"
                ),
                stop_loss=entry["stage2_stop"],
                signal_type=CROSSBACK_BUY_SIGNAL_TYPE,
                rs_data=rs_data,
            )
            events.append(signal)

            # 仅对新增事件累加统计字段（spec §6.1.1: crossback_count++ / last_crossback_date）
            if is_new:
                entry["crossback_count"] = entry.get("crossback_count", 0) + 1
                entry["last_crossback_date"] = cb_date_str

    return events
