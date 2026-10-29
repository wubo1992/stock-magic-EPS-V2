# RS/SPY 相对强度集成到 v_eps_v2 策略 — 设计规格

**日期**: 2026-07-31
**作者**: Claude (superpowers:brainstorming)
**关联任务**: 把已验证的 RS/SPY 加权相对强度系统性嵌入 v_eps_v2 策略
**状态**: 设计已批准（用户 2026-07-31 答复 5 个决策点）

---

## 1. 目标

把 IBD 风格的 RS（相对强度）作为**展示字段**嵌入到 v_eps_v2 策略的每条买入信号中，方便事后复盘识别"市场领涨股"，但**不影响任何信号生成逻辑**。

---

## 2. 决策摘要（用户已批准）

| # | 决策点 | 选定方案 |
|---|---|---|
| 1 | RS 在策略里的角色 | **仅作展示**（不影响信号产生） |
| 2 | RS 比较的参照范围 | **stock/SPY 比 + 同板块排名** |
| 3 | 板块数据来源 | **外部 API**（Finnhub `stock/profile2`） |
| 4 | 缓存策略 | **增量 + 全量混合**（按需 + 批量回算工具） |
| 5 | 展示位置 | **嵌入信号 reason 字段** |

---

## 3. RS 计算公式

采用 IBD 标准的 40/20/20/20 加权季度收益：

```
RS_raw(t) = 0.4 × R_63(t)  +  0.2 × R_126(t)  +  0.2 × R_189(t)  +  0.2 × R_252(t)
RS/SPY(t) = RS_raw(t) / RS_raw(SPY, t) × 100
```

- `R_n(t) = (close[t] / close[t-n]) - 1`：n 个交易日前到当前的收益
- 需要至少 252 个交易日历史数据，否则 RS 不可计算
- 跳过去重：pkl 缓存存在 4:00 和 13:30 等多交易所时间戳，用 `groupby(normalize_date).first()` 去重
- 跳过特殊行情：股票当日收盘价 < 0.01 或日收益 > 50% 或 < -50% 时视为脏数据，返回 None

---

## 4. 架构

### 4.1 新增模块

| 文件 | 职责 |
|---|---|
| `data/rs.py` | RS 计算核心：`compute_rs(ticker, date) -> RSResult` |
| `data/rs_daily_cache.json` | 日期 → universe 内每只 ticker 的 RS 全量预计算（信号日按需懒加载） |
| `tools/analyze_rs_sector_rank.py` | 离线批量回算：给定日期 + ticker 列表，输出 RS/SPY + 板块排名 |

### 4.2 修改模块

| 文件 | 修改点 |
|---|---|
| `strategies/v_eps_v2/eps_v2_strategy.py` | `_check_entry()` EPS 通过后调用 `compute_rs()`，结果拼到 `reason` 末尾 |
| `config.yaml` v_eps_v2 段 | 新增 `rs_enabled: true`、`rs_fail_silent: true` |
| `signals/report.py` | `[RS]` 段落解释更新为新格式（RS/SPY + 板块排名 + 解读） |

### 4.3 复用的现有基础设施

- `data/sector_cache.json`（1457 只 ticker 已映射）
- `tools/sector_analysis.py` 的 `industry_to_gics()` + `fetch_profile()` + `load_cache()` + `save_cache()` + `fetch_missing_tickers()`
- `data/cache/*.pkl` 价格缓存 + `data/fetcher.py` 的 `fetch()`
- `data/fundamentals.py` 的 cache hit/miss 处理模式

> 不重复造轮子。新模块 `data/rs.py` 通过 import 复用 `tools/sector_analysis.py` 的工具函数。

---

## 5. 数据流

### 5.1 Live mode 主流程

```
main.py 启动
  ↓
run_live() / run_all_strategies() 准备 market_data（含 SPY）
  ↓
EPSV2Strategy.run_date(today, queue)
  ↓ 对每只 ticker:
    WeinsteinStrategy._check_entry() → [Stage2 通过]
    ↓
    check_eps_filter() → [EPS 通过]
    ↓
    compute_rs(ticker, today)             ← 新增
      ├─ 读 market_data[ticker] → 计算 R_63/126/189/252
      ├─ 读 market_data["SPY"]   → 计算 SPY R_63/126/189/252
      ├─ rs_spy = (0.4×R63 + 0.2×R126 + 0.2×R189 + 0.2×R252) / SPY_same × 100
      ├─ 读 data/sector_cache.json → sector_name
      │   └─ 缓存未命中 → 调 Finnhub stock/profile2 → 写缓存
      ├─ 读 data/rs_daily_cache.json[today] → sector_rank / sector_total
      └─ 返回 RSResult(rs_spy, sector, sector_rank, sector_total)
    ↓
    reason += f"[RS] RS/SPY={rs_spy:.0f} ({sector} 排名{rank}/{total})"
    ↓
    SignalEvent(direction=buy, reason=...)
```

### 5.2 Backtest mode

- `_check_entry()` 在每次回测运行时都会调用 `compute_rs()`
- 没有"全市场预计算"步骤（回测时本来就在迭代日期，每天的 RS 都能按需计算）
- 缓存仍然有效：`sector_cache.json` 全程复用，`rs_daily_cache.json` 按回测日期范围逐渐填满

### 5.3 离线批量回算

```
uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --tickers WOLF,AMD,...
  ↓
对每只 ticker 调用 compute_rs(ticker, date)
  ↓
输出表格：ticker | RS/SPY | sector | sector_rank | sector_total
```

---

## 6. 接口设计

### 6.1 `data/rs.py`

```python
@dataclass
class RSResult:
    rs_spy: float                # RS/SPY 比值 (e.g. 105.4)
    sector: str                  # GICS Level 1 (e.g. "Information Technology")
    sector_rank: int | None      # 同板块排名 (e.g. 5)
    sector_total: int | None     # 同板块总股数 (e.g. 18)
    computed_at: str            # YYYY-MM-DD


def compute_rs(ticker: str, as_of_date: str | datetime) -> RSResult | None:
    """
    计算 ticker 在 as_of_date 的 RS/SPY + 同板块排名。

    返回 None 的情形：
      - 历史不足 252 个交易日
      - SPY 数据缺失
      - 数据脏（单日涨跌幅 > 50%）
      - Finnhub 调用失败且 sector 缓存未命中

    抛异常：不抛。所有失败情形都返回 None 或带 sector='Unknown' 的 RSResult。
    """


def industry_to_gics(industry: str) -> str:
    """复用 tools/sector_analysis.py 的实现。"""


def _compute_weighted_return(close_series: pd.Series) -> float | None:
    """计算 0.4*R63 + 0.2*R126 + 0.2*R189 + 0.2*R252。"""


def _deduplicate_bars(df: pd.DataFrame) -> pd.Series:
    """对 pkl 缓存去重：groupby(normalize_date).first()。"""
```

### 6.2 `tools/analyze_rs_sector_rank.py`

```python
# 用法：
#   uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --tickers WOLF,AMD,ADI
#   uv run python tools/analyze_rs_sector_rank.py --date 2026-07-31 --from-csv signals.csv
#
# 行为：
#   - 从 data/cache/{ticker}.pkl 读价格
#   - 调用 compute_rs() 计算
#   - 按 RS/SPY 降序打印表格
```

---

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 历史不足 252 天 | `compute_rs()` 返回 None；strategy 不在 reason 中附加 `[RS]` 段落；`rs_fail_silent=true` 时无日志 |
| SPY 数据缺失 | `compute_rs()` 返回 None；策略日志打印一次警告"SPY 数据缺失，RS 计算跳过" |
| Finnhub 调用失败 + 缓存未命中 | `sector='Unknown'`、`sector_rank=None`；reason 中只显示 RS/SPY 比，不显示板块排名 |
| `rs_daily_cache.json[today]` 未命中 | 同步懒加载：对 universe 内同 sector 的所有 ticker 计算 RS 并写入缓存（首次 ~30s，后续从缓存读） |
| 数据脏（单日涨跌幅 > 50%） | 视为脏数据，跳过该 ticker；记一次警告 |
| `rs_fail_silent=false` 时 | 所有失败情形打印 `[RS] 跳过 {ticker}: {reason}` 警告 |

---

## 8. 配置项

`config.yaml` 的 `strategies.v_eps_v2` 段新增：

```yaml
v_eps_v2:
  # ... 现有参数 ...

  # RS 集成（仅作展示，不影响信号生成）
  rs_enabled: true
  rs_fail_silent: true                # RS 计算失败时是否静默跳过
  rs_sector_cache: "data/sector_cache.json"
  rs_daily_cache: "data/rs_daily_cache.json"
```

---

## 9. 性能预算

| 步骤 | 预估耗时（live mode） |
|---|---|
| `compute_rs()` 单只 ticker | < 0.05s |
| sector 缓存命中 | ~0s |
| sector 缓存未命中 + Finnhub | ~1.1s（限流 60/min） |
| `rs_daily_cache[today]` 命中 | ~0.01s（只读同 sector 内几只 ticker） |
| `rs_daily_cache[today]` 未命中（懒加载） | 首次 ~30s（universe 全量），写缓存后 ~0.01s |
| 9 只信号股 (典型) | 首次 ~30s，后续 < 1s |

---

## 10. 测试计划

### 10.1 单元测试 (`tests/test_rs.py`)

- `test_compute_weighted_return` — 验证公式（已知输入 → 已知输出）
- `test_deduplicate_bars` — 验证多交易所时间戳去重
- `test_skip_dirty_data` — 验证单日 > 50% 涨跌幅被跳过
- `test_insufficient_history` — 验证 < 252 天返回 None
- `test_sector_cache_miss` — 验证缓存未命中走 Finnhub
- `test_sector_cache_hit` — 验证缓存命中不调 Finnhub
- `test_rs_daily_cache` — 验证懒加载 + 写缓存

### 10.2 集成测试

- `test_strategy_emits_rs_section` — 模拟完整策略运行，验证 reason 含 `[RS]` 段落
- `test_strategy_no_rs_when_disabled` — `rs_enabled=false` 时无 `[RS]` 段落
- `test_strategy_no_rs_on_failure` — `rs_fail_silent=true` 且 RS 失败时不阻塞信号

### 10.3 手工验证

- 用 2026-07-31 实盘运行结果验证：当日 9 只买入信号每条 reason 都含 `[RS]`
- 用 `tools/analyze_rs_sector_rank.py` 对 20 只半导体股回算，输出表格与之前 demo 数据一致
- 验证 Finnhub 调用次数（首次 sector 缓存未命中 ticker 数 = 实际 API 调用数）

---

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Finnhub 限流（60 calls/min） | sector_cache.json 复用现有缓存（1457 只已覆盖）；新 ticker 懒加载 |
| 数据脏（WOLF 反向拆股等） | `compute_rs()` 内置单日涨跌幅 sanity check；WOLF 已知 1-for-20 反向拆股 (2025-09-29) 由 `_deduplicate_bars()` 前置过滤 |
| `rs_daily_cache.json` 膨胀 | 只缓存最近 30 天（轮转）；单日 ~6000 tickers × 30 bytes ≈ 5MB |
| 失败时阻塞信号 | `rs_fail_silent=true` 默认；RS 是纯展示，任何失败都不能阻塞已通过 Stage 2 + EPS 的信号 |
| 重复调 Finnhub | `tools/sector_analysis.py` 已有的 `fetch_missing_tickers()` 处理并发 + 缓存 |

---

## 12. 实施步骤

1. **Step 1**: 在 `data/rs.py` 实现 `RSResult` + `compute_rs()` + 辅助函数（用 `tools/sector_analysis.py` 的工具）
2. **Step 2**: 实现 `tools/analyze_rs_sector_rank.py` 批量回算 CLI
3. **Step 3**: 修改 `strategies/v_eps_v2/eps_v2_strategy.py`：EPS 通过后调 `compute_rs()`，结果拼到 reason
4. **Step 4**: 修改 `config.yaml` v_eps_v2 段：新增 `rs_enabled` 等 4 个参数
5. **Step 5**: 修改 `signals/report.py`：更新 `[RS]` 段落解释（RS/SPY + 板块排名 + 解读）
6. **Step 6**: 写 `tests/test_rs.py`（6 个单元测试 + 3 个集成测试）
7. **Step 7**: 手工验证：实盘跑一次 + 批量回算工具跑一次

---

## 13. 不在本次范围

- 不实现 RS 作为硬过滤器
- 不实现 RS 作为软性加分（强度星数变化）
- 不实现全市场 RS 排名百分比（IBD 1-99）
- 不重写 `tools/sector_analysis.py`（复用现有 sector 缓存 + GICS 映射）
- 不动 `strategies/v_weinstein/weinstein_strategy.py`（RS 只在 v_eps_v2 层注入）
- 不改 `signals/log_generator.py`（reason 已经是完整字符串，不需要特殊处理）

---

## 14. 关联文件路径

- 策略入口：`strategies/v_eps_v2/eps_v2_strategy.py`
- 父类：`strategies/v_weinstein/weinstein_strategy.py`
- 复用 sector 工具：`tools/sector_analysis.py`
- sector 缓存：`data/sector_cache.json`
- 报告生成：`signals/report.py`
- 配置文件：`config.yaml`
- 入口脚本：`main.py`