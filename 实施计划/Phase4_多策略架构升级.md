# Phase 4 — 多策略架构升级

**状态：已完成 ✓**
**日期：2026-03-05**
**目标：将单策略系统重构为可扩展多策略架构，当前策略命名为「魔法师策略V1」**

---

## 背景

Phase 1-3 已完成：
- Phase 1：基础系统搭建（数据获取、事件驱动框架）
- Phase 2：SEPA 策略实现（趋势模板 + RS + 突破 + 量能）
- Phase 3：VCP 形态识别（波动收缩模式检测）

**当前问题**：系统只有一个策略，未来加入新策略时需要大量改动框架代码。

---

## 架构目标

```
signal_system/
├── strategies/                      ← 原 strategy/ 升级
│   ├── base.py                      ← 抽象基类 StrategyBase
│   ├── registry.py                  ← 策略注册表
│   └── v1_wizard/                   ← 魔法师策略V1
│       └── sepa_minervini.py
│
└── output/
    └── v1_wizard/                   ← 按策略分文件夹
        ├── signals.csv
        └── YYYY-MM-DD/
            └── 报告_魔法师策略V1_YYYY-MM-DD.md
```

---

## TODO 清单

- [x] 创建 `实施计划/` 文件夹和本文档
- [x] 修改 `config.yaml`（`strategy:` → `active_strategy: v1` + `strategies: v1:`）
- [x] 新建 `strategies/base.py`（抽象基类 StrategyBase）
- [x] 新建 `strategies/__init__.py`
- [x] 新建 `strategies/v1_wizard/__init__.py`
- [x] 新建 `strategies/v1_wizard/sepa_minervini.py`（继承基类，`strategy_id = "v1_wizard"`）
- [x] 新建 `strategies/registry.py`（注册表 + `get_strategy()` + `list_strategies()`）
- [x] 修改 `backtest/engine.py`（接受 `strategy_cls` + `strategy_config` 参数）
- [x] 修改 `signals/generator.py`（`strategy_id` 路径：`output/{strategy_id}/signals.csv`）
- [x] 修改 `signals/report.py`（`strategy_id`/`strategy_name` 报告路径和标题）
- [x] 修改 `main.py`（`--strategy` 参数 + `resolve_strategy()` + 注册表调用）
- [x] 修改 `run_daily.sh`（`STRATEGY="v1"` 变量 + `--strategy "$STRATEGY"`）
- [x] 删除旧 `strategy/` 文件夹
- [x] 更新 `USAGE.md`（路径、命令、目录结构全面更新）
- [x] 新建 `HOW_TO_ADD_STRATEGY.md`（Claude Code 操作手册）
- [x] 验证：运行回测确认系统正常

---

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 运行模式 | 每次只跑一个策略 | 简洁，满足当前需求 |
| 输出组织 | 按策略分文件夹 | `output/v1_wizard/signals.csv` 互不干扰 |
| 配置结构 | 按策略嵌套 | `strategies.v1.sma_short` 参数隔离 |

---

## 接口设计

### StrategyBase（抽象基类）

```python
class StrategyBase(ABC):
    strategy_id: str    # 文件夹名，如 "v1_wizard"
    strategy_name: str  # 显示名，如 "魔法师策略V1"

    def __init__(self, strategy_config: dict, market_data: dict): ...

    @abstractmethod
    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]: ...
```

### 策略注册表

```python
STRATEGY_REGISTRY = {
    "v1": SEPAStrategy,   # 命令行 --strategy v1
}
```

### CLI 调用

```bash
# 实盘（指定策略）
uv run python main.py --mode live --strategy v1

# 回测（指定策略）
uv run python main.py --mode backtest --start 2024-01-01 --end 2026-03-03 --strategy v1 --split
```

---

## 验证结果（2026-03-05）

回测区间：2024-01-01 至 2026-03-03，145 只股票池

| 指标 | 数值 | 及格线 | 状态 |
|------|------|--------|------|
| 胜率 | 42.2% | > 40% | ✓ |
| 盈亏比 | 4.14 | > 1.5 | ✓ |
| 最大回撤 | 17.5% | < 25% | ✓ |
| 每月信号数 | 1.7 | 2 ~ 10 | ✗ |
| 年化收益 | 62.4% | > 15% | ✓ |
| 夏普比率 | 2.20 | > 1.0 | ✓ |
| **综合** | **5/6 合格** | — | **✓** |

报告标题显示「魔法师策略V1 信号系统」，输出路径 `output/v1_wizard/` 正确，架构重构对策略逻辑无影响。

---

## 本次新增文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `strategies/base.py` | 新建 | 抽象基类 |
| `strategies/__init__.py` | 新建 | 包入口 |
| `strategies/registry.py` | 新建 | 注册表 |
| `strategies/v1_wizard/__init__.py` | 新建 | V1 包入口 |
| `strategies/v1_wizard/sepa_minervini.py` | 新建 | 从旧 `strategy/` 迁移并继承基类 |
| `实施计划/Phase4_多策略架构升级.md` | 新建 | 本文档 |
| `HOW_TO_ADD_STRATEGY.md` | 新建 | 新策略接入手册（供 Claude Code 读） |

## 本次删除文件

| 文件 | 说明 |
|------|------|
| `strategy/sepa_minervini.py` | 内容迁移到 `strategies/v1_wizard/`，旧包删除 |
| `strategy/__init__.py` | 同上 |
