# Phase 7 — V1+ 策略参数优化 + 股票池扩充

**状态：已完成 ✓**
**日期：2026-03-05**
**目标：通过控制变量法优化参数、扩充股票池，将每月信号频率从 0.8 提升至 2+**

---

## 背景

Phase 6 完成后，策略基准（v1，186 只股票池）样本内回测结果：
- 每月信号数：**0.8/月**（目标 2~10，不达标）
- 年化收益：12.3%，夏普 1.50，胜率 41.8%，盈亏比 4.91

信号太少的根本原因是：**VCP 末端箱体（8%）过严 + 股票池不够大**。

---

## 步骤一：创建 v1_plus 策略（子类架构）

新建 `strategies/v1_plus/` 包，`SEPAPlusStrategy` 继承自 `SEPAStrategy`，只覆盖 ID 和名称，参数通过 `config.yaml` 独立配置。

```
strategies/v1_plus/
├── __init__.py
└── sepa_plus.py   ← class SEPAPlusStrategy(SEPAStrategy), strategy_id = "v1_plus_wizard"
```

`strategies/registry.py` 注册：
```python
"v1_plus": SEPAPlusStrategy,
```

---

## 步骤二：live_mode Bug 修复

**问题**：Live 模式每次运行时 `_check_entry()` 会自动为触发买入条件的股票创建 Position 记录，导致回测中的出场逻辑（`_check_exits()`）在 live 实例中无法对"当次 live 中刚买入的信号股"正确管理。

**修复**：给 `SEPAStrategy.__init__()` 加 `live_mode: bool = False` 参数：
- `live_mode=True`：不自动创建 Position（持仓由用户手动告知后写入 positions.json）
- `live_mode=False`（默认，回测用）：保留自动创建逻辑，供出场逻辑使用

`main.py` 的 `run_live()` 传入 `live_mode=True`。

---

## 步骤三：控制变量参数测试（均基于 186 只股票池，2015-2024）

| 测试组 | 变更参数 | 信号数 | 每月信号 | 胜率 | 盈亏比 | 年化 | 夏普 |
|--------|---------|-------|---------|------|--------|------|------|
| 基准 | 无（v1原版） | 98 | 0.8✗ | 41.8% | 4.91 | 12.3% | 1.50 |
| 测试A | rs_min_percentile: 70→60 | 125 | 1.0✗ | 41.6% | 4.61 | 11.1% | 1.46 |
| 测试B | volume_mult: 1.5→1.3 | 113 | 0.9✗ | 41.6% | 4.57 | 11.5% | 1.52 |
| 测试C | min_breakout_pct: 0.5%→0.3% | 101 | 0.8✗ | 41.6% | 4.76 | 12.5% | 1.62 |
| **测试D** | **vcp_final_range_pct: 8%→12%** | **126** | **1.1✗** | **45.2%** | **4.93** | **15.1%** | **1.78** |
| 测试D+A | D + rs→60 | 157 | 1.3✗ | 43.9% | 4.69 | 12.7% | 1.68 |

**结论**：参数D（放宽VCP末端箱体）是唯一同时提升质量（胜率+45.2%，年化+15.1%）和数量的改动。
单纯调参无法达到 2/月目标，根本瓶颈是**股票池规模**（186 只）。

---

## 步骤四：UNIVERSE.md 股票池扩充（186 → ~297 只）

新增 10 个板块（板块十五至二十四），111 只新股票：

| 板块 | 内容 | 新增数量 |
|------|------|---------|
| 板块十五：SaaS/云软件 | ADBE NOW PANW CRWD DDOG NET ZS VEEV WDAY TEAM SNPS CDNS ANSS PAYC HUBS DOCU MDB ANET TTD SHOP FTNT INTU | 22 |
| 板块十六：支付/金融科技 | PYPL FIS FISV XYZ | 4 |
| 板块十七：医疗器械/制药 | REGN GILD AMGN BIIB PFE JNJ SYK BSX EW DXCM IDXX ZBH RMD STE IQV | 15 |
| 板块十八：工业/国防 | LMT NOC TDG AXON ROK ETN DE FAST ITW PH MMM CMI WM RSG | 14 |
| 板块十九：消费/零售 | SBUX CMG LOW TGT DECK YUM LULU PHM TOL NVR BKNG EXPE | 12 |
| 板块二十：银行/金融 | BLK SCHW CME ICE MSCI USB WFC C NDAQ CBOE | 10 |
| 板块二十一：大宗商品/材料 | FCX NUE CF MOS HAL DVN APA MRO X AA CLF | 11 |
| 板块二十二：亚太/新兴市场 | BABA BIDU JD SE PDD | 5 |
| 板块二十三：REIT | PLD PSA O EQR WY | 5 |
| 板块二十四：成长股+通信 | CELH BILL ZM DT RBLX SNOW ALNY CRSP MRNA BMRN VZ CMCSA CHTR | 13 |

---

## 步骤五：最终回测结果（294 只股票池 + 参数D）

### 样本内（2015-01-01 ～ 2024-12-30）

| 指标 | v1原版(186只) | v1_plus扩池+D(294只) | 及格线 |
|------|:---:|:---:|:---:|
| 每月信号 | 0.8 ✗ | 1.8 ✗ | 2~10 |
| 胜率 | 41.8% ✓ | 41.1% ✓ | >40% |
| 盈亏比 | 4.91 ✓ | 4.37 ✓ | >1.5 |
| 最大回撤 | 18.1% ✓ | 20.2% ✓ | <25% |
| 年化收益 | 12.3% ✗ | 13.0% ✗ | >15% |
| 夏普比率 | 1.50 ✓ | 1.64 ✓ | >1.0 |
| 综合 | 3/6 | 4/6 | — |

### 样本外（2024-02-12 ～ 2026-03-05）✅ 最终考试

| 指标 | v1原版(186只) | v1_plus扩池+D(294只) | 及格线 |
|------|:---:|:---:|:---:|
| 每月信号 | 2.2 ✓ | **6.8** ✓ | 2~10 |
| 胜率 | 42.2% ✓ | 44.3% ✓ | >40% |
| 盈亏比 | 4.14 ✓ | 4.21 ✓ | >1.5 |
| 最大回撤 | 17.2% ✓ | **15.2%** ✓ | <25% |
| 年化收益 | 57.4% ✓ | 54.8% ✓ | >15% |
| 夏普比率 | 2.10 ✓ | **2.24** ✓ | >1.0 |
| **综合** | **6/6 ✅** | **6/6 ✅** | — |

OOS 信号频率 2.2 → **6.8/月（提升 3 倍）**，质量维持甚至略有改善。

---

## 步骤六：新增 --save-signals 补档功能

**背景**：切换到 v1_plus 后，`output/v1_plus_wizard/signals.csv` 是空的，报告中"过去7天"显示无记录。

**修改文件**：
- `backtest/engine.py`：`BacktestEngine.__init__()` 新增 `save_signals_csv: bool = False` 和 `strategy_id: str = ""` 参数。当 `save_signals_csv=True` 时，每次产生买入信号同时调用 `_append_to_csv()` 写入 CSV。
- `main.py`：`_run_single_backtest()` 新增 `save_signals: bool = False` 参数并传给引擎；argparse 新增 `--save-signals` flag。

**补档命令**：
```bash
uv run python main.py --mode backtest --start 2026-02-19 --end 2026-03-04 --strategy v1_plus --save-signals
```

---

## 步骤七：主策略切换

`config.yaml` 修改：
```yaml
active_strategy: v1_plus  # 原来: v1
```

以后不加 `--strategy` 参数，默认使用 v1_plus。

---

## 步骤八：持仓迁移规则（新策略启动时）

切换策略时，持仓文件在各自目录中独立存储：
```
output/v1_wizard/positions.json      ← 旧策略
output/v1_plus_wizard/positions.json ← 新策略（初始为空 {}）
```

迁移命令：
```bash
cp output/v1_wizard/positions.json output/v1_plus_wizard/positions.json
```

手动修正个别持仓（如 entry_date）后重新运行 live 模式即可获得正确的卖出信号。

---

## 最终配置（v1_plus，当前生产参数）

```yaml
v1_plus:
  name: 魔法师调整参数版V1+
  sma_short: 50 / sma_mid: 150 / sma_long: 200
  trend_lookback: 20
  low_52w_mult: 1.25 / high_52w_mult: 0.75
  rs_min_percentile: 70
  pivot_lookback: 30
  min_breakout_pct: 0.005
  volume_mult: 1.5
  vcp_lookback: 50
  vcp_min_contractions: 2
  vcp_final_range_pct: 0.12    ← 关键改动（原 0.08）
  stop_loss_pct: 0.10
  trailing_stop_pct: 0.20
  time_stop_days: 20
  time_stop_min_gain: 0.03
```
