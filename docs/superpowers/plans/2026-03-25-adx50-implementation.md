# ADX50 动量突破策略 — 实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan.

**Goal:** 实现 ADX50 动量突破策略，在 2020-2024 数据上完成回测。

**Architecture:** 独立策略类，直接继承 StrategyBase，不依赖 SEPAStrategy。使用 pandas_ta 计算 ADX。

---

## 文件结构

```
strategies/v_adx50/
├── __init__.py              # 包入口，导出 ADX50Strategy
└── adx50_strategy.py        # 策略类实现

strategies/registry.py       # 添加 v_adx50 注册行
config.yaml                  # 添加 v_adx50 配置段
```

---

## Chunk 1: 创建策略包和入口文件

**Files:**
- Create: `strategies/v_adx50/__init__.py`

- [ ] **Step 1: 创建目录和 __init__.py**

```python
# strategies/v_adx50/__init__.py
from strategies.v_adx50.adx50_strategy import ADX50Strategy

__all__ = ["ADX50Strategy"]
```

---

## Chunk 2: 编写策略实现

**Files:**
- Create: `strategies/v_adx50/adx50_strategy.py`

- [ ] **Step 1: 编写 adx50_strategy.py**

```python
"""
strategies/v_adx50/adx50_strategy.py — ADX50 动量突破策略

买入条件：收盘价 > 50日最高价 且 ADX(14) > 25
止损：亏损 -10%
止盈：盈利 +20%
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
    shares: float = 100.0


class ADX50Strategy(StrategyBase):
    strategy_id = "v_adx50"
    strategy_name = "ADX50 动量突破策略"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)
        self.live_mode = live_mode
        self.positions: dict[str, Position] = {}

        self.high_lookback = self.cfg.get("high_lookback", 50)
        self.adx_period = self.cfg.get("adx_period", 14)
        self.adx_threshold = self.cfg.get("adx_threshold", 25)
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.10)
        self.take_profit_pct = self.cfg.get("take_profit_pct", 0.20)
        self.volume_mult = self.cfg.get("volume_mult", 1.5)
        self.volume_lookback = self.cfg.get("volume_lookback", 20)

    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]:
        signals = []
        # 1. 出场检查
        exit_signals = self._check_exits(date)
        for sig in exit_signals:
            queue.put(sig)
            signals.append(sig)

        # 2. 入场扫描
        for symbol, df in self.market_data.items():
            df_to_date = self._slice_to_date(df, date)
            min_rows = self.high_lookback + self.adx_period + 10
            if len(df_to_date) < min_rows:
                continue
            signal = self._check_entry(symbol, df_to_date, date)
            if signal:
                queue.put(signal)
                signals.append(signal)

        return signals

    def _check_exits(self, date: datetime) -> list[SignalEvent]:
        signals = []
        for symbol, pos in list(self.positions.items()):
            df = self.market_data.get(symbol)
            if df is None:
                continue
            df_to_date = self._slice_to_date(df, date)
            if len(df_to_date) < 1:
                continue
            current_price = float(df_to_date["close"].iloc[-1])
            gain_pct = (current_price - pos.entry_price) / pos.entry_price

            if gain_pct <= -self.stop_loss_pct or gain_pct >= self.take_profit_pct:
                direction = "sell"
                strength = 3
                if gain_pct >= self.take_profit_pct:
                    reason = f"[止盈] 收益率+{gain_pct*100:.1f}%（阈值+{self.take_profit_pct*100:.0f}%）"
                else:
                    reason = f"[止损] 亏损{gain_pct*100:.1f}%（阈值-{self.stop_loss_pct*100:.0f}%）"
                signal = SignalEvent.create(
                    symbol=symbol,
                    timestamp=date,
                    direction=direction,
                    strength=strength,
                    reason=reason,
                )
                signals.append(signal)
                # 出场后删除持仓
                del self.positions[symbol]
        return signals

    def _check_entry(self, symbol: str, df: pd.DataFrame, date: datetime):
        # 已在持仓中则跳过
        if symbol in self.positions:
            return None

        close_series = df["close"]
        high_series = df["high"]
        volume_series = df["volume"]

        # 计算 ADX(14)
        import pandas_ta as ta
        adx_df = df.copy()
        adx_df.ta.adx(length=self.adx_period, append=True)
        adx_col = f"ADX_{self.adx_period}"
        if adx_col not in adx_df.columns or pd.isna(adx_df[adx_col].iloc[-1]):
            return None
        adx_value = float(adx_df[adx_col].iloc[-1])
        if adx_value <= self.adx_threshold:
            return None

        # 50日最高价（排除今天）
        highs = high_series.iloc[:-1]
        if len(highs) < self.high_lookback:
            return None
        high_50 = float(highs.iloc[-self.high_lookback:].max())
        current_close = float(close_series.iloc[-1])
        if current_close <= high_50:
            return None

        # 放量确认
        if self.volume_mult > 0:
            vol_series = volume_series.iloc[:-1]
            if len(vol_series) < self.volume_lookback:
                return None
            avg_vol = float(vol_series.iloc[-self.volume_lookback:].mean())
            if avg_vol > 0:
                vol_ratio = float(volume_series.iloc[-1]) / avg_vol
                if vol_ratio < self.volume_mult:
                    return None

        # 计算突破幅度
        breakout_pct = (current_close - high_50) / high_50

        stop_loss = round(current_close * (1 - self.stop_loss_pct), 2)
        strength = 3
        reason = (
            f"[ADX50] 收盘价${current_close:.2f}突破{self.high_lookback}日高点${high_50:.2f}，"
            f"幅度+{breakout_pct*100:.1f}% | "
            f"[ADX{self.adx_period}]={adx_value:.1f}>{self.adx_threshold}"
        )
        signal = SignalEvent.create(
            symbol=symbol,
            timestamp=date,
            direction="buy",
            strength=strength,
            reason=reason,
            stop_loss=stop_loss,
        )
        # 建立持仓记录
        self.positions[symbol] = Position(
            symbol=symbol,
            entry_price=current_close,
            entry_date=date,
            highest_price=current_close,
        )
        return signal

    def _slice_to_date(self, df: pd.DataFrame, date: datetime) -> pd.DataFrame:
        """返回 date 当天之前的所有数据（包含 date 当天）"""
        mask = df.index <= date
        return df[mask]
```

---

## Chunk 3: 注册策略

**Files:**
- Modify: `strategies/registry.py` — 添加导入和注册行

- [ ] **Step 1: 添加导入**

在 `registry.py` 的 imports 区添加：
```python
from strategies.v_adx50.adx50_strategy import ADX50Strategy
```

- [ ] **Step 2: 注册**

在 `STRATEGY_REGISTRY` 字典中添加：
```python
    "v_adx50": ADX50Strategy,
```

---

## Chunk 4: 添加配置

**Files:**
- Modify: `config.yaml` — 在 `strategies:` 下添加 v_adx50 配置段

- [ ] **Step 1: 添加配置段**

在 `config.yaml` 的 `strategies:` 部分末尾添加：
```yaml
  v_adx50:
    name: ADX50 动量突破策略
    high_lookback: 50
    adx_period: 14
    adx_threshold: 25
    stop_loss_pct: 0.10
    take_profit_pct: 0.20
    volume_mult: 1.5
    volume_lookback: 20
```

---

## Chunk 5: 运行回测

- [ ] **Step 1: 运行回测**

```bash
cd /Users/wubo/Desktop/信号系统克劳德V3.1_Minimax支线/signal_system
uv run python main.py --mode backtest --strategy v_adx50 --start 2020-01-01 --end 2024-12-31
```

预期输出：年化收益、夏普比率、最大回撤、月均信号数
