# 捡烟蒂V1A - 纯格雷厄姆NCAV策略

> 最接近原教旨的深度价值策略
>
> **策略ID：** cigar_butt_v1a
> **输出目录：** output/cigar_butt_v1a/

---

## 一、策略概述

**核心理念**：用显著低于清算价值（P/NCAV < 0.67）的价格买入平庸公司，即使公司走下坡路也能获得满意回报。

**核心公式**：
```
NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 全部流动负债
P/NCAV = 市值 / NCAV
```

**买入逻辑**：P/NCAV < 0.67 → 买入（留33%+安全边际）

---

## 二、配置参数（config.yaml）

```yaml
strategies:
  cigar_butt_v1a:
    # === 入池过滤 ===
    max_p_ncav: 0.67           # P/NCAV 必须 < 0.67
    min_market_cap: 50_000_000 # 最小市值 $5000万
    max_market_cap: 1_000_000_000  # 最大市值 $10亿
    min_avg_volume_20d: 100_000    # 20日均成交 > 10万股
    max_debt_ratio: 0.80       # 负债/资产 < 80%

    # === 仓位管理 ===
    min_positions: 15          # 最少持仓数
    max_positions: 20          # 最多持仓数
    max_single_position_pct: 0.10  # 单只仓位上限 10%
    equal_weight: true        # 等权配置

    # === 持有期 ===
    target_hold_months: 18     # 目标持有期（月）
    max_hold_days: 540         # 最大持有期 18个月≈540天

    # === 退出规则 ===
    sell_p_ncav_threshold: 1.0    # P/NCAV > 1.0 卖出
    stop_loss_pct: 0.25            # 跌幅 > 25% 检查
    force_sell_days: 540           # 持有 > 540天 强制卖出

    # === Rebalance ===
    rebalance_frequency: "monthly"  # 每月rebalance

    # === 数据源 ===
    data_source: "fmp"           # FMP / yfinance
    universe: "all"               # 全市场扫描
```

---

## 三、核心计算模块

### 3.1 NCAV 计算

```python
def calculate_ncav(financial_data: dict) -> float:
    """
    计算 Net Current Asset Value（净流动资产值）

    公式：NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 全部流动负债

    参数 financial_data:
        cash: float           # 现金及等价物
        accounts_receivable: float  # 应收账款
        inventory: float      # 存货
        current_liabilities: float  # 流动负债

    返回：
        NCAV 值（美元）
    """
    cash = financial_data.get("cash", 0)
    ar = financial_data.get("accounts_receivable", 0)
    inventory = financial_data.get("inventory", 0)
    current_liabilities = financial_data.get("current_liabilities", 0)

    ncav = cash + (0.75 * ar) + (0.5 * inventory) - current_liabilities
    return ncav
```

### 3.2 P/NCAV 计算

```python
def calculate_p_ncav_ratio(market_cap: float, ncav: float) -> float:
    """
    计算 P/NCAV比率

    公式：P/NCAV = 市值 / NCAV

    返回：
        P/NCAV 比率，越低越便宜
    """
    if ncav <= 0:
        return float('inf')  # NCAV为负或零视为无效
    return market_cap / ncav
```

---

## 四、入池过滤器

### 4.1 过滤器流程图

```
全市场股票
    │
    ├─ [1] 深度价值过滤
    │       P/NCAV < 0.67? ──→ 否 ──→ 排除
    │       ↓ 是
    │       ↓
    ├─ [2] 市值过滤
    │       $5000万 < 市值 < $10亿? ──→ 否 ──→ 排除
    │       ↓ 是
    │       ↓
    ├─ [3] 流动性过滤
    │       20日均成交 > 10万股? ──→ 否 ──→ 排除
    │       ↓ 是
    │       ↓
    ├─ [4] 财务健康过滤
    │       负债/资产 < 80%? ──→ 否 ──→ 排除
    │       ↓ 是
    │       ↓
    └─ [5] 排除清单
            正在清算中? ──→ 是 ──→ 排除
            财报造假嫌疑? ──→ 是 ──→ 排除
            审计师非标意见? ──→ 是 ──→ 排除
                ↓ 否
            进入候选池
```

### 4.2 过滤器实现

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class FilterCriteria:
    """V1A 过滤器标准"""
    max_p_ncav: float = 0.67
    min_market_cap: float = 50_000_000
    max_market_cap: float = 1_000_000_000
    min_avg_volume: int = 100_000
    max_debt_ratio: float = 0.80

@dataclass
class StockFundamentals:
    """股票基本面数据"""
    symbol: str
    market_cap: float
    share_price: float
    cash: float
    accounts_receivable: float
    inventory: float
    current_liabilities: float
    total_liabilities: float
    total_assets: float
    avg_volume_20d: int
    is_liquidating: bool
    has_audit_issues: bool

def passes_filters(stock: StockFundamentals, criteria: FilterCriteria) -> tuple[bool, Optional[str]]:
    """
    检查股票是否通过所有过滤器

    返回：
        (通过, 失败原因)
    """
    # 计算 NCAV
    ncav = (stock.cash +
            0.75 * stock.accounts_receivable +
            0.5 * stock.inventory -
            stock.current_liabilities)

    if ncav <= 0:
        return False, "NCAV <= 0"

    # 计算 P/NCAV
    p_ncav = stock.market_cap / ncav
    if p_ncav >= criteria.max_p_ncav:
        return False, f"P/NCAV={p_ncav:.2f} >= {criteria.max_p_ncav}"

    # 市值过滤
    if not (criteria.min_market_cap <= stock.market_cap <= criteria.max_market_cap):
        return False, f"市值${stock.market_cap/1e6:.0f}M不在范围内"

    # 流动性过滤
    if stock.avg_volume_20d < criteria.min_avg_volume:
        return False, f"成交量{stock.avg_volume_20d} < {criteria.min_avg_volume}"

    # 负债率过滤
    if stock.total_assets > 0:
        debt_ratio = stock.total_liabilities / stock.total_assets
        if debt_ratio >= criteria.max_debt_ratio:
            return False, f"负债率{debt_ratio:.1%} >= {criteria.max_debt_ratio:.1%}"

    # 排除清单
    if stock.is_liquidating:
        return False, "正在清算中"
    if stock.has_audit_issues:
        return False, "审计问题"

    return True, None
```

---

## 五、入场规则

### 5.1 扫描频率

- **频率**：每月第一个交易日
- **候选池**：全市场所有股票

### 5.2 入场逻辑

```python
def select_entries(candidates: list[StockFundamentals], criteria: FilterCriteria, max_positions: int) -> list[str]:
    """
    选择入场标的

    逻辑：
    1. 计算每只股票的 P/NCAV
    2. 按 P/NCAV 从低到高排序
    3. 取最低的 max_positions 只
    4. 等权配置

    返回：
        入选股票代码列表
    """
    # 计算 P/NCAV 并排序
    scored = []
    for stock in candidates:
        ncav = (stock.cash +
                0.75 * stock.accounts_receivable +
                0.5 * stock.inventory -
                stock.current_liabilities)
        if ncav > 0:
            p_ncav = stock.market_cap / ncav
            scored.append((stock.symbol, p_ncav))

    # 按 P/NCAV 从低到高排序（越低越优先）
    scored.sort(key=lambda x: x[1])

    # 取最低的 N 只
    selected = [symbol for symbol, _ in scored[:max_positions]]

    return selected
```

### 5.3 仓位配置

```python
def calculate_position_size(total_capital: float, num_positions: int, max_pct: float) -> float:
    """
    计算每只股票的买入金额

    公式：position_dollar = min(total_capital / num_positions, total_capital * max_pct)

    等权配置，每只仓位相等
    """
    equal_share = total_capital / num_positions
    max_share = total_capital * max_pct
    return min(equal_share, max_share)

def calculate_shares(position_dollar: float, price: float) -> int:
    """计算买入股数（向下取整）"""
    return int(position_dollar / price)
```

---

## 六、退出规则

### 6.1 退出流程图

```
每日持仓检查：
    │
    ├─ [1] P/NCAV > 1.0 ? ──→ 是 ──→ 全部卖出（价值已回归）
    │       ↓ 否
    │       ↓
    ├─ [2] 持有 > 540天 ? ──→ 是 ──→ 全部卖出（强制）
    │       ↓ 否
    │       ↓
    ├─ [3] 跌幅 > 25% ? ──→ 检查基本面
    │       ├─ 基本面恶化 ──→ 止损卖出
    │       └─ 基本面正常 ──→ 继续持有
    │       ↓
    ├─ [4] 公司宣布清算 ──→ 持有至清算完成
    │
    └─ 无触发 ──→ 继续持有
```

### 6.2 退出实现

```python
@dataclass
class Position:
    """持仓"""
    symbol: str
    shares: int
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int

    @property
    def market_value(self) -> float:
        return self.shares * self.highest_price

    @property
    def gain_pct(self) -> float:
        return (self.highest_price / self.entry_price - 1) * 100

def check_exit_conditions(
    pos: Position,
    current_price: float,
    current_ncav: float,
    current_market_cap: float,
    fundamental_worsened: bool,
    current_date: datetime,
    max_hold_days: int = 540,
    sell_p_ncav: float = 1.0,
    stop_loss_pct: float = 0.25,
) -> tuple[bool, str]:
    """
    检查退出条件

    返回：
        (是否退出, 退出原因)
    """
    days_held = (current_date - pos.entry_date).days

    # [1] P/NCAV > 阈值（价值已回归）
    if current_ncav > 0:
        current_p_ncav = current_market_cap / current_ncav
        if current_p_ncav > sell_p_ncav:
            return True, f"P/NCAV={current_p_ncav:.2f} > {sell_p_ncav}，价值已回归"

    # [2] 持有期超限
    if days_held >= max_hold_days:
        return True, f"持有{days_held}天超过{max_hold_days}天，强制卖出"

    # [3] 止损检查
    drawdown = (current_price / pos.entry_price - 1)
    if drawdown <= -stop_loss_pct:
        if fundamental_worsened:
            return True, f"跌幅{drawdown*100:.1f}% + 基本面恶化，止损"
        # 基本面正常则继续持有（深度价值容忍短期波动）

    # [4] 公司清算
    # （在 Position 数据中标记，触发后持有至清算完成）

    return False, ""
```

---

## 七、策略类结构

```python
"""
strategies/cigar_butt_v1a/__init__.py
strategies/cigar_butt_v1a/strategy.py

巴菲特早期"捡烟蒂"策略 - 纯格雷厄姆NCAV版
"""

from datetime import datetime
from typing import Optional
import pandas as pd

from strategies.base import StrategyBase
from events import SignalEvent

class CigarButtV1AStrategy(StrategyBase):
    """
    捡烟蒂V1A策略：纯格雷厄姆NCAV深度价值

    核心逻辑：
    - 全市场扫描 P/NCAV < 0.67 的股票
    - 等权持有 15-20 只
    - 持有期 6-18 个月
    - P/NCAV > 1.0 或持有超限则卖出
    """

    strategy_id = "cigar_butt_v1a"
    strategy_name = "捡烟蒂V1A-纯NCAV"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)

        # 配置参数
        self.max_p_ncav = self.cfg.get("max_p_ncav", 0.67)
        self.min_market_cap = self.cfg.get("min_market_cap", 50_000_000)
        self.max_market_cap = self.cfg.get("max_market_cap", 1_000_000_000)
        self.min_avg_volume = self.cfg.get("min_avg_volume_20d", 100_000)
        self.max_debt_ratio = self.cfg.get("max_debt_ratio", 0.80)

        self.min_positions = self.cfg.get("min_positions", 15)
        self.max_positions = self.cfg.get("max_positions", 20)
        self.max_single_position_pct = self.cfg.get("max_single_position_pct", 0.10)

        self.sell_p_ncav_threshold = self.cfg.get("sell_p_ncav_threshold", 1.0)
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.25)
        self.max_hold_days = self.cfg.get("force_sell_days", 540)

        # 持仓
        self.positions: dict[str, Position] = {}

        # 上次 rebalance 日期
        self._last_rebalance_month: Optional[int] = None

    def run_date(self, date: datetime, queue) -> list:
        """
        每日运行：检查退出 → 月末rebalance → 检查入场
        """
        signals = []

        # 1. 检查持仓退出
        exit_signals = self._check_exits(date)
        signals.extend(exit_signals)

        # 2. 月末 rebalance
        current_month = date.month
        if self._last_rebalance_month != current_month:
            self._last_rebalance_month = current_month
            rebalance_signals = self._do_rebalance(date)
            signals.extend(rebalance_signals)

        return signals

    def _check_exits(self, date: datetime) -> list:
        """检查所有持仓的退出条件"""
        signals = []
        for symbol, pos in list(self.positions.items()):
            df = self.market_data.get(symbol)
            if df is None or len(df) < 60:
                continue

            current_row = df.iloc[-1]
            current_price = float(current_row["close"])
            current_date = current_row.name

            # 获取最新财务数据（需调用财务API）
            fundamentals = self._get_financials(symbol)
            if fundamentals is None:
                continue

            # 计算当前 NCAV
            ncav = self._calculate_ncav(fundamentals)
            market_cap = fundamentals.get("market_cap", 0)

            # 检查退出
            should_exit, reason = self._check_exit_single(
                pos, current_price, ncav, market_cap,
                self._check_fundamental_worsened(symbol, fundamentals),
                date
            )

            if should_exit:
                sig = SignalEvent.create(
                    symbol=symbol,
                    timestamp=date,
                    direction="sell",
                    strength=3,
                    reason=reason,
                    shares=pos.shares,
                )
                signals.append(sig)
                del self.positions[symbol]

        return signals

    def _do_rebalance(self, date: datetime) -> list:
        """月末重新扫描并调仓"""
        signals = []

        # 获取全市场候选股（需要API获取所有股票的财务数据）
        candidates = self._scan_all_stocks(date)

        # 通过过滤器
        filtered = self._apply_filters(candidates)

        # 按 P/NCAV 排序，取前 N 只
        filtered.sort(key=lambda x: x.p_ncav)
        selected = filtered[:self.max_positions]

        # 计算当前持仓
        current_holdings = set(self.positions.keys())
        new_selections = set(s.symbol for s in selected)

        # 卖出不在新名单中的
        to_sell = current_holdings - new_selections
        for symbol in to_sell:
            pos = self.positions[symbol]
            sig = SignalEvent.create(
                symbol=symbol,
                timestamp=date,
                direction="sell",
                strength=2,
                reason="rebalance: 不在新候选名单",
                shares=pos.shares,
            )
            signals.append(sig)
            del self.positions[symbol]

        # 买入需要新增的
        to_buy = new_selections - current_holdings
        available_capital = self._calculate_available_capital()
        position_size = available_capital / len(to_buy) if to_buy else 0

        for symbol in to_buy:
            stock = next(s for s in selected if s.symbol == symbol)
            price = stock.current_price
            shares = int(position_size / price)

            if shares > 0:
                sig = SignalEvent.create(
                    symbol=symbol,
                    timestamp=date,
                    direction="buy",
                    strength=2,
                    reason=f"NCAV选股: P/NCAV={stock.p_ncav:.2f}",
                    shares=shares,
                )
                signals.append(sig)

                # 记录持仓
                self.positions[symbol] = Position(
                    symbol=symbol,
                    shares=shares,
                    entry_price=price,
                    entry_date=date,
                    highest_price=price,
                    days_held=0,
                )

        return signals

    def _scan_all_stocks(self, date: datetime) -> list:
        """扫描全市场，返回候选股列表"""
        # TODO: 实现全市场扫描
        # 使用 FMP / yfinance API 获取所有股票
        # 需要获取：市值、现金、应收账款、存货、流动负债、总负债、总资产、20日均成交
        raise NotImplementedError

    def _apply_filters(self, candidates: list) -> list:
        """应用所有过滤器，返回通过列表"""
        filtered = []
        for stock in candidates:
            passed, reason = passes_filters(stock, FilterCriteria(
                max_p_ncav=self.max_p_ncav,
                min_market_cap=self.min_market_cap,
                max_market_cap=self.max_market_cap,
                min_avg_volume=self.min_avg_volume,
                max_debt_ratio=self.max_debt_ratio,
            ))
            if passed:
                filtered.append(stock)
        return filtered

    def _calculate_ncav(self, fundamentals: dict) -> float:
        """计算 NCAV"""
        return (fundamentals.get("cash", 0) +
                0.75 * fundamentals.get("accounts_receivable", 0) +
                0.5 * fundamentals.get("inventory", 0) -
                fundamentals.get("current_liabilities", 0))

    def _get_financials(self, symbol: str) -> Optional[dict]:
        """获取股票财务数据"""
        # TODO: 调用 FMP API
        raise NotImplementedError

    def _check_fundamental_worsened(self, symbol: str, fundamentals: dict) -> bool:
        """检查基本面是否恶化"""
        # TODO: 对比上季度数据
        return False

    def _calculate_available_capital(self) -> float:
        """计算可用资金"""
        # TODO: 计算总资产 - 已持仓市值
        raise NotImplementedError

    def _check_exit_single(self, pos: Position, current_price: float,
                           ncav: float, market_cap: float,
                           fundamental_worsened: bool, date: datetime) -> tuple[bool, str]:
        """检查单只持仓的退出条件"""
        days_held = (date - pos.entry_date).days

        # [1] P/NCAV > 阈值
        if ncav > 0:
            p_ncav = market_cap / ncav
            if p_ncav > self.sell_p_ncav_threshold:
                return True, f"P/NCAV={p_ncav:.2f} > {self.sell_p_ncav_threshold}，价值已回归"

        # [2] 持有期超限
        if days_held >= self.max_hold_days:
            return True, f"持有{days_held}天超过{self.max_hold_days}天"

        # [3] 止损
        drawdown = (current_price / pos.entry_price - 1)
        if drawdown <= -self.stop_loss_pct:
            if fundamental_worsened:
                return True, f"跌幅{drawdown*100:.1f}% + 基本面恶化"

        return False, ""
```

---

## 八、目录结构

```
strategies/cigar_butt_v1a/
├── __init__.py
├── strategy.py          # 主策略类
└── position.py          # 持仓数据类

signals/cigar_butt_v1a/
├── __init__.py
├── scanner.py           # 全市场扫描
├── filters.py           # 过滤器实现
├── ncav_calculator.py   # NCAV计算
└── validator.py         # 数据验证

output/cigar_butt_v1a/   # 回测输出目录
```

---

## 九、所需数据字段

| 数据源 | 字段 | 说明 |
|--------|------|------|
| 市场数据 | close, high, low, volume | 价格和成交量 |
| 财务数据 | cash | 现金及等价物 |
| 财务数据 | accounts_receivable | 应收账款 |
| 财务数据 | inventory | 存货 |
| 财务数据 | current_liabilities | 流动负债 |
| 财务数据 | total_liabilities | 总负债 |
| 财务数据 | total_assets | 总资产 |
| 财务数据 | market_cap | 市值 |

---

## 十、回测配置

```yaml
backtest:
  start_date: "2010-01-01"
  end_date: "2025-12-31"
  initial_capital: 100_000
  commission: 0.001  # 0.1% 交易佣金
  slippage: 0.0005   # 0.05% 滑点
```

---

*文档版本：V1.0*
*创建日期：2026-04-08*
*策略ID：cigar_butt_v1a*
