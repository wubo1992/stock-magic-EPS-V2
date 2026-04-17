# 如何添加新策略 — Claude Code 操作手册

> 本文档专为 Claude Code 编写。阅读后即可独立完成新策略的接入，无需修改任何现有框架代码。

---

## 1. 架构概览

系统采用**策略注册表模式**，框架与策略完全解耦：

```
main.py
  └─ strategies/registry.py   ← 根据 --strategy 参数找到对应策略类
       └─ strategies/vN_xxx/  ← 每个策略是一个独立 Python 包
            └─ 继承 StrategyBase，实现 run_date()
```

**添加新策略只涉及 3 处改动，其余框架文件一律不动：**

| 改动 | 文件 |
|------|------|
| 新建策略包 | `strategies/vN_xxx/` |
| 注册策略 | `strategies/registry.py` |
| 添加配置 | `config.yaml` |

---

## 2. 完整步骤

### Step 1：新建策略包

在 `strategies/` 下新建文件夹，命名规则：`v{版本号}_{策略简称}`，例如 `v2_momentum`。

```
strategies/
└── v2_momentum/
    ├── __init__.py
    └── momentum_strategy.py   ← 策略主文件（文件名自取）
```

**`__init__.py` 内容（固定模板）：**

```python
from strategies.v2_momentum.momentum_strategy import MomentumStrategy

__all__ = ["MomentumStrategy"]
```

---

### Step 2：实现策略类

策略类必须继承 `StrategyBase`，并实现 `run_date()` 方法。

**最小可运行模板：**

```python
"""
strategies/v2_momentum/momentum_strategy.py — 动量策略V2

策略逻辑：（在这里描述策略原理）
"""

from datetime import datetime, timezone

import pandas as pd

from events import EventQueue, SignalEvent
from strategies.base import StrategyBase


class MomentumStrategy(StrategyBase):
    # ── 必须定义这两个类属性 ────────────────────────────
    strategy_id = "v2_momentum"      # 输出文件夹名，唯一，不得与已有策略重复
    strategy_name = "动量策略V2"      # 显示名称，用于报告标题和控制台输出

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
    ) -> None:
        super().__init__(strategy_config, market_data)
        # 从 strategy_config（即 config.yaml 中 strategies.v2 段）读取参数
        # self.cfg 即 strategy_config，由父类赋值
        self.lookback = self.cfg.get("lookback", 20)
        # ... 其他参数

    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]:
        """
        对单个交易日运行策略。

        职责：
          1. 检查出场条件（已持仓的股票）
          2. 检查入场条件（未持仓的股票）
          3. 把产生的 SignalEvent 放入 queue，同时 return 为列表

        参数：
            date:  当前交易日（带 UTC 时区的 datetime）
            queue: 事件队列，用 queue.put(signal) 推送信号

        返回：
            本日产生的所有 SignalEvent 列表（含买入和卖出）
        """
        signals = []

        for symbol, df in self.market_data.items():
            df_to_date = self._slice_to_date(df, date)
            if len(df_to_date) < self.lookback + 10:
                continue

            signal = self._check_entry(symbol, df_to_date, date)
            if signal:
                queue.put(signal)
                signals.append(signal)

        return signals

    def _check_entry(self, symbol: str, df: pd.DataFrame, date: datetime):
        """返回 SignalEvent 或 None。"""
        # 示例：写你的选股逻辑
        close = df["close"]
        # ...

        # 产生买入信号时：
        reason = "[趋势] ... | [RS] ... | [突破] ..."
        stop_loss = float(close.iloc[-1]) * 0.90
        return SignalEvent.create(
            symbol=symbol,
            timestamp=date,
            direction="buy",      # "buy" / "sell" / "watch"
            strength=3,           # 1-5
            reason=reason,
            stop_loss=stop_loss,
        )
```

**关键约束（违反会报错）：**

| 约束 | 说明 |
|------|------|
| `strategy_id` 唯一 | 不能与 `v1_wizard` 重复，否则输出文件夹会冲突 |
| `run_date` 必须 return | 同时 put 进 queue 并 return，两者都要 |
| `_slice_to_date` 已由父类提供 | 直接调用 `self._slice_to_date(df, date)` |
| `strategy_config` 是专属段 | 收到的是 `config.yaml → strategies.v2:` 的内容，不是整个 config |

---

### Step 3：注册策略

编辑 `strategies/registry.py`，添加两行：

```python
from strategies.v1_wizard.sepa_minervini import SEPAStrategy
from strategies.v2_momentum.momentum_strategy import MomentumStrategy   # ← 新增

STRATEGY_REGISTRY: dict[str, type] = {
    "v1": SEPAStrategy,
    "v2": MomentumStrategy,   # ← 新增（key = --strategy 参数的值）
}
```

注意：`"v2"` 是 CLI 中 `--strategy v2` 使用的键，与 `strategy_id = "v2_momentum"` 是两个不同概念：
- CLI 键（`"v2"`）：短，用于命令行
- `strategy_id`（`"v2_momentum"`）：完整，用于输出文件夹名

---

### Step 4：在 config.yaml 添加配置段

在 `config.yaml` 的 `strategies:` 下添加新策略的参数段：

```yaml
active_strategy: v1   # 默认策略（不改）

strategies:
  v1:                 # 已有，不动
    name: 魔法师策略V1
    sma_short: 50
    # ...

  v2:                 # ← 新增
    name: 动量策略V2
    description: "（策略描述）"
    lookback: 20
    # ... 其他参数（与策略类中 self.cfg.get(...) 对应）
```

---

## 3. 验证

添加完成后，依次运行：

```bash
cd /Users/wubo/Desktop/信号系统克劳德V3.1_Minimax支线/signal_system

# 1. 确认 import 无报错
uv run python -c "from strategies.registry import get_strategy; print(get_strategy('v2'))"

# 2. 短区间回测（快速验证逻辑能跑通）
uv run python main.py --mode backtest --start 2024-01-01 --end 2024-06-30 --strategy v2

# 3. 完整区间样本内外对比
uv run python main.py --mode backtest --start 2020-01-01 --end 2026-03-03 --strategy v2 --split
```

预期输出路径：
- CSV：`output/v2_momentum/signals.csv`
- 报告：`output/v2_momentum/YYYY-MM-DD/报告_动量策略V2_YYYY-MM-DD.md`

---

## 4. StrategyBase 提供的工具方法

```python
# 父类 strategies/base.py 提供，直接在策略类中调用：

self.cfg          # dict，当前策略的 config.yaml 配置段
self.market_data  # dict[str, pd.DataFrame]，全部股票历史数据

self._slice_to_date(df, date)
# 返回 df[df.index <= date]，并自动处理时区
# 用于在回测中"假装只能看到 date 当天及之前的数据"
```

---

## 5. SignalEvent.create() 参数说明

```python
SignalEvent.create(
    symbol    = "NVDA",              # 股票代码
    timestamp = date,                # datetime（带 UTC 时区）
    direction = "buy",               # "buy" / "sell" / "watch"
    strength  = 3,                   # int，1-5 星
    reason    = "[趋势] ... | ...",   # 触发原因（用 | 分隔各段）
    stop_loss = 109.02,              # float 或 None（卖出信号填 None）
)
```

`reason` 的格式约定（报告解读器依赖此格式）：

```
[趋势] SMA50(...)>SMA150(...)>SMA200(...)，SMA200上升中，距52W低点+X%，距52W高点-Y% |
[RS] 相对强度排名Z% |
[VCP] VCP2T：回调18%→9%，量能收缩，末端4.2%箱体 |
[突破] 超过30日高点$123.45，幅度+1.8% |
[量能] 成交量2.3倍均量
```

如果新策略的逻辑与上述格式差异较大（例如没有 VCP），可以自定义 reason 格式，但报告中的详细解读段会退化为原文显示（不影响功能）。

---

## 6. 目前已注册策略

| CLI 键 | strategy_id | strategy_name | 文件 |
|--------|-------------|---------------|------|
| `v1` | `v1_wizard` | 魔法师策略V1 | `strategies/v1_wizard/sepa_minervini.py` |

---

## 7. 文件改动检查清单

添加新策略后，确认以下文件已处理：

- [ ] `strategies/vN_xxx/__init__.py` — 新建
- [ ] `strategies/vN_xxx/策略文件.py` — 新建，继承 StrategyBase
- [ ] `strategies/registry.py` — 添加 import 和注册表条目
- [ ] `config.yaml` — 添加 `strategies.vN:` 配置段
- [ ] 运行 `uv run python -c "from strategies.registry import get_strategy; print(get_strategy('vN'))"` 无报错
- [ ] 短区间回测通过

**不需要修改的文件：**
`main.py` / `backtest/engine.py` / `signals/generator.py` / `signals/report.py` / `run_daily.sh`

---

---

## 8. RS（相对强度）计算说明

RS 是多个策略共用的选股过滤机制，目前在以下策略中使用：

- `v1_wizard/sepa_minervini.py`（SEPAStrategy）
- `v_adx50/adx50_strategy.py`

### 计算方法

**Step 1 — 分市场基准对比**

| 市场 | 基准 ETF |
|------|---------|
| US（美股） | SPY |
| HK（港股） | ASHR |
| TW（台股） | EWT |

对每只股票计算三个周期的收益率，再与基准同期收益率对比：

```
RS分数 = (stock_252d - bench_252d) × 0.4
       + (stock_126d - bench_126d) × 0.3
       + (stock_60d  - bench_60d)  × 0.3
```

**Step 2 — 百分位排名**

在 `UNIVERSE.md` 固定股票池内排名：

```
百分位 = 超过池内 % 的股票
门槛 = 美股 top 30%（即 ≥70 百分位）
```

**关键特性**：
- 固定池：分母 = `UNIVERSE.md` 的股票数量（启动时加载，不受每日新闻股影响）
- 三个周期：252d（约1年）、126d（约半年）、60d（约3个月）
- 权重：年周期 40%，半年 30%，季 30%

**数据不足时的 Fallback**：

若基准 ETF 数据不足 252d，或某只股票缺少某个周期数据，自动降级为 legacy 模式——在池内所有股票中直接排名（不看基准）。

### 在策略中使用 RS

若新策略需要使用 RS，直接继承 `SEPAStrategy`，然后在 `_check_entry` 中调用：

```python
rs_ok, rs_value = self._check_rs(symbol, df, date)
if not rs_ok:
    return None  # RS 未通过，直接过滤
```

若需要自定义门槛，可在 `config.yaml` 中覆盖：

```yaml
strategies:
  v_your_strategy:
    rs_min_percentile: 70   # 默认 70，可调
```

*文档版本：2026-03-30（RS 固定池更新）*
*适用架构：Phase 4 多策略可扩展架构*
