# Phase 12 — 分批止盈功能实现

> 完成日期：2026-03-16

## 背景

原有的出场策略是"一把梭"——要么全仓持有，要么触发止损/止盈时全部卖出。这会导致：
- 盈利到目标位时全部卖出，错失后续涨幅
- 止损触发时已经回吐大部分利润

## 目标

实现分批止盈（Partial Take Profit）功能：
- 盈利达到不同目标位时，分批卖出
- 兼顾"保住部分盈利"和"让利润奔跑"

---

## 实现方案

### 1. 配置参数化

在 `config.yaml` 中为每个策略添加分批止盈参数：

```yaml
strategies:
  v1_plus:
    # 现有出场参数
    stop_loss_pct: 0.10
    trailing_stop_pct: 0.20
    time_stop_days: 20
    time_stop_min_gain: 0.03

    # 新增：分批止盈参数
    partial_tp_enabled: false          # 默认关闭
    partial_tp_levels:                 # 止盈层级
      - [0.10, 0.33]               # +10% 卖出 33%
      - [0.20, 0.33]               # +20% 卖出 33%
      - [0.30, 0.34]               # +30% 卖出最后 34%
```

参数说明：
- `partial_tp_enabled`: 是否开启分批止盈
- `partial_tp_levels`: 盈利目标与卖出比例的对应关系
  - 格式：`[盈利目标(小数), 累计卖出比例(小数)]`
  - 例如 `[0.10, 0.33]` 表示盈利 10% 时，累计卖出 33%

### 2. Position 类扩展

在 `strategies/v1_wizard/sepa_minervini.py` 中：

```python
@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int = 0
    shares_sold_pct: float = 0.0  # 新增：已卖出比例（0.0-1.0）
```

### 3. SignalEvent 扩展

在 `events/base.py` 中添加 `shares_pct` 字段：

```python
@classmethod
def create(
    cls,
    symbol: str,
    timestamp: datetime,
    direction: str,
    strength: int,
    reason: str,
    stop_loss: float | None = None,
    shares_pct: float = 1.0,  # 新增：卖出比例，默认全卖
) -> "SignalEvent":
```

### 4. 出场逻辑修改

修改 `_check_exits` 方法的执行顺序：

```
1. 分批止盈检查（如果开启）
   → 达到盈利目标 → 发出部分卖出信号 → 更新 shares_sold_pct

2. 固定止损检查
   → 触发止损 → 卖出剩余部分 → 从持仓中删除

3. 追踪止盈检查
   → 触发止盈 → 卖出剩余部分 → 从持仓中删除

4. 时间止损检查
   → 触发条件 → 卖出剩余部分 → 从持仓中删除
```

---

## 修改的文件

| 文件 | 修改内容 |
|------|---------|
| `config.yaml` | 为 9 个策略添加 `partial_tp_enabled` 和 `partial_tp_levels` 参数 |
| `events/base.py` | SignalEvent.create() 添加 `shares_pct` 参数 |
| `strategies/v1_wizard/sepa_minervini.py` | Position 类添加 `shares_sold_pct`，`_check_exits()` 实现分批止盈逻辑 |

---

## 使用方法

### 开启分批止盈

在 `config.yaml` 中修改策略配置：

```yaml
strategies:
  v1_plus:
    partial_tp_enabled: true
    partial_tp_levels:
      - [0.10, 0.33]   # +10% 卖 1/3
      - [0.20, 0.33]   # +20% 卖 1/3
      - [0.30, 0.34]   # +30% 卖最后
```

### A/B 测试对比

用同一个策略进行对比回测：

```bash
# 关闭分批止盈（基准）
uv run python main.py --mode backtest --strategy v1_plus --start 2024-02-12 --end 2026-03-16

# 开启分批止盈
# 修改 config.yaml 中 partial_tp_enabled: true
uv run python main.py --mode backtest --strategy v1_plus --start 2024-02-12 --end 2026-03-16
```

---

## 分批止盈 vs 原有策略

| 场景 | 原策略（关闭） | 分批止盈（开启） |
|------|--------------|-----------------|
| 买入后 +10% | 继续持有 | 卖出 1/3，保留 2/3 继续奔跑 |
| 继续上涨至 +20% | 继续持有 | 再卖出 1/3，保留 1/3 |
| 继续上涨至 +30% | 继续持有 | 卖出最后 1/3 |
| 触发追踪止盈（-20%） | 全部平仓 | 剩余持仓全部平仓 |
| 触发止损 | 全部平仓 | 剩余持仓全部平仓 |

---

## 注意事项

1. **分批止盈与持仓追踪**：开启分批止盈后，`positions.json` 会记录 `shares_sold_pct`，下次运行时继续追踪剩余仓位

2. **回测兼容性**：分批止盈逻辑同时支持 live 和 backtest 模式

3. **建议测试**：先用小仓位/短周期回测验证效果，再决定是否开启

---

## 后续优化方向

- [ ] ATR 动态止损（根据波动率调整止损位）
- [ ] 波动率调仓（高波动少买，低波动多买）
- [ ] 连续亏损熔断（防止情绪化交易）
