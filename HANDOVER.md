# 交接文档 — 美股信号系统上下文

> 给下一个 Claude Code 会话读的文件。
> 新会话开始时，请先完整阅读 `CLAUDE.md`，再读本文件。

---

## 重要说明：数据源规范

**⚠️ 数据必须是收盘数据，不能是盘中数据！**

系统当前使用：
- **数据源**: Alpaca Market Data API（主） + Yahoo Finance（备）
- **数据类型**: 日线数据（Daily OHLCV）
- **时间点**: 收盘后数据（After-market close）
- **数据内容**: Open, High, Low, Close, Volume
- **时区**: UTC

**本地缓存规范（永久存储，优先使用）**：
- `data/cache/*.pkl` 是本地持久化存储，**永不过期，永不删除**
- 所有数据获取**优先调用本地缓存**——本地有的数据永远直接用，不重复下载
- 流程：本地有 → 直接用；本地不够新 → 增量下载；本地没有 → 全量下载
- fetch() 调用时传入 `end_date` 参数用于判断本地数据是否足够新（回测时传回测结束日，live时默认今天）
- `_load_local()` 不做历史覆盖检查，只要本地文件存在就返回

**确认要点**：
1. ✅ 所有历史数据都是收盘后的日线数据
2. ✅ 不包含盘中实时数据
3. ✅ 每日一条记录，代表当天完整交易日的数据
4. ✅ 增量更新时也是收盘后更新
5. ✅ 本地缓存永久保存，下次运行优先读取

**代码位置**：`data/fetcher.py`
- `_load_local()` - 读取本地 pkl 文件（不做时效性判断）
- `fetch()` - 三层数据获取逻辑（本地/增量/全量）
- `end_date` 参数 - 回测时传入实际回测结束日，用于 staleness 判断

---

## 1. 项目概述

这是一个**美股事件驱动交易信号系统**，基于 Mark Minervini 的 SEPA + VCP 策略（《股票魔法师》）。

- 用户是非程序员，所有代码由 Claude Code 编写和维护
- 系统每天收盘后运行，产生买入/卖出信号和 Markdown 日报
- 不自动下单，信号供人工决策参考

**当前策略：v_eps_v2（Weinstein + EPS）**

---

## 2. 系统当前状态（截至 2026-03-26）

### 已完成的阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1-3 | 系统骨架、回测验证、迭代优化 | ✅ 完成 |
| Phase 4 | 多策略架构升级（strategies/ 重构） | ✅ 完成 |
| Phase 5 | SA Quant 扫描器接入（--mode scan） | ✅ 完成 |
| Phase 6 | 卖出信号持仓追踪（positions.json） | ✅ 完成 |
| Phase 7 | V1+ 参数优化 + 股票池扩充 | ✅ 完成 |
| Phase 8 | 数据持久化三层架构 + live 模式缓存 staleness 修复 | ✅ 完成 |
| Phase 9 | 指数成分股扩池（S&P 500 + Nasdaq 100，Wikipedia 自动拉取）| ✅ 完成 |
| Phase 10 | 7 大师策略变种实施 + 样本外回测对比 | ✅ 完成 |
| Phase 11 | 多策略共享持仓（positions.json 统一，动态计算止损）| ✅ 完成 |
| Phase 12 | 分批止盈功能（Partial Take Profit，参数化配置）| ✅ 完成 |
| Phase 13 | UNIVERSE.md 三分法重构（美股/港股/台股），update_universe_sp500.py 防覆盖保护 | ✅ 完成 |

### 最新回测结果（v1_plus，高波动期 2024-02-12 至 2026-03-06）

| 阶段 | 区间 | 年化收益 | 夏普比率 | 最大回撤 | 每月信号 | 综合 |
|------|------|----------|----------|----------|---------|------|
| 样本内 | 2015-01 ~ 2024-12 | 13.0% | 1.64 | 20.2% | 1.8 | 4/6 合格 |
| 样本外 OOS | 2024-02 ~ 2026-03 | 45.3% | 1.95 | 15.3% | 6.4 | **6/6 全部达标** |

> **注**：样本外年化 45.3%（含 2026 Q1 高波动期），v1_plus 是唯一 6/6 全部达标的策略。
>
> **详细回测报告**：`docs/回测对比报告_高波动期_2024-02-12_2026-03-06.md`

---

## 3. 股票池状态

**当前手动股票池总数：775 只**
- 美股 S&P 500：503 只
- 美股 Nasdaq 100 独有：14 只
- 港股：206 只（恒生科技 + 其他港股，均本地缓存）
- 台股：52 只（均本地缓存）

**自动股票池**：Alpaca 新闻 API 每日增量抓取，约 150-200 只

**股票池文件**：`UNIVERSE.md`（唯一来源，直接编辑生效）

> ⚠️ UNIVERSE.md 是手动股票池唯一来源。修改后验证方法见 memory/MEMORY.md

---

## 4. 当前持仓（截至 2026-04-09）

**持仓文件：**
- `output/v_eps_v2/positions.json` — v_eps_v2 专用

**v_eps_v2 当前建仓（2026-04-09）：**

| 股票 | 入场价 | 股数 | 参考止损 |
|------|--------|------|---------|
| GLW | $165.18 | 6 | $151.97 |
| KLAC | $1671.84 | 0 | $1538.09 |
| NI | $48.13 | 20 | $44.28 |
| UI | $910.43 | 1 | $837.60 |

**说明**：
- KLAC 股价 $1671.84，$1000 不够买 1 手（100股），故股数为 0
- `shares: 0` 的为已卖出或资金不足以建仓
- `cigar_butt_v1a/b` 策略有 ImportError 尚未修复

---

## 5. 持仓管理规则

### 重要变更（2026-04-09）
**持仓文件已独立：**
- `output/v_eps_v2/positions.json` — v_eps_v2 专用

### 用户每日操作时机
**美股收盘后（北京时间次日约 05:00-06:00）**，告知 Claude 持仓变化，更新完再运行系统。

### 用户需要告知的内容
- 新买入：股票代码 + 实际成交均价
- 已平仓：哪只股票平掉了
- 加仓后均价变化：股票代码 + 新的加权平均成本

### Claude 负责处理的部分
- 更新对应策略的 `positions.json`（entry_price、entry_date、highest_price）
- **止损价由策略动态计算，不再存储**
  - v_eps_v2：止损 = entry_price × 92%，追踪止盈 = 从最高点回落 15%
- entry_date 保持**最早入场日期**不变
- highest_price 和 days_held 由系统自动更新

---

## 6. 每日运行命令

```bash
cd /Users/wubo/Desktop/signal_system_v_eps_v2

# 每天收盘后运行策略
uv run python main.py --mode live --strategy v_eps_v2

# 回测
uv run python main.py --mode backtest --start 2024-02-12 --end 2026-03-06 --strategy v_eps_v2

# 扫描 SA Quant 新强势股
uv run python main.py --mode scan --dry-run   # 只看结果不写入
uv run python main.py --mode scan             # 写入 UNIVERSE.md

# 补档（补充 signals.csv 历史记录）
uv run python main.py --mode backtest --start YYYY-MM-DD --end YYYY-MM-DD --strategy v_eps_v2 --save-signals
```

---

## 7. 策略体系

### 当前已注册策略（5 种）

| 策略 ID | 名称 | 核心特点 | 状态 |
|---------|------|---------|------|
| v_weinstein | Weinstein Stage 2 | SMA150 上升 + 突破 + 放量，无 RS/VCP | ✅ 正常 |
| v_eps_v2 | Weinstein + EPS | Stage 2 + EPS 增长筛选（同比+环比） | ✅ 正常 |
| cigar_butt_v1a | 捡烟蒂V1A | 格雷厄姆 NCAV 深度价值 | ❌ ImportError |
| cigar_butt_v1b | 捡烟蒂V1B | 深度价值 + 盈利过滤 | ❌ ImportError |

**已删除**：v1、v1_plus、v_weinstein 原独立策略（2026-04-09）
**注**：v_weinstein 重建为独立实现，不依赖 v1_wizard

**策略文档**：
- `docs/strategies/v_weinstein.md`
- `docs/strategies/捡烟蒂V1A_纯NCAV_策略文档.md`
- `docs/strategies/捡烟蒂V1B_深度价值盈利过滤_策略文档.md`

---

## 8. 关键文件说明

```
signal_system/
├── HANDOVER.md                ← 本文件
├── UNIVERSE.md                ← 股票池，唯一来源，直接编辑生效
├── config.yaml                ← 所有参数（active_strategy: v_eps_v2）
├── main.py                    ← 运行入口
│
├── strategies/
│   ├── base.py               ← StrategyBase 抽象基类
│   ├── registry.py           ← 策略注册表（4个策略）
│   ├── v_weinstein/          ← Weinstein Stage 2（独立实现，不依赖 v1）
│   ├── v_eps_v2/            ← Weinstein + EPS
│   ├── cigar_butt_v1a/      ← 捡烟蒂 V1A（有 ImportError）
│   └── cigar_butt_v1b/      ← 捡烟蒂 V1B（有 ImportError）
│
├── backtest/
│   └── engine.py             ← 回测引擎
│
├── signals/
│   ├── generator.py          ← 信号格式化 + CSV 存储
│   ├── report.py             ← 每日 Markdown 报告生成
│   └── positions.py         ← 持仓持久化（各策略独立文件）
│
├── universe/
│   ├── manager.py            ← 股票池合并
│   ├── index_fetcher.py     ← S&P 500 / Nasdaq 100
│   ├── alpaca_fetcher.py   ← Alpaca 新闻
│   ├── sa_scanner.py       ← SA Quant Rating
│   └── updater.py           ← 写入 UNIVERSE.md
│
├── data/
│   ├── fetcher.py           ← 数据获取（三层架构）
│   ├── cache/               ← 本地持久化（.pkl）
│   └── balance_sheet.py     ← 财务数据获取
│
└── output/
    └── v_eps_v2/
        ├── positions.json   ← v_eps_v2 专属持仓
        ├── signals.csv
        └── YYYY-MM-DD/报告.md
```

---

## 9. 系统出场条件

### v_eps_v2 出场条件

| 条件 | 触发标准 | 说明 |
|------|---------|------|
| 固定止损 | 收盘价 ≤ 入场价 × 92% | 亏损超过 8% |
| 追踪止盈 | 收盘价 ≤ 历史最高价 × 85% | 从高点回落 15% |
| 时间止损 | 持有 ≥ 60 天且涨幅 < 5% | 中长期持仓 |

---

## 10. 已知限制

| 事项 | 说明 |
|------|------|
| cigar_butt_v1a/b | `get_financials_ttm` ImportError，尚未修复 |
| cigar_butt 回测 | 有 ImportError，无法运行 |
| SA Quant 403 | SA API 有时被 Cloudflare 拦截，用户可手动截图告知 Claude |

---

## 11. 相关文档索引

| 文档 | 说明 |
|------|------|
| `README.md` | 项目概览和快速开始 |
| `HOW_TO_ADD_STRATEGY.md` | 新策略接入手册 |
| `USAGE.md` | 用户使用手册 |
| `docs/strategies/v_weinstein.md` | Weinstein Stage 2 策略详解 |

---

*文件更新时间：2026-04-14*
*下一个会话接手时，请确认 `output/v_eps_v2/positions.json` 与用户最新持仓一致。*
