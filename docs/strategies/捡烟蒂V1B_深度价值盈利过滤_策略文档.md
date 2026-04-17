# 捡烟蒂V1B - 深度价值 + 盈利过滤策略

> 在原教旨和现代市场之间取得平衡

**策略ID：** cigar_butt_v1b
**输出目录：** output/cigar_butt_v1b/

---

## 一、策略概述

**核心理念**：在深度价值（P/NCAV < 0.75）的基础上，过滤掉"真价值陷阱"（盈利恶化的公司），只选择"低估且质量过关"的标的。

**与V1A的核心区别**：
- V1A：不关心盈利，只看清算价值
- **V1B**：清算价值 + 盈利质量，两者都要满足

**核心公式**：
```
NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 全部流动负债
P/NCAV = 市值 / NCAV
```

**买入逻辑**：P/NCAV < 0.75 **且** 盈利为正 **且** 现金流为正 → 买入

---

## 二、配置参数（config.yaml）

```yaml
strategies:
  cigar_butt_v1b:
    # === 第一层：深度价值过滤 ===
    max_p_ncav: 0.75              # P/NCAV < 0.75
    max_p_netnet: 0.85             # P/Net-Net < 0.85（辅助验证）
    max_p_b: 1.2                   # P/B < 1.2
    max_p_e: 15                    # P/E < 15
    max_debt_ratio: 0.50          # 负债/资产 < 50%（比V1A严格）

    # === 第二层：盈利质量过滤 ===
    require_positive_eps: true     # 近4季度净利润 > 0
    require_positive_cfo: true     # 经营现金流 > 0
    min_profit_stability_years: 2  # 近2年至少1年盈利
    max_revenue_decline_1y: 0.20   # 营收下降 < 20%（不能崩盘）

    # === 第三层：流动性过滤 ===
    min_market_cap: 100_000_000    # 最小市值 $1亿（比V1A高）
    max_market_cap: 5_000_000_000  # 最大市值 $50亿
    min_avg_volume_20d: 100_000    # 20日均成交 > 10万股
    min_avg_dollar_volume: 1_000_000  # 20日均成交额 > $100万

    # === 仓位管理 ===
    min_positions: 15              # 最少持仓数
    max_positions: 25              # 最多持仓数（比V1A多）
    max_single_position_pct: 0.08  # 单只仓位上限 8%（比V1A低）
    equal_weight: true             # 等权配置

    # === 持有期 ===
    target_hold_months: 24         # 目标持有期 24个月
    max_hold_days: 720             # 最大持有期 24个月≈720天

    # === 退出规则 ===
    sell_p_ncav_threshold: 1.0     # P/NCAV > 1.0 卖出
    sell_pe_threshold: 25         # P/E > 25 重新评估
    stop_loss_pct: 0.20            # 跌幅 > 20% 检查（V1A是25%）
    time_stop_days: 720            # 时间止损 24个月
    min_gain_for_time_stop: 0.05  # 时间止损需盈利 < 5%

    # === 综合评分权重 ===
    score_p_ncav_weight: 0.30     # P/NCAV 权重 30%
    score_pe_weight: 0.20          # P/E 权重 20%
    score_debt_weight: 0.20        # 负债率 权重 20%
    score_cfo_weight: 0.15        # 经营现金流/股价 权重 15%
    score_size_weight: 0.15      # 市值 权重 15%

    # === Rebalance ===
    rebalance_frequency: "monthly"  # 每月rebalance

    # === 数据源 ===
    data_source: "fmp"             # FMP / yfinance
    universe: "all"                # 全市场扫描
```

---

## 三、核心计算模块

### 3.1 NCAV 计算（与V1A相同）

```python
def calculate_ncav(financial_data: dict) -> float:
    """
    计算 Net Current Asset Value

    公式：NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 全部流动负债
    """
    cash = financial_data.get("cash", 0)
    ar = financial_data.get("accounts_receivable", 0)
    inventory = financial_data.get("inventory", 0)
    current_liabilities = financial_data.get("current_liabilities", 0)

    ncav = cash + (0.75 * ar) + (0.5 * inventory) - current_liabilities
    return ncav
```

### 3.2 综合评分计算

```python
from dataclasses import dataclass
from typing import Optional
import numpy as np

@dataclass
class StockMetrics:
    """股票各项指标"""
    symbol: str
    p_ncav: float          # P/NCAV
    p_e: float             # P/E
    debt_ratio: float      # 负债/资产
    cfo_per_price: float   # 经营现金流/股价
    market_cap: float      # 市值

def calculate_composite_score(
    stock: StockMetrics,
    weights: dict,
    all_stocks: list[StockMetrics] = None,
) -> float:
    """
    计算综合评分（0-100，越高越好）

    各因子评分逻辑：
    - P/NCAV：越低越好，取百分位排名
    - P/E：越低越好，取百分位排名
    - 负债率：越低越好，取百分位排名
    - CFO/股价：越高越好，取百分位排名
    - 市值：中等偏好（避免极小盘和超大盘），取与中位数的接近度
    """
    scores = {}

    # 收集所有股票用于排名
    if all_stocks is None:
        all_stocks = [stock]

    # P/NCAV 评分（越低越好）
    p_ncav_values = [s.p_ncav for s in all_stocks if s.p_ncav > 0]
    if p_ncav_values:
        rank = sum(1 for v in p_ncav_values if v >= stock.p_ncav) / len(p_ncav_values)
        scores['p_ncav'] = rank * 100 * weights.get('p_ncav', 0.30)
    else:
        scores['p_ncav'] = 0

    # P/E 评分（越低越好，负PE给0分）
    pe_values = [s.p_e for s in all_stocks if s.p_e > 0]
    if pe_values and stock.p_e > 0:
        rank = sum(1 for v in pe_values if v >= stock.p_e) / len(pe_values)
        scores['p_e'] = rank * 100 * weights.get('p_e', 0.20)
    else:
        scores['p_e'] = 0

    # 负债率评分（越低越好）
    debt_values = [s.debt_ratio for s in all_stocks]
    if debt_values:
        rank = sum(1 for v in debt_values if v >= stock.debt_ratio) / len(debt_values)
        scores['debt'] = rank * 100 * weights.get('debt', 0.20)
    else:
        scores['debt'] = 0

    # CFO/股价评分（越高越好）
    cfo_values = [s.cfo_per_price for s in all_stocks if s.cfo_per_price > 0]
    if cfo_values and stock.cfo_per_price > 0:
        rank = sum(1 for v in cfo_values if v <= stock.cfo_per_price) / len(cfo_values)
        scores['cfo'] = rank * 100 * weights.get('cfo', 0.15)
    else:
        scores['cfo'] = 0

    # 市值评分（中等偏好，越接近中位数越好）
    cap_values = [s.market_cap for s in all_stocks]
    if cap_values:
        median_cap = np.median(cap_values)
        # 距离中位数越近分数越高，范围0-100
        cap_ratio = min(stock.market_cap, median_cap * 2) / max(stock.market_cap, median_cap * 0.5)
        cap_score = min(cap_ratio, 1.0) * 100 * weights.get('size', 0.15)
        scores['size'] = cap_score
    else:
        scores['size'] = 0

    total_score = sum(scores.values())
    return total_score
```

---

## 四、三层筛选体系

### 4.1 筛选流程图

```
全市场股票
    │
    ├─ [第一层：深度价值]
    │       P/NCAV < 0.75? ──→ 否 ──→ 排除
    │       P/Net-Net < 0.85? ──→ 否 ──→ 排除
    │       P/B < 1.2? ──→ 否 ──→ 排除
    │       P/E < 15? ──→ 否 ──→ 排除
    │       负债/资产 < 50%? ──→ 否 ──→ 排除
    │       ↓ 全部通过
    │       ↓
    ├─ [第二层：盈利质量]
    │       近4季度净利润 > 0? ──→ 否 ──→ 排除
    │       经营现金流 > 0? ──→ 否 ──→ 排除
    │       近2年至少1年盈利? ──→ 否 ──→ 排除
    │       营收下降 < 20%? ──→ 否 ──→ 排除
    │       ↓ 全部通过
    │       ↓
    ├─ [第三层：流动性]
    │       $1亿 < 市值 < $50亿? ──→ 否 ──→ 排除
    │       20日均成交额 > $100万? ──→ 否 ──→ 排除
    │       ↓ 通过
    │       ↓
    ├─ [排除清单]
    │       财报造假嫌疑? ──→ 排除
    │       审计师非标意见? ──→ 排除
    │       主营业务持续萎缩? ──→ 排除
    │       ↓
    └─ 进入候选池 → 综合评分 → 取前15-25只
```

### 4.2 过滤器实现

```python
@dataclass
class FilterCriteriaV1B:
    """V1B 过滤器标准"""

    # 第一层：深度价值
    max_p_ncav: float = 0.75
    max_p_netnet: float = 0.85
    max_p_b: float = 1.2
    max_p_e: float = 15
    max_debt_ratio: float = 0.50

    # 第二层：盈利质量
    require_positive_eps: bool = True
    require_positive_cfo: bool = True
    min_profit_stability_years: int = 2
    max_revenue_decline_1y: float = 0.20

    # 第三层：流动性
    min_market_cap: float = 100_000_000
    max_market_cap: float = 5_000_000_000
    min_avg_dollar_volume: float = 1_000_000

@dataclass
class StockFundamentalsV1B:
    """V1B 股票基本面数据"""
    symbol: str

    # 市场数据
    share_price: float
    market_cap: float
    avg_volume_20d: int
    avg_dollar_volume_20d: float

    # 估值数据
    pe_ratio: float
    pb_ratio: float

    # NCAV 相关
    cash: float
    accounts_receivable: float
    inventory: float
    current_liabilities: float
    total_liabilities: float
    total_assets: float

    # 盈利质量
    net_income_ttm: float          # 近4季度净利润
    operating_cash_flow_ttm: float  # 近4季度经营现金流
    net_income_history: list[float]  # 历史净利润（近2年）
    revenue_1y_ago: float
    revenue_current: float

    # 排除标记
    has_audit_issues: bool = False
    revenue_declining: bool = False

def passes_filters_v1b(stock: StockFundamentalsV1B, criteria: FilterCriteriaV1B) -> tuple[bool, Optional[str]]:
    """
    检查V1B股票是否通过所有过滤器
    """

    # ========== 第一层：深度价值 ==========

    # 计算 NCAV
    ncav = (stock.cash +
            0.75 * stock.accounts_receivable +
            0.5 * stock.inventory -
            stock.current_liabilities)

    if ncav <= 0:
        return False, f"NCAV={ncav:.0f} <= 0"

    p_ncav = stock.market_cap / ncav
    if p_ncav >= criteria.max_p_ncav:
        return False, f"P/NCAV={p_ncav:.2f} >= {criteria.max_p_ncav}"

    # Net-Net（更保守的清算价值）
    netnet = (stock.cash +
              0.67 * stock.accounts_receivable +
              0.5 * stock.inventory -
              stock.total_liabilities)

    if netnet > 0:
        p_netnet = stock.market_cap / netnet
        if p_netnet >= criteria.max_p_netnet:
            return False, f"P/Net-Net={p_netnet:.2f} >= {criteria.max_p_netnet}"

    # P/B
    if stock.pb_ratio >= criteria.max_p_b:
        return False, f"P/B={stock.pb_ratio:.2f} >= {criteria.max_p_b}"

    # P/E
    if stock.pe_ratio <= 0 or stock.pe_ratio >= criteria.max_p_e:
        return False, f"P/E={stock.pe_ratio:.1f} 不在 (0, {criteria.max_p_e}) 范围"

    # 负债率
    if stock.total_assets > 0:
        debt_ratio = stock.total_liabilities / stock.total_assets
        if debt_ratio >= criteria.max_debt_ratio:
            return False, f"负债率={debt_ratio:.1%} >= {criteria.max_debt_ratio:.1%}"

    # ========== 第二层：盈利质量 ==========

    # 净利润
    if criteria.require_positive_eps and stock.net_income_ttm <= 0:
        return False, f"净利润TTM={stock.net_income_ttm:.0f} <= 0"

    # 经营现金流
    if criteria.require_positive_cfo and stock.operating_cash_flow_ttm <= 0:
        return False, f"经营现金流TTM={stock.operating_cash_flow_ttm:.0f} <= 0"

    # 盈利稳定性：近2年至少1年盈利
    if criteria.min_profit_stability_years > 0:
        profitable_years = sum(1 for ni in stock.net_income_history if ni > 0)
        if profitable_years == 0:
            return False, "近2年无盈利年份"

    # 营收下降
    if criteria.max_revenue_decline_1y > 0 and stock.revenue_1y_ago > 0:
        revenue_decline = (stock.revenue_1y_ago - stock.revenue_current) / stock.revenue_1y_ago
        if revenue_decline >= criteria.max_revenue_decline_1y:
            return False, f"营收下降={revenue_decline:.1%} >= {criteria.max_revenue_decline_1y:.1%}"

    # ========== 第三层：流动性 ==========

    if not (criteria.min_market_cap <= stock.market_cap <= criteria.max_market_cap):
        return False, f"市值${stock.market_cap/1e6:.0f}M不在范围内"

    if stock.avg_dollar_volume_20d < criteria.min_avg_dollar_volume:
        return False, f"日均成交额${stock.avg_dollar_volume_20d/1e6:.1f}M < ${criteria.min_avg_dollar_volume/1e6:.1f}M"

    # ========== 排除清单 ==========

    if stock.has_audit_issues:
        return False, "审计问题"

    return True, None
```

---

## 五、入场规则

### 5.1 综合评分选股

```python
def select_entries_v1b(
    candidates: list[StockFundamentalsV1B],
    criteria: FilterCriteriaV1B,
    max_positions: int,
    weights: dict,
) -> list[str]:
    """
    V1B 选股逻辑

    1. 通过三层过滤器
    2. 计算综合评分
    3. 按评分从高到低排序
    4. 取前 max_positions 只
    """
    # 通过过滤器
    passed = []
    for stock in candidates:
        ok, reason = passes_filters_v1b(stock, criteria)
        if ok:
            passed.append(stock)

    # 计算评分
    scored = []
    for stock in passed:
        metrics = StockMetrics(
            symbol=stock.symbol,
            p_ncav=stock.market_cap / calculate_ncav(stock.__dict__),
            p_e=stock.pe_ratio if stock.pe_ratio > 0 else 999,
            debt_ratio=stock.total_liabilities / stock.total_assets if stock.total_assets > 0 else 1.0,
            cfo_per_price=stock.operating_cash_flow_ttm / stock.market_cap if stock.market_cap > 0 else 0,
            market_cap=stock.market_cap,
        )
        score = calculate_composite_score(metrics, weights, passed)
        scored.append((stock.symbol, score))

    # 按评分排序（越高越好）
    scored.sort(key=lambda x: x[1], reverse=True)

    # 取前 N 只
    selected = [symbol for symbol, _ in scored[:max_positions]]

    return selected
```

### 5.2 仓位配置

```python
def calculate_position_size_v1b(total_capital: float, num_positions: int) -> float:
    """
    V1B 仓位计算

    等权配置，每只仓位 = 总资本 / 持仓数
    单只上限 8%
    """
    position_dollar = total_capital / num_positions
    max_position = total_capital * 0.08
    return min(position_dollar, max_position)
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
    ├─ [2] 净利润转负 ? ──→ 是 ──→ 立即检查（不一定卖，但需理由）
    │       ├─ 基本面恶化 ──→ 卖出
    │       └─ 一次性因素 ──→ 继续持有
    │       ↓ 否
    │       ↓
    ├─ [3] 经营现金流连续2季度为负 ? ──→ 是 ──→ 卖出
    │       ↓ 否
    │       ↓
    ├─ [4] 跌幅 > 20% ? ──→ 检查基本面
    │       ├─ 基本面恶化 ──→ 止损卖出
    │       └─ 基本面正常 ──→ 继续持有
    │       ↓
    ├─ [5] P/E > 25 ? ──→ 重新评估，高估则卖出
    │       ↓
    ├─ [6] 持有 > 720天 且 盈利 < 5% ? ──→ 时间止损，卖出
    │       ↓
    └─ 无触发 ──→ 继续持有
```

### 6.2 退出实现

```python
@dataclass
class PositionV1B:
    """V1B 持仓（扩展版，包含盈利质量追踪）"""
    symbol: str
    shares: int
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int

    # V1B 特有字段
    entry_pe: float              # 入场时 P/E
    entry_debt_ratio: float      # 入场时负债率
    entry_ncav: float            # 入场时 NCAV
    entry_cfo: float             # 入场时经营现金流

    # 盈利质量追踪
    consecutive_negative_cfo_quarters: int = 0

def check_exit_conditions_v1b(
    pos: PositionV1B,
    current_price: float,
    current_metrics: StockMetrics,
    current_quality: dict,  # 包含净利润、经营现金流等
    current_date: datetime,
    cfg: dict,
) -> tuple[bool, str]:
    """
    V1B 退出条件检查

    返回：(是否退出, 退出原因)
    """

    days_held = (current_date - pos.entry_date).days

    # [1] P/NCAV > 阈值（价值已回归）
    if current_metrics.p_ncav > cfg.get("sell_p_ncav_threshold", 1.0):
        return True, f"P/NCAV={current_metrics.p_ncav:.2f} > 1.0，价值已回归"

    # [2] 净利润转负
    net_income = current_quality.get("net_income_ttm", 0)
    if net_income < 0:
        return True, f"净利润转负={net_income:.0f}，基本面恶化"

    # [3] 经营现金流连续2季度为负
    cfo = current_quality.get("operating_cash_flow_ttm", 0)
    if cfo < 0:
        pos.consecutive_negative_cfo_quarters += 1
        if pos.consecutive_negative_cfo_quarters >= 2:
            return True, f"经营现金流连续为负={cfo:.0f}"
    else:
        pos.consecutive_negative_cfo_quarters = 0

    # [4] 止损检查
    drawdown = (current_price / pos.entry_price - 1)
    if drawdown <= -cfg.get("stop_loss_pct", 0.20):
        return True, f"跌幅{drawdown*100:.1f}%触发止损"

    # [5] P/E > 阈值
    if current_metrics.p_e > cfg.get("sell_pe_threshold", 25):
        return True, f"P/E={current_metrics.p_e:.1f} > 25，高估"

    # [6] 时间止损
    max_days = cfg.get("time_stop_days", 720)
    min_gain = cfg.get("min_gain_for_time_stop", 0.05)
    if days_held >= max_days:
        current_gain = (current_price / pos.entry_price - 1)
        if current_gain < min_gain:
            return True, f"时间止损：持有{days_held}天，盈利仅{current_gain*100:.1f}%"

    return False, ""
```

---

## 七、策略类结构

```python
"""
strategies/cigar_butt_v1b/__init__.py
strategies/cigar_butt_v1b/strategy.py

巴菲特早期"捡烟蒂"策略 - 深度价值+盈利过滤版
"""

from datetime import datetime
from typing import Optional
import pandas as pd

from strategies.base import StrategyBase
from events import SignalEvent

class CigarButtV1BStrategy(StrategyBase):
    """
    捡烟蒂V1B策略：深度价值 + 盈利质量过滤

    核心逻辑：
    - 三层筛选：深度价值 + 盈利质量 + 流动性
    - 综合评分选股
    - 等权持有 15-25 只
    - 持有期 12-24 个月
    - 盈利质量恶化立即检查
    """

    strategy_id = "cigar_butt_v1b"
    strategy_name = "捡烟蒂V1B-深度价值+盈利过滤"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)

        # === 第一层：深度价值 ===
        self.max_p_ncav = self.cfg.get("max_p_ncav", 0.75)
        self.max_p_netnet = self.cfg.get("max_p_netnet", 0.85)
        self.max_p_b = self.cfg.get("max_p_b", 1.2)
        self.max_p_e = self.cfg.get("max_p_e", 15)
        self.max_debt_ratio = self.cfg.get("max_debt_ratio", 0.50)

        # === 第二层：盈利质量 ===
        self.require_positive_eps = self.cfg.get("require_positive_eps", True)
        self.require_positive_cfo = self.cfg.get("require_positive_cfo", True)
        self.max_revenue_decline_1y = self.cfg.get("max_revenue_decline_1y", 0.20)

        # === 第三层：流动性 ===
        self.min_market_cap = self.cfg.get("min_market_cap", 100_000_000)
        self.max_market_cap = self.cfg.get("max_market_cap", 5_000_000_000)
        self.min_avg_dollar_volume = self.cfg.get("min_avg_dollar_volume_20d", 1_000_000)

        # === 仓位管理 ===
        self.min_positions = self.cfg.get("min_positions", 15)
        self.max_positions = self.cfg.get("max_positions", 25)
        self.max_single_position_pct = self.cfg.get("max_single_position_pct", 0.08)

        # === 退出规则 ===
        self.sell_p_ncav_threshold = self.cfg.get("sell_p_ncav_threshold", 1.0)
        self.sell_pe_threshold = self.cfg.get("sell_pe_threshold", 25)
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.20)
        self.time_stop_days = self.cfg.get("time_stop_days", 720)
        self.min_gain_for_time_stop = self.cfg.get("min_gain_for_time_stop", 0.05)

        # === 综合评分权重 ===
        self.score_weights = {
            'p_ncav': self.cfg.get("score_p_ncav_weight", 0.30),
            'p_e': self.cfg.get("score_pe_weight", 0.20),
            'debt': self.cfg.get("score_debt_weight", 0.20),
            'cfo': self.cfg.get("score_cfo_weight", 0.15),
            'size': self.cfg.get("score_size_weight", 0.15),
        }

        # 持仓
        self.positions: dict[str, PositionV1B] = {}
        self._last_rebalance_month: Optional[int] = None

    def run_date(self, date: datetime, queue) -> list:
        """每日运行"""
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

            # 获取财务数据
            fundamentals = self._get_financials(symbol)
            if fundamentals is None:
                continue

            # 计算当前指标
            ncav = self._calculate_ncav(fundamentals)
            current_metrics = StockMetrics(
                symbol=symbol,
                p_ncav=fundamentals.get("market_cap", 0) / ncav if ncav > 0 else float('inf'),
                p_e=fundamentals.get("pe_ratio", 0),
                debt_ratio=fundamentals.get("total_liabilities", 0) / fundamentals.get("total_assets", 1),
                cfo_per_price=fundamentals.get("operating_cash_flow_ttm", 0) / fundamentals.get("market_cap", 1),
                market_cap=fundamentals.get("market_cap", 0),
            )

            current_quality = {
                "net_income_ttm": fundamentals.get("net_income_ttm", 0),
                "operating_cash_flow_ttm": fundamentals.get("operating_cash_flow_ttm", 0),
            }

            should_exit, reason = check_exit_conditions_v1b(
                pos, current_price, current_metrics, current_quality, date, self.cfg
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

        # 获取候选股
        candidates = self._scan_all_stocks(date)

        # 应用过滤器
        criteria = FilterCriteriaV1B(
            max_p_ncav=self.max_p_ncav,
            max_p_netnet=self.max_p_netnet,
            max_p_b=self.max_p_b,
            max_p_e=self.max_p_e,
            max_debt_ratio=self.max_debt_ratio,
            min_market_cap=self.min_market_cap,
            max_market_cap=self.max_market_cap,
            min_avg_dollar_volume=self.min_avg_dollar_volume,
        )

        # 选股
        selected_symbols = select_entries_v1b(
            candidates, criteria, self.max_positions, self.score_weights
        )

        # 调仓逻辑（同V1A，但使用V1B的Position类）
        # ... (省略，参考V1A实现)

        return signals

    def _scan_all_stocks(self, date: datetime) -> list:
        """扫描全市场"""
        raise NotImplementedError

    def _get_financials(self, symbol: str) -> Optional[dict]:
        """获取财务数据"""
        raise NotImplementedError

    def _calculate_ncav(self, fundamentals: dict) -> float:
        """计算 NCAV"""
        return (fundamentals.get("cash", 0) +
                0.75 * fundamentals.get("accounts_receivable", 0) +
                0.5 * fundamentals.get("inventory", 0) -
                fundamentals.get("current_liabilities", 0))
```

---

## 八、目录结构

```
strategies/cigar_butt_v1b/
├── __init__.py
├── strategy.py              # 主策略类
├── position.py               # 持仓数据类
└── quality_tracker.py        # 盈利质量追踪

signals/cigar_butt_v1b/
├── __init__.py
├── scanner.py                # 全市场扫描
├── filters_v1b.py            # V1B过滤器
├── scorer.py                 # 综合评分
└── financial_calculator.py   # NCAV/估值计算

output/cigar_butt_v1b/         # 回测输出目录
```

---

## 九、所需数据字段

| 分类 | 字段 | 说明 |
|------|------|------|
| 市场数据 | close, high, low, volume | 价格和成交量 |
| 估值 | pe_ratio | 市盈率 |
| 估值 | pb_ratio | 市净率 |
| 估值 | market_cap | 市值 |
| 财务 | cash | 现金 |
| 财务 | accounts_receivable | 应收账款 |
| 财务 | inventory | 存货 |
| 财务 | current_liabilities | 流动负债 |
| 财务 | total_liabilities | 总负债 |
| 财务 | total_assets | 总资产 |
| 盈利质量 | net_income_ttm | 近4季度净利润 |
| 盈利质量 | operating_cash_flow_ttm | 近4季度经营现金流 |
| 盈利质量 | net_income_history | 历史净利润（近2年） |
| 盈利质量 | revenue_1y_ago, revenue_current | 营收对比 |

---

## 十、回测配置

```yaml
backtest:
  start_date: "2010-01-01"
  end_date: "2025-12-31"
  initial_capital: 100_000
  commission: 0.001  # 0.1% 交易佣金
  slippage: 0.0005  # 0.05% 滑点
```

---

## 十一、V1A vs V1B 对比速查

| 维度 | V1A（纯NCAV） | V1B（深度价值+盈利过滤） |
|------|--------------|------------------------|
| P/NCAV | < 0.67 | < 0.75 |
| 盈利要求 | 无 | 近4季度净利润 > 0 |
| 现金流要求 | 无 | 经营现金流 > 0 |
| 负债率 | < 80% | < 50%（更严格） |
| 市值 | $5000万-$10亿 | $1亿-$50亿 |
| 持仓数 | 15-20只 | 15-25只 |
| 单只上限 | 10% | 8% |
| 止损 | 25% | 20% |
| 持有期 | 6-18月 | 12-24月 |
| 退出优先级 | P/NCAV > 1.0 | P/NCAV > 1.0 + 盈利质量恶化 |

---

*文档版本：V1.0*
*创建日期：2026-04-08*
*策略ID：cigar_butt_v1b*
