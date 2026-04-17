# ADX50 动量突破策略设计

**日期**：2026-03-25
**策略 ID**：v_adx50
**策略名称**：ADX50 动量突破策略

---

## 1. 策略逻辑

### 核心规则

| 条件 | 规则 |
|------|------|
| **买入入场** | 收盘价 > 50日最高价 **且** ADX(14) > 25 |
| **止损出场** | 亏损达到 -10% |
| **止盈出场** | 收益达到 +20% |
| **方向** | 只做多（Long Only） |

### 技术指标说明

- **50日最高价**：排除当天，滚动计算过去50个交易日的最高收盘价
- **ADX(14)**：平均方向指数，周期14，衡量趋势强度
  - ADX > 25：趋势确认（顺势信号有效）
  - ADX ≤ 25：盘整（不入场）

---

## 2. 文件结构

```
strategies/v_adx50/
├── __init__.py           # 包入口
└── adx50_strategy.py     # 策略类实现

config.yaml                # 添加 v_adx50 配置段
```

---

## 3. 实现要点

### 3.1 策略类（adx50_strategy.py）

继承 `StrategyBase`，实现 `run_date()` 方法：

```python
class ADX50Strategy(StrategyBase):
    strategy_id = "v_adx50"
    strategy_name = "ADX50 动量突破策略"

    def run_date(self, date, queue):
        # 1. 出场检查：遍历 self.positions
        #    - 亏损 <= -10% 或 盈利 >= +20% → 发出场信号
        # 2. 入场扫描：遍历所有股票
        #    - 计算50日最高价（排除今天）
        #    - 计算ADX(14)
        #    - 满足条件 → 发入场信号，建立持仓记录
```

### 3.2 ADX 计算

使用 `pandas_ta` 库：

```python
import pandas_ta as ta
df.ta.adx(length=14, append=True)
# 产生列：ADX, ADX+_14, ADX-_14
```

### 3.3 入场判断（_check_entry）

1. 数据窗口：需要至少 60 根 K 线（50日 + ADX 预热），不足则跳过
2. NaN 检查：若 `ADX_14` 值为 NaN，则跳过该股票
3. 50日最高价：`df['high'].iloc[:-1].rolling(50).max().iloc[-1]`（排除今天）
4. ADX 值：`df['ADX_14'].iloc[-1]`，需 > 25
5. 放量确认：成交量（`df['volume']`） > `volume_mult` × 20日均量（**始终检查**，由 config 的 `volume_mult` 控制；若设为 0 则跳过此条件）

### 3.4 出场判断（_check_exits）

对每只持仓股票：
- `gain_pct = (current_price - entry_price) / entry_price`
- `gain_pct <= -0.10` → 止损出场
- `gain_pct >= +0.20` → 止盈出场

**入场前检查**：若 `symbol` 已在 `self.positions` 中（已有其他策略持仓），则跳过不入场。

**固定止损**：本策略**不使用**追踪止损，始终以 `entry_price` 为基准计算亏损。

### 3.5 持仓记录（Position）

入场时创建 `Position`（shares=100，由系统统一设置）：
```python
Position(
    symbol=symbol,
    entry_price=close,
    entry_date=date,
    highest_price=close,
    shares=100,           # 固定100股
)
```

**持仓文件**：使用共享 `output/shared/positions.json`，与其他策略一致。

---

## 4. 配置参数（config.yaml）

```yaml
strategies:
  v_adx50:
    name: ADX50 动量突破策略
    high_lookback: 50       # 最高价回溯周期
    adx_period: 14           # ADX 周期
    adx_threshold: 25        # ADX 入场门槛
    stop_loss_pct: 0.10     # 止损 10%
    take_profit_pct: 0.20   # 止盈 20%
    volume_mult: 1.5         # 放量倍数（非必需）
    volume_lookback: 20      # 均量周期
```

---

## 5. 回测安排

| 项目 | 说明 |
|------|------|
| **区间** | 2020-01-01 至 2024-12-31 |
| **股票池** | UNIVERSE.md 全股票池（美股625 + 港股244 + 台股60，动态合并） |
| **运行命令** | `uv run python main.py --mode backtest --strategy v_adx50 --start 2020-01-01 --end 2024-12-31` |
| **输出指标** | 年化收益、夏普比率、最大回撤、月均信号数 |

---

## 6. 与现有策略的关系

- **完全独立**：不继承 SEPAStrategy，直接继承 StrategyBase
- **无 RS 排名**：不做相对强度过滤
- **无 VCP 形态**：不做波动收缩形态检测
- **简洁优先**：保持策略逻辑清晰，便于理解和调整
