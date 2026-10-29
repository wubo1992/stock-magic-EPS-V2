"""
strategies/v_weinstein/weinstein_strategy.py — Weinstein Stage 2 阶段分析

独立实现 Weinstein Stage 2 入场（无 RS 过滤，无 VCP）：

  1. SMA150（≈30 周均线）本身在上升至少 trend_lookback 天（Stage 2 确认）
  2. 今日收盘价 > SMA150（价格在均线上方）
  3. 今日突破过去 N 日整理区高点
  4. 成交量 ≥ 1.5 倍 20 日均量（放量确认）

出场管理（继承自 SEPAStrategy 的逻辑，直接在这里实现）：
  - 固定止损：买入价 × (1 - stop_loss_pct)
  - 追踪止盈：最高价 × (1 - trailing_stop_pct)
  - 时间止损：持仓 ≥ time_stop_days 且涨幅 < time_stop_min_gain

对应 config.yaml 的 strategies.v_weinstein 段。
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from events import EventQueue, SignalEvent
from strategies.base import StrategyBase


@dataclass
class Position:
    """持仓记录"""
    symbol: str
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int = 0
    shares: float = 0.0
    entry_state: str = None
    entry_state_params = None
    partial_profit_taken: bool = False
    partial_profit_date = None
    add_on_triggered: bool = False
    add_on_entry_price = None
    add_on_date = None


class WeinsteinStrategy(StrategyBase):
    strategy_id = "v_weinstein"
    strategy_name = "Weinstein Stage 2 分析"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)
        self.live_mode = live_mode
        self.positions: dict[str, Position] = {}
        # 持仓功能开关（用户偏好：v_eps_v2 仅做选股，不追踪出场）
        self.positions_enabled = self.cfg.get("enable_positions", True)

        # Weinstein Stage 2 参数
        self.sma_long = self.cfg.get("sma_long", 150)
        self.trend_lookback = self.cfg.get("trend_lookback", 30)
        self.pivot_lookback = self.cfg.get("pivot_lookback", 30)
        self.min_breakout_pct = self.cfg.get("min_breakout_pct", 0.005)
        self.volume_mult = self.cfg.get("volume_mult", 1.5)

        # 出场参数
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.10)
        self.trailing_stop_pct = self.cfg.get("trailing_stop_pct", 0.18)
        self.time_stop_days = self.cfg.get("time_stop_days", 30)
        self.time_stop_min_gain = self.cfg.get("time_stop_min_gain", 0.05)

    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]:
        signals = []
        if self.positions_enabled:
            exit_signals = self._check_exits(date)
            for sig in exit_signals:
                queue.put(sig)
                signals.append(sig)

        for symbol, df in self.market_data.items():
            if symbol in ("SPY", "^VIX"):
                continue
            df_to_date = self._slice_to_date(df, date)
            min_rows = self.sma_long + self.pivot_lookback + 10
            if len(df_to_date) < min_rows:
                continue
            signal = self._check_entry(symbol, df_to_date, date)
            if signal:
                queue.put(signal)
                signals.append(signal)

        return signals

    def _check_entry(self, symbol, df, date):
        close_series = df["close"]
        current = float(close_series.iloc[-1])

        # 1. 计算 SMA150（≈30 周均线）
        sma150 = close_series.rolling(self.sma_long).mean()
        sma_now = float(sma150.iloc[-1])
        sma_past = float(sma150.iloc[-self.trend_lookback])

        # Stage 2 确认：SMA150 当前值 > trend_lookback 天前的值
        if sma_now <= sma_past:
            return None

        # 2. 价格必须在 SMA150 之上
        if current <= sma_now:
            return None

        # 3. 突破近期整理区最高价（排除今天）
        pivot = float(close_series.iloc[-(self.pivot_lookback + 1):-1].max())
        if current <= pivot * (1 + self.min_breakout_pct):
            return None

        # 4. 放量确认（成交量 >= volume_mult × 20 日均量）
        avg_vol = float(close_series.iloc[-21:-1].mean())  # 等等，这里用的是 close_series 而不是 volume
        # 原文用的是 df["volume"]
        avg_vol = float(df["volume"].iloc[-21:-1].mean())
        if avg_vol == 0:
            return None
        vol_ratio = float(df["volume"].iloc[-1]) / avg_vol
        if vol_ratio < self.volume_mult:
            return None

        breakout_pct = (current - pivot) / pivot
        strength = 3  # Weinstein 策略默认 3 星（纯趋势跟随）
        stop_loss = round(current * (1 - self.stop_loss_pct), 2)
        reason = (
            f"[Stage2] SMA{self.sma_long}({sma_now:.0f})上升{self.trend_lookback}天 | "
            f"[突破] 超{self.pivot_lookback}日整理区高点${pivot:.2f}，"
            f"幅度+{breakout_pct*100:.1f}% | "
            f"[量能] {vol_ratio:.1f}x均量"
        )
        signal = SignalEvent.create(
            symbol=symbol, timestamp=date, direction="buy",
            strength=strength, reason=reason, stop_loss=stop_loss,
        )
        return signal

    def _check_exits(self, date):
        signals = []
        to_close = []
        for symbol, pos in self.positions.items():
            df = self.market_data.get(symbol)
            if df is None:
                continue
            df_to_date = self._slice_to_date(df, date)
            if df_to_date.empty:
                continue
            current_price = float(df_to_date["close"].iloc[-1])
            pos.days_held += 1
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            exit_reason = None
            fixed_stop = pos.entry_price * (1 - self.stop_loss_pct)
            if current_price <= fixed_stop:
                exit_reason = (
                    f"触发止损：入场${pos.entry_price:.2f}，当前${current_price:.2f}，"
                    f"跌幅{(current_price/pos.entry_price-1)*100:.1f}%"
                )
            elif pos.highest_price > pos.entry_price:
                trailing_stop = pos.highest_price * (1 - self.trailing_stop_pct)
                if current_price <= trailing_stop:
                    gain = (pos.highest_price / pos.entry_price - 1) * 100
                    exit_reason = (
                        f"追踪止盈：最高${pos.highest_price:.2f}（+{gain:.1f}%），"
                        f"当前${current_price:.2f}，从最高点回落{self.trailing_stop_pct*100:.0f}%"
                    )
            elif pos.days_held >= self.time_stop_days:
                current_gain = current_price / pos.entry_price - 1
                if current_gain < self.time_stop_min_gain:
                    exit_reason = (
                        f"时间止损：持仓{pos.days_held}天，盈利仅{current_gain*100:.1f}%"
                    )
            if exit_reason:
                sig = SignalEvent.create(
                    symbol=symbol, timestamp=date, direction="sell",
                    strength=3, reason=exit_reason, stop_loss=None,
                )
                signals.append(sig)
                to_close.append(symbol)
        for symbol in to_close:
            del self.positions[symbol]
        return signals

    def _slice_to_date(self, df: pd.DataFrame, date: datetime) -> pd.DataFrame:
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        idx = df.index
        if idx.tzinfo is None:
            idx = idx.tz_localize("UTC")
            df = df.copy()
            df.index = idx
        return df[df.index <= date]

    def get_open_positions(self):
        return dict(self.positions)
