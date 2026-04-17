# 交易信号系统 — 使用说明

> 本文档供用户和 AI 助手阅读，完整描述系统的用途、结构、运行方式和维护方法。
> AI 助手阅读本文档后，应能独立完成用户提出的所有操作和修改需求。

---

## 1. 系统概述

这是一个**美股交易信号扫描工具**，基于 Mark Minervini《股票魔法师》中的 SEPA + VCP 策略。

系统采用**多策略可扩展架构**：当前主策略为「v_eps_v2」（Weinstein + EPS），各策略独立持仓。

**系统做什么：**
- 每次手动触发时，扫描 775 只股票（美股517 + 港股206 + 台股52）
- 对每只股票检查 SEPA + VCP 策略的 11 个进场条件
- 输出买入/卖出信号，并生成含详细解读的 Markdown 日报
- 追踪当前持仓，检测固定止损/追踪止盈/时间止损三种出场条件

**系统不做什么：**
- 不自动下单，不连接券商
- 不管理真实资金
- 信号仅供参考，最终决策由用户做

---

## 2. 项目位置

```
/Users/wubo/Desktop/signal_system_v_eps_v2/
```

所有命令都必须在此目录下运行。

---

## 3. 目录结构

```
signal_system/
├── main.py                  ← 唯一入口，所有功能从这里启动
├── config.yaml              ← 所有参数配置（active_strategy: v_eps_v2）
├── UNIVERSE.md              ← 股票池（~1900只，含指数成分股+新闻自动池）
├── .env                     ← Alpaca API Key（不要外泄）
├── run_daily.sh             ← 每日手动触发脚本（含 macOS 通知）
│
├── strategies/              ← 多策略包（扩展时在此添加新策略）
│   ├── __init__.py
│   ├── base.py              ← 抽象基类 StrategyBase（所有策略继承此类）
│   ├── registry.py          ← 策略注册表（STRATEGY_REGISTRY + get_strategy()）
│   ├── v_weinstein/         ← Weinstein Stage 2（独立实现，不依赖v1）
│   ├── v_eps_v2/           ← Weinstein + EPS（当前主策略）
│   ├── cigar_butt_v1a/     ← 捡烟蒂V1A（有ImportError）
│   └── cigar_butt_v1b/       ← 捡烟蒂V1B（有ImportError）
│       └── sepa_plus.py     ← 继承 V1，只改 ID/名称，参数在 config
│
├── data/
│   ├── fetcher.py           ← 数据获取（三层策略：本地直用 / 增量更新 / 全量下载）
│   └── cache/               ← 本地持久化存储（.pkl 格式，永久保存，不过期删除）
│
├── backtest/
│   └── engine.py            ← 回测引擎（逐日模拟，计算绩效指标）
│
├── signals/
│   ├── generator.py         ← 信号格式化 + 保存 CSV
│   ├── report.py            ← 每日 Markdown 报告生成器
│   └── positions.py         ← 持仓状态持久化（load/save positions.json）
│
├── universe/
│   ├── manager.py           ← 股票池管理（手动 ∪ 指数成分股 ∪ 自动池，去重）
│   ├── index_fetcher.py     ← S&P 500 / Nasdaq 100 成分股（Wikipedia，7天缓存）
│   ├── alpaca_fetcher.py    ← 从 Alpaca 新闻 API 抓取热门股票
│   ├── sa_scanner.py        ← SA Quant Rating 查询（逐 ticker 调用 SA API）
│   └── updater.py           ← SA 扫描结果写入 UNIVERSE.md（--mode scan）
│
├── events/
│   ├── base.py              ← 事件基类（SignalEvent 等）
│   └── queue.py             ← 事件总线
│
├── 实施计划/                 ← 每次迭代开发的计划文档
│   ├── Phase4_多策略架构升级.md
│   └── Phase7_V1Plus扩池与参数优化.md
│
└── output/
    └── v_eps_v2/           ← v_eps_v2 输出
        ├── signals.csv      ← 历史信号
        ├── positions.json   ← v_eps_v2 专属持仓
        └── YYYY-MM-DD/
            └── 报告_Weinstein + EPS_YYYY-MM-DD.md
```

---

## 4. 环境依赖

**运行环境：** Python 3.13，使用 `uv` 管理依赖

**依赖安装（首次使用）：**
```bash
cd /Users/wubo/Desktop/signal_system_v_eps_v2
uv sync
```

**API Key 配置（`.env` 文件已存在，无需重新配置）：**
```
ALPACA_API_KEY=<已配置>
ALPACA_SECRET_KEY=<已配置>
ALPACA_BASE_URL=<已配置>
```
`.env` 文件位于项目根目录，`main.py` 启动时自动加载。

---

## 5. 核心命令

### 5.1 实盘模式（日常使用）

```bash
cd /Users/wubo/Desktop/signal_system_v_eps_v2

# 默认使用 v1_plus（active_strategy 已设为 v1_plus）
uv run python main.py --mode live

# 或显式指定策略
uv run python main.py --mode live --strategy v1_plus
```

**做什么：**
1. 自动获取今日股票池（手动 775 只 + 新闻热门股）
2. 数据三层策略：本地有最新数据直接用 / 有旧数据则增量下载 delta / 无数据全量下载
3. 对每只股票运行魔法师策略V1+（SEPA + VCP）
4. 检查当前持仓是否触发止损/止盈/时间止损（卖出信号）
5. 打印信号到终端
6. 将信号追加到 `output/v1_plus_wizard/signals.csv`
7. 生成当日报告到 `output/v1_plus_wizard/YYYY-MM-DD/报告_魔法师调整参数版V1+_YYYY-MM-DD.md`

**运行时机：** 美股收盘后（北京时间次日约 05:00-06:00）

**也可用封装脚本运行（含 macOS 桌面通知）：**
```bash
bash run_daily.sh
```

---

### 5.2 回测模式

```bash
# 样本内回测（2015-2024）
uv run python main.py --mode backtest --start 2015-01-01 --end 2024-12-30 --strategy v1_plus

# 样本外验证（OOS）
uv run python main.py --mode backtest --start 2024-02-12 --end 2026-03-05 --strategy v1_plus

# 带样本内/外分割（前2/3为样本内，后1/3为OOS）
uv run python main.py --mode backtest --start 2020-01-01 --end 2026-03-05 --strategy v1_plus --split
```

**做什么：**
- 在指定历史区间逐日模拟策略
- 输出：胜率、盈亏比、最大回撤、年化收益、夏普比率、每月信号数
- 打印最近 5 笔交易记录

**回测只使用 `UNIVERSE.md` 手动股票池（不抓取新闻）**

---

### 5.3 历史信号补档（`--save-signals`）

切换策略后，新策略的 `signals.csv` 是空的，报告中「过去7天」会无记录。
用此命令补充历史信号：

```bash
# 回测指定区间并同时把买入信号写入 signals.csv
uv run python main.py --mode backtest --start 2026-02-19 --end 2026-03-04 --strategy v1_plus --save-signals
```

**注意：** `--save-signals` 会追加写入，运行前确认区间正确，避免重复。

---

### 5.4 SA Quant 股票池扫描

```bash
# 扫描候选池，将 Strong Buy（≥ 4.5）的股票写入 UNIVERSE.md
uv run python main.py --mode scan

# 只预览发现的股票，不修改文件
uv run python main.py --mode scan --dry-run
```

**做什么：**
1. 构建候选池：Alpaca 新闻自动缓存（`data/universe_cache.json`）+ `config.yaml` 的 `scan.extra_candidates`
2. 排除已在 UNIVERSE.md 中的股票（避免重复）
3. 逐只调用 Seeking Alpha Quant Rating API（无需账号）
4. 将评分 ≥ 4.5（Strong Buy）的新股票写入 UNIVERSE.md 的「自动扫描新增」节

**运行时机：** 每周或每月运行一次，保持股票池的动态更新。

---

### 5.5 清除本地数据

```bash
# 强制清除所有本地历史数据（清除后下次运行会全量重新下载）
uv run python -c "from data.fetcher import clear_cache; clear_cache()"

# 清除单只股票本地数据（数据异常时使用）
uv run python -c "from data.fetcher import clear_cache; clear_cache('NVDA')"
```

**注意：** 本地数据永久保存不过期，通常不需要手动清除。只在某只股票数据出现明显异常时使用。

---

## 6. 配置说明（config.yaml）

所有参数都在 `config.yaml`，**修改参数不需要改任何 Python 代码**。

### 6.1 股票池

**三个来源，自动合并：**

| 来源 | 内容 | 管理方式 |
|------|------|---------|
| 手动池 | UNIVERSE.md，775 只，美股/港股/台股三分法 | 直接编辑 UNIVERSE.md 文件 |
| 指数成分股 | S&P 500（503只）+ Nasdaq 100独有（14只），静态写入 UNIVERSE.md | 运行 `update_universe_sp500.py` 更新 |
| 自动池 | Alpaca 新闻 API 热门股 | 每次运行增量更新 |

合计去重后约 **700-800 只**（手动池与指数有重叠）。

**添加/删除手动池股票：** 直接编辑 `UNIVERSE.md`，下次运行自动生效，**无需改 `config.yaml`**

**关闭指数成分股：** 注释掉 `config.yaml` 中的 `include_indices` 段即可

---

### 6.2 策略参数（当前主策略：v_eps_v2）

```yaml
active_strategy: v_eps_v2   # 默认策略

strategies:
  v_eps_v2:
    name: Weinstein + EPS（优化版）
    # Weinstein Stage 2 技术参数
    sma_long: 150
    trend_lookback: 30
    pivot_lookback: 30
    min_breakout_pct: 0.005
    volume_mult: 1.5

    # 出场
    stop_loss_pct: 0.08      # 固定止损 -8%
    trailing_stop_pct: 0.20  # 追踪止盈：从最高点回落 -20%
    time_stop_days: 20       # 时间止损：持仓超过20天
    time_stop_min_gain: 0.03 # 时间止损：盈利不足3%才触发
```

**v_eps_v2 vs Weinstein 的差异：** v_eps_v2 在 Weinstein Stage 2 基础上增加了 EPS 增长筛选（同比+环比均正增长）

---

## 7. 输出文件说明

### 7.1 signals.csv

路径：`output/v1_plus_wizard/signals.csv`

每次 live 模式运行后自动追加，用 Excel 直接打开（UTF-8 BOM，中文正常显示）。

| 列名 | 说明 |
|------|------|
| 日期 | 信号触发当天（次日开盘执行） |
| 股票 | 股票代码 |
| 信号 | 买入 / 卖出 |
| 强度(1-5) | 1-5，基于突破幅度和成交量倍数评分 |
| 触发原因 | 完整条件描述，含[趋势][RS][VCP][突破][量能]五段 |
| 参考止损 | 建议止损价（买入价的 -10%） |

### 7.2 每日报告

路径：`output/v1_plus_wizard/YYYY-MM-DD/报告_魔法师调整参数版V1+_YYYY-MM-DD.md`

每次 live 模式运行后自动生成，包含：
- 今日新增信号（逐条完整解读五段触发条件）
- 今日出场信号（止损/止盈/时间止损触发的卖出）
- 过去 7 天信号汇总表
- 过去 7 天历史信号详细解读
- 操作提醒

### 7.3 positions.json

路径：`output/v_eps_v2/positions.json`

持仓追踪文件，live 模式运行后自动更新：
- `entry_price`：加权平均入场价（加仓后由 Claude 手动更新）
- `entry_date`：首次建仓日期
- `highest_price`：系统自动追踪的历史最高价
- `stop_loss`：`entry_price × 90%`
- `days_held`：每次 live 运行自动 +1

---

## 8. 策略逻辑简介（供 AI 参考）

### 当前已注册策略

| 策略ID | 类名 | 输出目录 | 描述 |
|--------|------|----------|------|
| v_eps_v2 | EPSV2Strategy | v_eps_v2 | Weinstein Stage 2 + EPS 增长筛选（**当前主策略**） |

### v_eps_v2 策略逻辑

```
入场条件：
  1. Weinstein Stage 2：SMA150 上升 + 价格在均线上方 + 突破30日高点 + 放量
  2. EPS 增长筛选：近 2 季同比+环比均正增长

出场（三种，任意触发）：
  - 固定止损：跌破买入价 -8%
  - 追踪止盈：从持仓最高价回落 -15%
  - 时间止损：持仓 > 60 天且盈利 < 5%
```

---

## 9. 回测历史结果（最新，v_eps_v2）

| 阶段 | 区间 | 年化收益 | 夏普比率 | 最大回撤 | 每月信号 | 综合 |
|------|------|----------|----------|----------|---------|------|
| 样本外 OOS | 2024-01 ~ 2024-06 | **20.5%** | **1.81** | **3.9%** | **12.3** | **5/6 达标** ✅ |

**注：** v_eps_v2 样本外测试区间较短（2024 上半年），年化 20.5% 但信号频率偏高（12.3/月），建议关注信号质量。

---

## 10. 持仓管理规则

### 用户操作时机

**美股收盘后（东部时间 4pm 后）**，告知 Claude 持仓变化，更新完再运行系统。

### 用户需要告知的内容

- 新买入：股票代码 + 实际成交均价
- 已平仓：哪只股票平掉了
- 加仓后均价变化：股票代码 + 新的加权平均成本

### Claude 负责的部分

- 根据均价计算止损价（v_eps_v2 = 均价 × 92%）
- 更新 `output/v_eps_v2/positions.json`
- entry_date 保持最早入场日期不变
- highest_price 和 days_held 由系统自动更新，Claude 不手动改

---

## 11. AI 助手常见操作指引

### 扩充股票池

直接编辑 `UNIVERSE.md`，在对应板块的表格末尾加一行：
```
| GOOGL | Alphabet | 搜索+云+AI |
```
保存即生效，**无需改 `config.yaml`**，下次运行时自动读取。

### 切换策略时的持仓迁移

1. 复制持仓文件：
   ```bash
   cp output/旧策略_id/positions.json output/新策略_id/positions.json
   ```
2. 补档过去几天的 signals.csv：
   ```bash
   uv run python main.py --mode backtest --start YYYY-MM-DD --end YYYY-MM-DD --strategy 新策略 --save-signals
   ```
3. 运行实盘模式确认正常

### 查看最新信号

```bash
cd /Users/wubo/Desktop/signal_system_v_eps_v2
uv run python main.py --mode live
```
报告生成在 `output/v1_plus_wizard/YYYY-MM-DD/` 目录下。

### 查看历史信号记录

```bash
cat output/v1_plus_wizard/signals.csv
# 或者直接用 Excel 打开该文件
```

### 运行指定区间回测

```bash
uv run python main.py --mode backtest --start 2023-01-01 --end 2024-12-31 --strategy v1_plus
```

### 数据异常时强制刷新

```bash
uv run python -c "from data.fetcher import clear_cache; clear_cache()"
uv run python main.py --mode live
```

### 添加新策略（V2）

1. 新建 `strategies/v2_xxx/` 文件夹
2. 创建策略类，继承 `StrategyBase`（或继承现有策略），设置 `strategy_id` 和 `strategy_name`
3. 在 `strategies/registry.py` 的 `STRATEGY_REGISTRY` 中添加：`"v2": MyV2Strategy`
4. 在 `config.yaml` 的 `strategies:` 下添加 `v2:` 配置段
5. 运行：`uv run python main.py --mode live --strategy v2`

---

## 12. 注意事项

1. **数据来源**：Alpaca IEX（免费），成交量只覆盖真实市场约 20%，成交量信号可能偏低
2. **信号日期 ≠ 执行日期**：信号日期是收盘后检测，**次日开盘执行**
3. **止损必须手动挂单**：系统只发信号，不会自动止损
4. **回测不等于实盘**：回测假设以收盘价成交，忽略滑点和冲击成本
5. **本地数据永久保存，优先使用**：历史数据存在 `data/cache/`，不过期，不删除。所有数据获取优先调用本地缓存——本地有的数据永远直接用，不重复下载。只有本地数据不满足需求时（数据不够新或根本不存在），才触发网络下载。
6. **数据三层策略**：
   - 本地数据最新 → 直接使用，零网络请求
   - 本地有但不够新（超过3天） → 增量下载缺口
   - 本地没有 → 全量下载
7. **港股/台股支持**：港股（.HK后缀）和台股（.TW后缀）均已支持，使用 ASHR/EWT 作为 RS 基准
8. **SA Quant 403**：SA API 有时被 Cloudflare 拦截，用户可手动截图告知 Claude

---

*文档版本：2026-03-26*
*架构版本：Phase 13 — UNIVERSE.md 三分法重构（美股/港股/台股）*
*当前策略：v_eps_v2（Weinstein + EPS）*
*股票池：775 只（美股517 + 港股206 + 台股52）*
