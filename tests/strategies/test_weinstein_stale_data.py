"""退市/停牌股票不该再出「今日」信号（2026-10-30 实测故障）。

现场：Alpaca IEX 对 CNTA / NFBK / OLPX / TWO / HLX 的行情**停在几个月前**
（这 5 只已停止交易），但 `data/cache/{sym}.pkl` 里最后一根 K 线就是那一天。
`run_date()` 用 `df[df.index <= date]` 切片，date = 今天 →
死票的最后一根 K 线被当成「今天」的技术形态，信号盖上今天的日期写进 signals.csv。

2026-10-30 实测：v_weinstein 当天出了 10 个买入信号，其中 5 个
（CNTA/NFBK/OLPX/TWO/HLX）是从 2026-06-23 ~ 2026-09-01 的旧数据算出来的。

修法：拿基准（SPY）的最后一根 K 线当「当前交易日」，
落后基准超过容忍度的票 → 不出信号 + 打印告警（不再静默）。
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from events import EventQueue
from strategies.v_weinstein.weinstein_strategy import WeinsteinStrategy


def _make_stage2_df(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """造一个满足 Stage 2 的价格序列：SMA150 上升 + 突破整理区 + 放量。"""
    n = len(dates)
    # 缓慢上升的阶梯 → SMA150 上升
    close = [10.0 + i * 0.05 for i in range(n)]
    # 最后一根跳空突破前面 30 日高点
    close[-1] = close[-2] * 1.06
    volume = [1_000_000] * n
    volume[-1] = 3_000_000  # 3x 均量
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": volume},
        index=dates,
    )


def _trading_days(start: str, count: int) -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=count, freq="B", tz="UTC")


def _market_data() -> tuple[dict, str]:
    """基准到 2026-10-29；LIVE 同样到 10-29；DEAD 停在 2026-06-23。"""
    live_dates = _trading_days("2026-02-02", 190)[-130:]  # 2026-02-02 起 190 个交易日
    # 让 live_dates 精确落在 2026-10-29
    live_dates = pd.date_range(end="2026-10-29", periods=190, freq="B", tz="UTC")

    spy = _make_stage2_df(live_dates)
    live = _make_stage2_df(live_dates)
    dead = _make_stage2_df(pd.date_range(end="2026-06-23", periods=190, freq="B", tz="UTC"))
    return {"SPY": spy, "LIVE": live, "DEAD": dead}, "LIVE"


def _strategy(market_data: dict) -> WeinsteinStrategy:
    cfg = {
        "sma_long": 150,
        "trend_lookback": 30,
        "pivot_lookback": 30,
        "min_breakout_pct": 0.005,
        "volume_mult": 1.5,
        "stop_loss_pct": 0.10,
        "enable_positions": False,
    }
    return WeinsteinStrategy(cfg, market_data)


def test_run_date_skips_symbol_whose_data_stopped_trading():
    """死票（行情停在几个月前）不该拿到标着今天的买入信号。"""
    market_data, _ = _market_data()
    strategy = _strategy(market_data)

    q = EventQueue()
    signals = strategy.run_date(datetime(2026, 10, 30, tzinfo=timezone.utc), q)

    symbols = {s.symbol for s in signals}
    assert "DEAD" not in symbols, "行情停在 2026-06-23 的票不该出今日信号"
    assert "LIVE" in symbols, "数据新鲜的票应该正常出信号"


def test_stale_symbols_are_reported_not_silent():
    """跳过必须有告警 —— 静默跳过会让人以为'今天市场没机会'。"""
    market_data, _ = _market_data()
    strategy = _strategy(market_data)

    strategy.run_date(datetime(2026, 10, 30, tzinfo=timezone.utc), EventQueue())

    assert "DEAD" in strategy.stale_symbols
    assert "LIVE" not in strategy.stale_symbols


def test_stale_tolerance_is_configurable():
    """容忍度可配（交易日落后数）。默认 3 个交易日。"""
    market_data, _ = _market_data()
    strategy = _strategy(market_data)
    assert strategy.max_stale_bars == 3

    strategy2 = _strategy({**market_data, "SPY": market_data["SPY"]})
    strategy2.cfg["max_stale_bars"] = 200
    strategy2.max_stale_bars = 200
    q = EventQueue()
    signals = strategy2.run_date(datetime(2026, 10, 30, tzinfo=timezone.utc), q)
    assert "DEAD" in {s.symbol for s in signals}
