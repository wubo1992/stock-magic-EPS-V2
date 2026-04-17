# Phase 6 — 卖出信号持仓追踪

**状态：已完成 ✓**
**日期：2026-03-05**
**目标：实现跨天持仓状态持久化，使卖出信号能在每日 live 运行中正常触发**

---

## 背景

Phase 1-5 的系统只有买入信号。虽然策略代码中已实现三种出场条件（`_check_exits()`），但每次 live 运行时策略都是**全新实例**，`strategy.positions` 始终为空，导致出场逻辑永远不会触发。

**根本问题**：持仓状态没有在两次运行之间保存。

---

## 解决方案

新建 `signals/positions.py`，将持仓状态序列化为 JSON 文件，在每次 live 运行前后读写：

```
live 运行流程（Phase 6 后）：
  加载 positions.json → 注入 strategy.positions
      → strategy.run_date()（检查出场 + 检查买入）
      → save positions.json（更新 highest_price / days_held，移除已出场）
```

---

## TODO 清单

- [x] 新建 `signals/positions.py`（`load_positions()` / `save_positions()`）
- [x] 修改 `main.py`：`run_live()` 中注入持仓、运行后保存
- [x] 修改 `strategies/v1_wizard/sepa_minervini.py`：
  - 删除 `if symbol in self.positions: continue`（已持仓股票也检查买入条件）
  - `_check_entry()` 结尾：已持仓时只发信号，不覆盖原有持仓记录
- [x] 初始化 `output/v1_wizard/positions.json`（写入用户 8 只实际持仓）
- [x] 将 AXTI / GLW / VRT 补入 `UNIVERSE.md`（持仓股需在 universe 内才能获取价格）
- [x] 更新 `CLAUDE.md` 和 `HANDOVER.md`（文件结构添加 positions.py / positions.json）
- [x] 运行验证：`--mode live` 确认持仓加载正常、卖出信号流程通畅

---

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 存储格式 | JSON | 人类可读，用户可直接编辑 |
| 文件位置 | `output/{strategy_id}/positions.json` | 按策略隔离，多策略不冲突 |
| highest_price 更新 | 系统自动更新（每次 live 运行） | 无需用户干预 |
| days_held 更新 | 系统自动 +1（每次 live 运行） | 无需用户干预 |
| 加仓处理 | 用户告知新均价 → Claude 手动更新 entry_price/stop_loss | 加权平均逻辑复杂，手动更准确 |
| entry_date | 始终保留最早入场日期 | 时间止损从首次建仓起算 |
| 加仓信号 | 已持仓股票满足条件时正常发出买入信号 | 用于提示用户加仓机会 |

---

## 三种出场条件

| 条件 | 触发标准 | 对应参数 |
|------|---------|---------|
| 固定止损 | 收盘价 ≤ entry_price × (1 - 10%) | `stop_loss_pct: 0.10` |
| 追踪止盈 | 收盘价 ≤ highest_price × (1 - 20%) | `trailing_stop_pct: 0.20` |
| 时间止损 | days_held ≥ 20 且 涨幅 < 3% | `time_stop_days: 20` / `time_stop_min_gain: 0.03` |

---

## positions.json 结构

```json
{
  "AAOI": {
    "symbol": "AAOI",
    "entry_price": 102.87,
    "entry_date": "2026-03-03T00:00:00+00:00",
    "highest_price": 102.87,
    "stop_loss": 92.58,
    "days_held": 2
  }
}
```

**用户可直接编辑此文件：**
- 删除某行 → 取消追踪（已手动平仓）
- 修改 entry_price → 调整实际买入价（告知 Claude 由其计算 stop_loss）
- 手动添加 → 追踪自己买入但系统未发信号的股票

---

## 用户操作约定

| 时机 | 用户操作 | Claude 操作 |
|------|---------|------------|
| 每日收盘后（4pm ET）| 告知新买入/平仓/加仓均价 | 更新 positions.json |
| 加仓后 | 告知新加权均价 | 更新 entry_price + 重算 stop_loss，entry_date 不变 |
| 平仓后 | 告知已平仓 ticker | 从 positions.json 删除该行 |
| 新买入（系统无信号）| 告知 ticker + 均价 + 日期 | 新增到 positions.json |

---

## 验证结果（2026-03-05）

运行 `uv run python main.py --mode live --strategy v1`：

```
[持仓] 已加载 1 个持仓：ATNI
[持仓] 已保存 1 个持仓：ATNI
```

positions.json 自动更新：`days_held: 0 → 1`，`highest_price` 更新为当日最高价。

---

## 本次新增 / 修改文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `signals/positions.py` | 新建 | 持仓持久化（load/save） |
| `main.py` | 修改 | run_live() 注入 + 保存持仓 |
| `strategies/v1_wizard/sepa_minervini.py` | 修改 | 删除跳过已持仓逻辑；加仓信号不覆盖持仓记录 |
| `output/v1_wizard/positions.json` | 新建 | 用户实际持仓（8只，3月3日入场） |
| `HANDOVER.md` | 新建 | 会话交接文档 |
