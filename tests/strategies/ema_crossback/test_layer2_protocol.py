"""第二层（watchlist EMA10 回踩）的 4 条件协议 —— 与母项目「克劳德V3.1」对齐。

母项目（`信号系统克劳德V3.1_Minimax支线/signal_system/watchlist/analyzer.py`）
把「放量穿回 EMA10」定义为 4 个条件全满足：

    ① 自加入日起曾跌破 EMA10
    ② 当前已站回 EMA10 上方
    ③ 穿回日成交量 ≥ volume_threshold × 20日均量（放量穿回）
    ④ 穿回日距今 ≤ max_days_since_crossback 天（新鲜度过滤，超过视为过期）

另有一道硬性排除：最近 30 根 K 线内发生过 ≥8 倍机械跳空（反向拆股）的股票，
它的 EMA10 建立在混合价格基准上，「跌破 / 穿回」是假象 → 直接剔除。

本项目原先只有 ③ 的一个更严的版本（1.5×），① 由状态机隐含保证，
**② ④ 和反向拆股排除全都缺失** —— 意味着：
- 一只票上个月穿回、今天早就跌回 EMA10 下面了，照样发信号；
- 一只票 30 天前的穿回照样发信号，用户以为是今天的。

这些测试先写、看着失败，再改实现。

所有 fixture 的**最后一根 bar 固定在 2026-11-11**（相对 TODAY=2026-11-12 的
上一个交易日），穿回日落在 RECOVER_IDX=56（=2026-11-06），
正好在 7 天新鲜度窗口内 —— 这样「窗口内/窗口外」只由被测参数决定。
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    DEFAULT_MAX_DAYS_SINCE_CROSSBACK,
    DEFAULT_VOL_MULT,
    _crossback_strength,
    detect_crossback,
    run_ema_crossback_strategy,
)

TODAY = datetime(2026, 11, 12, tzinfo=timezone.utc)
LAST_CLOSE = pd.Timestamp("2026-11-11", tz="UTC")
TOTAL = 60
RECOVER_IDX = 56          # 2026-11-06，在 7 天窗口内


def _df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000] * n
    dates = pd.date_range(end=LAST_CLOSE, periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volumes,
        },
        index=dates,
    )


def _dip_and_recover(recover_idx: int = RECOVER_IDX, total: int = TOTAL,
                     recovery_volume: float = 3_000_000,
                     tail_closes: list[float] | None = None) -> pd.DataFrame:
    """构造一只「先跌破 EMA10 → 放量上穿」的票。

    前 20 天从 10 涨到 20（EMA10 暖起来），之后趴在 10（跌破 EMA10），
    recover_idx 那天放量上穿到 12，之后保持 12（仍在 EMA10 上方）。
    """
    closes = list(np.linspace(10, 20, 20)) + [10.0] * (total - 20)
    for i in range(recover_idx, total):
        closes[i] = 12.0
    if tail_closes:
        for offset, val in enumerate(tail_closes):
            closes[total - len(tail_closes) + offset] = val
    vols = [1_000_000] * total
    vols[recover_idx] = recovery_volume
    return _df(closes, vols)


# ── 协议常量 ────────────────────────────────────────────────────────────

def test_default_volume_threshold_is_one_x():
    """第二层的放量门槛是 1.0×（母项目口径），不是 1.5×。"""
    assert DEFAULT_VOL_MULT == 1.0


def test_default_freshness_window_is_seven_days():
    assert DEFAULT_MAX_DAYS_SINCE_CROSSBACK == 7


# ── 条件 ④：新鲜度 ──────────────────────────────────────────────────────

def test_crossback_older_than_window_is_dropped():
    """穿回日距今超过窗口 → 不再是「可行动」信号。"""
    df = _dip_and_recover()
    recover_day = df.index[RECOVER_IDX].date()
    late_today = datetime.combine(recover_day, datetime.min.time()).replace(
        tzinfo=timezone.utc) + timedelta(days=30)

    signals = detect_crossback(
        df, signal_date=df.index[0],
        vol_mult=1.0, max_days_since_crossback=7, today=late_today,
    )
    assert signals == [], "超窗的旧穿回不该再发信号"


def test_crossback_inside_window_is_kept():
    """穿回日距今 ≤ 窗口 → 保留。"""
    df = _dip_and_recover()
    recover_day = df.index[RECOVER_IDX].date()
    near_today = datetime.combine(recover_day, datetime.min.time()).replace(
        tzinfo=timezone.utc) + timedelta(days=3)

    signals = detect_crossback(
        df, signal_date=df.index[0],
        vol_mult=1.0, max_days_since_crossback=7, today=near_today,
    )
    assert len(signals) == 1
    assert signals[0]["date"].date() == recover_day


def test_freshness_filter_is_off_when_not_requested():
    """不传 max_days_since_crossback → 不做新鲜度过滤（检测器保持纯检测语义）。"""
    df = _dip_and_recover()
    late_today = datetime(2027, 1, 1, tzinfo=timezone.utc)
    signals = detect_crossback(
        df, signal_date=df.index[0], vol_mult=1.0, today=late_today,
    )
    assert len(signals) == 1


# ── 条件 ②：当前已站回 ─────────────────────────────────────────────────

def test_price_back_below_ema10_now_yields_no_signal():
    """上穿后又跌回 EMA10 下面 → 不是「当前已站回」，不发信号。"""
    # 尾部 3 根 bar 收在 9（已跌破 EMA10）
    df = _dip_and_recover(tail_closes=[9.0, 9.0, 9.0])

    signals = detect_crossback(
        df, signal_date=df.index[0], vol_mult=1.0,
        max_days_since_crossback=7, today=TODAY,
        require_currently_above=True,
    )
    assert signals == [], "已跌回 EMA10 下方，不该发信号"


def test_price_still_above_ema10_now_keeps_signal():
    """对照组：上穿后仍站在 EMA10 上方 → 保留。"""
    df = _dip_and_recover()
    signals = detect_crossback(
        df, signal_date=df.index[0], vol_mult=1.0,
        max_days_since_crossback=7, today=TODAY,
        require_currently_above=True,
    )
    assert len(signals) == 1


# ── 条件 ③：放量阈值 ───────────────────────────────────────────────────

def test_volume_ratio_below_one_x_is_not_a_signal():
    """量比 < 1.0× = 缩量，不算「放量穿回」。"""
    df = _dip_and_recover(recovery_volume=900_000)
    signals = detect_crossback(
        df, signal_date=df.index[0], vol_mult=1.0,
        max_days_since_crossback=7, today=TODAY,
    )
    assert signals == []


def test_marginal_volume_between_one_and_one_five_is_a_signal():
    """1.0× ~ 1.5× 之间的「弱放量」现在也算第二层信号（母项目口径）。"""
    df = _dip_and_recover(recovery_volume=1_200_000)
    signals = detect_crossback(
        df, signal_date=df.index[0], vol_mult=1.0,
        max_days_since_crossback=7, today=TODAY,
    )
    assert len(signals) == 1
    assert 1.0 <= signals[0]["volume_ratio"] < 1.5


# ── 强度分级 ───────────────────────────────────────────────────────────

def test_strength_grades_marginal_volume_as_two_stars():
    """弱放量（1.0~1.5×）给 2 星，和强信号区分开。"""
    assert _crossback_strength(1.0) == 2
    assert _crossback_strength(1.4) == 2


def test_strength_thresholds_unchanged_above_one_five():
    assert _crossback_strength(1.5) == 3
    assert _crossback_strength(1.9) == 3
    assert _crossback_strength(2.0) == 4
    assert _crossback_strength(2.9) == 4
    assert _crossback_strength(3.0) == 5
    assert _crossback_strength(10.0) == 5


def test_strength_rejects_ratio_below_one():
    with pytest.raises(ValueError):
        _crossback_strength(0.9)


# ── 反向拆股硬排除 ─────────────────────────────────────────────────────

def test_reverse_split_stock_is_excluded_by_orchestrator(tmp_path):
    """最近 30 根 K 线内有 ≥8 倍机械跳空 → 整个票剔除。"""
    df = _dip_and_recover()
    # 倒数第 5 根 bar 价格机械跳 30 倍（反向拆股）
    df.iloc[-5, df.columns.get_loc("close")] = 12.0 * 30

    watchlist = {
        "SPLT": {
            "symbol": "SPLT",
            "signal_date": df.index[0].strftime("%Y-%m-%d"),
            "signal_price": 12.0,
            "stage2_stop": 11.0,
        }
    }
    events = run_ema_crossback_strategy(
        watchlist, market_data={"SPLT": df}, today=TODAY, cache_dir=tmp_path,
    )
    assert events == [], "反向拆股的假穿回不该发信号"


def test_clean_stock_passes_the_split_guard(tmp_path):
    """对照组：没有机械跳空的票照常发信号。"""
    df = _dip_and_recover()
    watchlist = {
        "CLEAN": {
            "symbol": "CLEAN",
            "signal_date": df.index[0].strftime("%Y-%m-%d"),
            "signal_price": 12.0,
            "stage2_stop": 11.0,
        }
    }
    events = run_ema_crossback_strategy(
        watchlist, market_data={"CLEAN": df}, today=TODAY, cache_dir=tmp_path,
    )
    assert len(events) == 1
    assert events[0].symbol == "CLEAN"


def test_orchestrator_enforces_the_whole_protocol(tmp_path):
    """编排器默认就应该执行完整协议（②③④ + 反向拆股）。"""
    df = _dip_and_recover()
    watchlist = {
        "FULL": {
            "symbol": "FULL",
            "signal_date": df.index[0].strftime("%Y-%m-%d"),
            "signal_price": 12.0,
            "stage2_stop": 11.0,
        }
    }
    events = run_ema_crossback_strategy(
        watchlist, market_data={"FULL": df}, today=TODAY, cache_dir=tmp_path,
    )
    assert len(events) == 1
    # 过了新鲜度闸门 → 穿回日距今 ≤ 7 天
    days_since = (TODAY.date() - events[0].timestamp.date()).days
    assert 0 <= days_since <= 7
