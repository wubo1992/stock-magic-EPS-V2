# Phase 1 — 系统骨架搭建

**状态：已完成 ✓**
**日期：2026-03-03**
**目标：从零搭建事件驱动交易信号系统的基础骨架**

---

## 背景

用户需要一个美股交易信号系统：
- 基于技术分析自动产生买入/卖出信号
- 用户不懂编程，所有代码由 Claude Code 编写维护
- 系统需要可回测、可实盘运行，两套模式共用同一套策略代码

---

## 架构目标

```
signal_system/
├── main.py              ← 运行入口
├── config.yaml          ← 所有可调参数
├── data/
│   └── fetcher.py       ← 数据获取（Alpaca + Yahoo Finance 备用）
├── events/
│   ├── base.py          ← 事件基类（BarEvent / SignalEvent）
│   └── queue.py         ← 事件总线（FIFO 队列）
├── strategy/
│   └── sepa_minervini.py ← 策略核心（Phase 2-3 逐步完善）
├── signals/
│   └── generator.py     ← 信号格式化 + CSV 存储
└── backtest/
    └── engine.py        ← 回测引擎（Phase 2 完成）
```

---

## TODO 清单

- [x] 创建项目文件夹结构
- [x] 编写 `pyproject.toml`（uv 管理依赖：alpaca-trade-api、yfinance、pyyaml、python-dotenv）
- [x] 编写 `.env.example`（Alpaca API Key 模板）
- [x] 实现 `events/base.py`（BarEvent、SignalEvent 事件类）
- [x] 实现 `events/queue.py`（EventQueue，基于 collections.deque）
- [x] 实现 `data/fetcher.py`（Alpaca IEX 主源，Yahoo Finance 备用，本地缓存）
- [x] 实现 `strategy/sepa_minervini.py` 初始版（趋势模板 + 突破检测）
- [x] 实现 `signals/generator.py`（信号格式化，追加写入 `output/signals.csv`）
- [x] 实现 `main.py` 骨架（`--mode live` 基本流程：取数据 → 运行策略 → 输出信号）
- [x] 编写 `config.yaml`（strategy 参数段、data 参数段）
- [x] 验证：对单只股票成功输出信号

---

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 事件驱动架构 | EventQueue + SignalEvent | 回测和实盘复用同一套代码 |
| 数据源 | Alpaca IEX 主 + Yahoo Finance 备 | Alpaca 免费、稳定；Yahoo 作兜底 |
| 参数管理 | config.yaml 统一 | 用户可调参数不需要改代码 |
| 包管理 | uv | 比 pip 更快，锁定依赖版本 |
| 缓存策略 | .pkl 文件，3天有效 | 避免每次运行重复下载历史数据 |

---

## 核心数据结构

```python
# 信号事件
class SignalEvent:
    symbol: str
    timestamp: datetime
    direction: str      # "buy" / "sell"
    strength: int       # 1-5
    reason: str
    stop_loss: float

# 市场数据格式（DataFrame）
# 列：open, high, low, close, volume
# 索引：UTC datetime
```

---

## 本次新增文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `main.py` | 新建 | 运行入口 |
| `config.yaml` | 新建 | 全局配置 |
| `events/base.py` | 新建 | 事件基类 |
| `events/queue.py` | 新建 | 事件总线 |
| `data/fetcher.py` | 新建 | 数据获取 |
| `strategy/sepa_minervini.py` | 新建 | 策略初始版 |
| `signals/generator.py` | 新建 | 信号输出 |
| `.env.example` | 新建 | API Key 模板 |
| `USAGE.md` | 新建 | 用户使用手册 |
