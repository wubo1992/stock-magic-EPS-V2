# EMA 10 Crossback + Watch List 设计文档

**日期**：2026-08-24
**作者**：用户需求 → Claude 辅助设计
**关联项目**：`/Users/wubo/Desktop/codes/signal_system_v_eps_v2`

---

## 1. 目标与背景

### 1.1 用户需求

用户在 v_eps_v2 选股流程基础上，新增两个紧密耦合的功能：

1. **Watch List 持久化**：把每天产生的 v_eps_v2 买入信号自动加入 watch list
2. **EMA 10 Crossback 策略**：每天扫描 watch list，筛选出符合以下模式的股票：
   - 在加入 watch list 之后，经历过 close < EMA10（跌破）
   - 之后 close > EMA10（上穿/crossback）
   - **关键约束**：crossback 当天的成交量 ≥ 过去 20 天均量的 1.5 倍

### 1.2 业务价值

v_eps_v2 的买入信号是"突破触发型"，价格已经走了一段。EMA 10 crossback 策略捕捉的是**回调后再次启动**的标的，往往比首次突破的入场价位更好、风险更低。这是经典的"pullback entry"思路。

### 1.3 适用范围

- 仅作用于 v_eps_v2 产生的信号
- 不影响其他策略（cigar_butt_v1a/b 等）

---

## 2. 设计决策（已与用户确认）

| 决策点 | 选择 | 说明 |
|---|---|---|
| 与 v_eps_v2 的集成方式 | **v_eps_v2 运行后自动追加** | main.py 中 v_eps_v2 跑完后自动跑 crossback |
| 信号输出位置 | **追加到同一 signals.csv** | 新增"信号类型"列区分 |
| Markdown 报告 | **独立报告 + v_eps_v2 报告中加区段** | 两份报告都生成 |
| Watch list 过期 | **60 天未触发就过期** | 可配置 |
| 多次 crossback | **每次都发信号** | 不做冷却期 |
| 跌破判定 | **宽松：单日 close < EMA10 就算跌破** | 不要求连续多日 |
| 信号字段 | **完整：强度 + RS + 量比** | 与 v_eps_v2 一致 |

---

## 3. 架构

```
main.py --mode live --strategy v_eps_v2
    │
    ├── [现有] v_eps_v2 策略运行
    │   ├── 数据获取（Alpaca + Yahoo fallback）
    │   ├── 计算买入信号
    │   └── 输出到 signals.csv（信号类型=v_eps_v2_buy）
    │
    ├── [新增] Watch List 更新
    │   ├── 加载现有 watchlist.json
    │   ├── 把今日所有 v_eps_v2_buy 信号加入
    │   │   ├── 若股票已存在 → 更新元数据（不重置过期时间）
    │   │   └── 新股票 → 创建条目，expires_at = today + 60d
    │   ├── 检查并移除过期条目（expires_at < today）
    │   └── 写回 watchlist.json
    │
    └── [新增] EMA 10 Crossback 策略运行
        ├── 遍历 watchlist.json 所有有效条目
        ├── 对每只股票：
        │   ├── 加载 OHLCV（in-memory market_data → cache pkl → fetch）
        │   ├── 计算 EMA(10)、20 日均量（shift 1）
        │   ├── 从 signal_date 到 today 逐日扫描：
        │   │   ├── state = "below" if first_row.close <= EMA10 else "above"
        │   │   ├── 若 close < EMA10 → state = "below"
        │   │   └── 若 state == "below" 且 close > EMA10：
        │   │       ├── 若 volume >= 1.5 * avg_vol_20：
        │   │       │   └── emit crossback_buy 信号
        │   │       │       ├── 更新 watchlist[sym].crossback_count++
        │   │       │       └── 更新 watchlist[sym].last_crossback_date
        │   │       └── state = "above"（无论是否触发）
        └── 输出到 signals.csv（信号类型=crossback_buy）
            + 独立 Markdown 报告
```

---

## 4. 数据结构

### 4.1 Watch List

**文件**：`output/v_eps_v2/watchlist.json`

```json
{
  "CRDL": {
    "symbol": "CRDL",
    "signal_date": "2026-08-18",
    "signal_price": 1.67,
    "stage2_stop": 1.5364,
    "added_at": "2026-08-18T09:37:00+00:00",
    "expires_at": "2026-10-17T09:37:00+00:00",
    "crossback_count": 0,
    "last_crossback_date": null
  }
}
```

**字段说明**：
- `signal_date`：v_eps_v2 买入信号的原始日期（YYYY-MM-DD 字符串）
- `signal_price`：信号当日的收盘价（float，单位 USD）
- `stage2_stop`：v_eps_v2 当时的参考止损价 = `signal_price × (1 - stop_loss_pct)`，其中 `stop_loss_pct=0.08`（来自 config.yaml:65）。用于历史追溯参考。CRDL 示例：1.67 × (1 - 0.08) = 1.5364
- `added_at`：ISO datetime 字符串（加入 watchlist 的实际 UTC 时间戳）
- `expires_at`：ISO datetime 字符串（超过则过期；首次添加时 = `added_at + 60 天`）
- `crossback_count`：累计触发次数。**修复 Iteration 2 #14**：过期被重新激活的 symbol，`crossback_count` **累计不重置**（保留历史累计次数，便于后续统计"该股累计 crossback 多少次"）
- `last_crossback_date`：最近一次 crossback 信号日期（YYYY-MM-DD 字符串，初始为 null）

**CRUD API**（`strategies/ema_crossback/watchlist.py`）：
```python
def load_watchlist() -> dict[str, dict]:
    """加载 watchlist.json，若不存在返回空 dict"""

def save_watchlist(watchlist: dict[str, dict]) -> None:
    """写回 watchlist.json"""

def add_signals_to_watchlist(
    watchlist: dict[str, dict],
    new_signals: list[dict],  # 来自 v_eps_v2
    expire_days: int,
    today: datetime,
) -> dict[str, dict]:
    """
    把新的 v_eps_v2 信号加入 watchlist。

    **行为规则（修订后）**：
    - 新 symbol：expires_at = today + expire_days
    - 已存在但 expires_at < today（已过期）的 symbol：**刷新 expires_at = today + expire_days**（避免过期 symbol 永远不被监控）
    - 已存在且未过期的 symbol：刷新 expires_at = today + expire_days（重新激活，让最近一次信号起算新的 60 天窗口）
    - 始终更新 signal_date / signal_price / stage2_stop 为最新一次 v_eps_v2 信号

    返回更新后的 watchlist（functional，返回新 dict，不修改入参）。
    """

def prune_expired(
    watchlist: dict[str, dict],
    today: datetime,
) -> dict[str, dict]:
    """移除 expires_at < today 的条目，返回剩余的 watchlist"""
```

### 4.2 signals.csv（扩展 schema）

**当前 schema**（6 列）：
```
日期, 股票, 信号, 强度(1-5), 触发原因, 参考止损
```

**新 schema**（7 列）：
```
日期, 股票, 信号, 信号类型, 强度(1-5), 触发原因, 参考止损
```

**`信号类型` 取值**：
- `v_eps_v2_buy`：v_eps_v2 策略买入信号（默认）
- `crossback_buy`：EMA 10 crossback 信号

**`信号` 列保持不变**：两种类型都用 "买入"，由 `信号类型` 列区分。

**`触发原因` 格式**：
- v_eps_v2：保持现有 `[EPS] ... | [Stage2] ... | [量能] ... | [RS] ...`
- crossback：`[EMA10回踩] 上穿EMA10($X.XX, +Y%) + 量能Z.Zx均量 | [RS] ...`

**示例 crossback 行**：
```
2026-08-24,CYPH,买入,crossback_buy,3,[EMA10回踩] 上穿EMA10($1.30, +5.2%) + 量能9.4x均量,$1.05
```

### 4.3 新增 Markdown 报告

**v_eps_v2 报告**（`output/v_eps_v2/2026-08-24/报告_Weinstein + EPS_2026-08-24.md`）：
在末尾新增区段：
```markdown
## EMA 10 Crossback 信号

| 日期 | 股票 | 强度 | 现价 | 参考止损 | 量比 | 上穿 EMA10 | 上穿涨幅 |
|------|------|------|------|----------|------|-----------|---------|
| 2026-08-24 | CYPH | ★★★☆☆ | $1.30 | $1.05 | 9.4x | $1.18 | +10.3% |

### Watch List 状态

- 当前监控：**45** 只
- 今日新增：**8** 只（来自 v_eps_v2_buy）
- 今日触发 crossback：**1** 只（CYPH）
- 已过期移除：**0** 只
```

**独立 Crossback 报告**（`output/v_eps_v2/2026-08-24/报告_EMA10回踩_2026-08-24.md`）：
完整版的 crossback 报告，含字段说明、看盘建议、watch list 状态汇总。

---

## 5. 模块拆分

### 5.1 新增文件

```
strategies/ema_crossback/
├── __init__.py
├── ema_crossback_strategy.py    # 策略主体（detect_crossback 函数）
└── watchlist.py                 # watch list CRUD

输出文件：
output/v_eps_v2/watchlist.json  # 数据持久化
```

### 5.2 修改文件

```
main.py                          # v_eps_v2 跑完后调用 crossback
config.yaml                      # 新增 ema_crossback 配置块
signals/generator.py             # CSV 新增"信号类型"列
signals/report.py                # v_eps_v2 报告末尾加 crossback 区段
```

### 5.3 模块依赖

```
ema_crossback_strategy.py
    ├─→ strategies/ema_crossback/watchlist.py
    ├─→ data/cache/{symbol}.pkl
    └─→ signals/generator.py (写入 signals.csv)

watchlist.py
    └─→ output/v_eps_v2/watchlist.json

main.py
    └─→ strategies/ema_crossback/ema_crossback_strategy.py
```

---

## 6. 关键算法

### 6.0 概念区分：EMA10 vs SMA150

| 维度 | EMA 10 (crossback 用) | SMA 150 (v_eps_v2 Stage 2 用) |
|---|---|---|
| 计算公式 | EWM(span=10) on close | 简单移动平均 (150 天 close 求和 ÷ 150) |
| 反应速度 | 快（指数衰减加权，近期权重大） | 慢（150 天等权） |
| 用途 | 短期趋势反转捕捉 | 中长期趋势判定 (Stage 2) |
| 来源 | 本 spec 新引入 | 既有 v_eps_v2 策略 |

**关键认知**：
- 两条均线**完全独立计算**，没有交叉过滤
- v_eps_v2 触发看的是 SMA150 上方突破（长期趋势），不要求 close > EMA10
- crossback 看的是 close vs EMA10 的相对位置（短期回踩）
- 因此可能出现：**v_eps_v2 触发当日的 close 已经在 EMA10 下方**（短期超买回落，但长期趋势仍好），这种情况在 Issue 3 修复后能正确处理

### 6.1 EMA 10 Crossback 检测

```python
def detect_crossback(
    df: pd.DataFrame,
    signal_date: pd.Timestamp,
    ema_period: int = 10,
    vol_mult: float = 1.5,
    vol_lookback: int = 20,
) -> list[dict]:
    """
    在 df[signal_date..today] 范围内检测 EMA crossback + 量能确认。

    **关键修订（修复 Issue 3）**：
    - EMA10 = EWM(span=ema_period, adjust=False) on raw `close`
      （与 v_eps_v2 使用的 SMA150 是**完全独立的两条均线**，用于不同的判定维度）
    - state 初值 = 根据 signal_date 当日 close vs EMA10 决定：
      - close > EMA10 → state = "above"
      - close <= EMA10 → state = "below"
    - state == "below" 且 close > EMA10 → crossback 候选
      - volume >= vol_mult * avg_vol_20 → emit signal
      - state = "above"（无论是否触发，都标记为回到 above）

    Returns:
        list of dict: [{date, close, ema10, volume, avg_vol_20, volume_ratio, strength}]
    """
    if df is None or len(df) < ema_period + vol_lookback:
        return []

    # 计算指标
    df = df.copy()
    df['ema10'] = df['close'].ewm(span=ema_period, adjust=False).mean()
    # shift(1)：今日成交量 ÷ 过去 20 日均量（不含今日）—— 与 v_eps_v2 量比口径一致
    df['avg_vol_20'] = df['volume'].rolling(vol_lookback).mean().shift(1)

    # 从 signal_date 起开始扫描
    df = df[df.index >= signal_date]
    if df.empty:
        return []

    signals = []
    # 修复 Issue 3：初值根据 signal_date 当日状态决定，而非硬编码 "above"
    first_row = df.iloc[0]
    state = "below" if first_row['close'] <= first_row['ema10'] else "above"

    for date, row in df.iterrows():
        if pd.isna(row['avg_vol_20']):
            continue

        if row['close'] < row['ema10']:
            state = "below"
        elif state == "below" and row['close'] > row['ema10']:
            # Crossback 触发
            vol_ratio = row['volume'] / row['avg_vol_20']
            if vol_ratio >= vol_mult:
                signals.append({
                    'date': date,
                    'close': row['close'],
                    'ema10': row['ema10'],
                    'volume': row['volume'],
                    'avg_vol_20': row['avg_vol_20'],
                    'volume_ratio': vol_ratio,
                    'strength': _crossback_strength(vol_ratio),
                })
            state = "above"

    return signals


def _crossback_strength(vol_ratio: float) -> int:
    """
    根据量能倍数计算强度等级（1-5）：
    - < 1.5x: 不会进入此函数（caller 在 detect_crossback 中已经过滤 vol_ratio >= vol_mult=1.5）
    - 1.5 - 2.0x: ★★★☆☆ (3)
    - 2.0 - 3.0x: ★★★★☆ (4)
    - >= 3.0x: ★★★★★ (5)

    Returns:
        int: 强度等级 3-5（caller 已保证 vol_ratio >= 1.5）
    """
    if vol_ratio >= 3.0:
        return 5
    if vol_ratio >= 2.0:
        return 4
    return 3  # 1.5 <= vol_ratio < 2.0
```

### 6.1.1 数据来源：优先 in-memory market_data（修复 Issue 12）

`run_ema_crossback_strategy` 函数签名：

```python
from data.fetcher import fetch  # v_eps_v2 既有依赖

def _get_df_for_symbol(
    sym: str,
    market_data: dict[str, pd.DataFrame] | None,
    cache_dir: Path,
) -> pd.DataFrame | None:
    """
    数据获取优先级：
    1. in-memory market_data[sym]（main.py 已 fetch 过，避免重复 IO）
    2. data/cache/{sym}.pkl（main.py 跑过后已写盘）
    3. fetch() 重新拉取（仅用于 watchlist 中不在今日 universe 的 symbol）

    Returns:
        DataFrame 或 None（拉取失败时返回 None，caller 应跳过）
    """
    # 优先级 1：内存
    if market_data and sym in market_data:
        return market_data[sym]
    # 优先级 2：cache pkl
    pkl_path = cache_dir / f"{sym}.pkl"
    if pkl_path.exists():
        try:
            return pd.read_pickle(pkl_path)
        except Exception:
            pass
    # 优先级 3：fetch 重新拉取（修复 Iteration 3 #11 + Iteration 4 #17）
    # 实际签名：fetch(symbols, history_days, live_mode=False, end_date=None)
    # watchlist 场景：拉取最近 60 天（涵盖 crossback 检测窗口 + EMA 计算周期）
    try:
        df_new = fetch(
            [sym],
            history_days=60,
            live_mode=False,
            end_date=today,  # today 由 caller 传入
        )
        if df_new and sym in df_new:
            return df_new[sym]
    except Exception as e:
        logger.warning(f"[crossback] {sym} fetch failed: {e}")
    return None


def run_ema_crossback_strategy(
    watchlist: dict[str, dict],
    market_data: dict[str, pd.DataFrame] | None = None,  # 优先使用
    today: datetime | None = None,
    cache_dir: Path | None = None,  # fallback：data/cache/{symbol}.pkl
) -> list[SignalEvent]:
    """
    遍历 watchlist，对每只股票检测 crossback。
    返回 SignalEvent 列表（含 signal_type="crossback_buy"）
    """
```

**关键约束**：
- watchlist 中的 symbol 可能不在今日 `universe`（v_eps_v2 触发时是 universe 成员，但后来可能被剔出）
- 这种 symbol 必须**通过优先级 3 重新拉取 OHLCV**，否则 crossback 检测不到
- 若优先级 3 也失败（退市 / Yahoo 403 / Alpaca 无覆盖），跳过该 symbol + 记录日志

### 6.2 main.py 集成点（修复 Issue 2）

**关键修订（修复 Issue 2：报告生成时机）**：

```python
# main.py 中，在 v_eps_v2 策略运行完之后，generate_daily_report 之前：

from strategies.ema_crossback.watchlist import (
    load_watchlist, save_watchlist,
    add_signals_to_watchlist, prune_expired,
)
from strategies.ema_crossback.ema_crossback_strategy import (
    run_ema_crossback_strategy,
)

# === 关键：必须在 process_signals 之前从队列取出，否则 records 丢失 ===
# process_signals 内部已经 drain 了队列；我们需要在调用 process_signals 之前快照一份
# 实现方式：在 process_signals 里增加 return_records=True 的参数，或者
# 直接在调用 process_signals 之前手动 drain。详见 §6.2.1

# 1. 运行 v_eps_v2 并 drain 信号（保留 queue 用于 crossback）
# 假设 process_signals_v2 内部已经 drain 队列并返回 records
# 但我们需要原始 SignalEvent 来构造 watchlist 条目
# —— 这里重构：把 process_signals 拆分为「drain + return records」，由调用方控制

# === 替代方案（推荐）：在 main.py 顶部新增 helper ===
v_eps_events = drain_signals(v_eps_queue)
v_eps_records = [_event_to_record(e) for e in v_eps_events]
# v_eps_events 保留 SignalEvent 完整信息（含 close、stop_loss 等）

# 2. 更新 watch list
watchlist = load_watchlist()
watchlist = add_signals_to_watchlist(
    watchlist, v_eps_events, expire_days=60, today=today
)
watchlist = prune_expired(watchlist, today)
save_watchlist(watchlist)

# 3. 运行 crossback 策略
crossback_events = []
if ema_crossback_enabled:
    crossback_events = run_ema_crossback_strategy(watchlist, market_data, today)

# 4. 把 v_eps_v2 + crossback events 合并输出到 signals.csv
combined_queue = EventQueue()
for e in v_eps_events:
    combined_queue.put(e)
for e in crossback_events:
    combined_queue.put(e)
all_records = process_signals(combined_queue, config, strategy_id)

# 5. 生成报告（包含 v_eps_v2 + crossback 两类）
generate_daily_report(all_records, config, strategy_id)
```

**核心要点**：
1. **重构方案（已确定，修复 Iteration 2 #5）**：把 `signals/generator.py:process_signals` 拆为两个函数：
   - `drain_signals(queue) -> list[SignalEvent]`：仅 drain 队列并返回原始 events（供 crossback 流程使用）
   - `process_signals(queue, config, strategy_id) -> list[dict]`：内部调用 `drain_signals` 后格式化输出（保持向后兼容）
   - main.py 显式调用 `drain_signals(queue)` 拿到 events → 用于 watchlist 更新 + crossback 输入 → 然后再调用 `process_signals` 把两类 events 一起输出
2. `generate_daily_report` 必须**在 crossback 跑完后**调用，这样报告里才有两类信号。
3. `today_buy_signals` 的来源是 **v_eps_v2 策略运行时直接产出的 SignalEvent 列表**（不是 `process_signals()` 返回的 records，因为 records 缺少 `signal_price`、`stage2_stop` 字段）。
4. **`signal_price` 来源（修复 Iteration 2 #6 + Iteration 4 #18，确定方案）**：
   - **方案：修改 `eps_v2_strategy.py:101-104`**，把 `signal_price` 显式存入 `event.data`：
     ```python
     return SignalEvent.create(
         symbol=symbol, timestamp=date, direction="buy",
         strength=signal.data["strength"], reason=reason, stop_loss=stop_loss,
         signal_price=close,  # ← 新增：close = float(df["close"].iloc[-1])
     )
     ```
   - `stage2_stop = stop_loss`（已是 `close * (1 - 0.08)`），无需新增字段
   - **不引入 watchlist.py fallback**（避免 Iteration 4 #18 的矛盾）
   - **实施前提**：必须同时修改 `eps_v2_strategy.py` 和 `events/base.py:SignalEvent.create()` 接受 `signal_price` 字段
   - **invariant**：未来 `stop_loss` 不再硬编码 `entry_price × (1 - 0.08)`（如改为 ATR-based），watchlist 数据仍然正确（因为走 signal_price 字段而非反推）

#### 6.2.1 SignalEvent schema 扩展（修复 Issue 1）

**修改位置**：`events/base.py:SignalEvent`

```python
# 在 SignalEvent.data 中新增 signal_type 字段
# SignalEvent.create() 签名增加 signal_type 参数

class SignalEvent:
    @classmethod
    def create(
        cls,
        symbol: str,
        timestamp: datetime,
        direction: str,  # "buy" | "sell"
        strength: int,   # 1-5
        reason: str,
        stop_loss: float | None = None,
        signal_type: str = "v_eps_v2_buy",  # ← 新增：默认 v_eps_v2_buy
        signal_price: float | None = None,  # ← 新增：close（用于 watchlist）
        **extra,
    ) -> "SignalEvent":
        return cls(
            symbol=symbol,
            timestamp=timestamp,
            event_type=EventType.SIGNAL,
            data={
                "direction": direction,
                "strength": strength,
                "reason": reason,
                "stop_loss": stop_loss,
                "signal_type": signal_type,
                "signal_price": signal_price,  # ← 新增
                **extra,
            },
        )
```

**SignalEvent.data['signal_type'] 取值**：
- `"v_eps_v2_buy"`：v_eps_v2 策略买入信号（默认）
- `"crossback_buy"`：EMA 10 crossback 信号

**SignalEvent.data['signal_price'] 取值**：
- `None`（默认，兼容旧代码）
- `float`：v_eps_v2 / crossback 信号当日的收盘价（USD）

#### 6.2.2 signals.csv schema 扩展 + 迁移（修复 Issue 1）

**修改位置**：`signals/generator.py:_CSV_HEADERS`

```python
# 新 schema (7 列)
_CSV_HEADERS = [
    "日期", "股票", "信号", "信号类型",  # ← 新增在第 4 位
    "强度(1-5)", "触发原因", "参考止损",
]
_OLD_SCHEMA_COLUMN_COUNT = 6  # 用于迁移检测
```

**CSV 迁移策略（修复 Iteration 2 #7）**：
1. **目标文件限定为 `output/v_eps_v2/signals.csv`**（仅 v_eps_v2 strategy_id 的 CSV）。其他策略的 CSV 不在本次迁移范围。
2. **旧 CSV（6 列）→ 新 CSV（7 列）的迁移**：在 `_append_to_csv(records, strategy_id="v_eps_v2")` 启动时检测：
   - 读首行 header
   - 若 `len(header) == _OLD_SCHEMA_COLUMN_COUNT (6)` 且 `strategy_id == "v_eps_v2"` → 自动重写整个文件：保留旧 6 列内容 + 补齐"信号类型"列（值为 `"v_eps_v2_buy"`，因为 v_eps_v2 CSV 历史记录都是 v_eps_v2 产生的）
   - **若 strategy_id 不是 "v_eps_v2"，即使列数变化也不触发迁移**（避免误标其他策略信号）
   - 备份为 `signals.csv.bak.schema_v1`
3. **实现方式**：
   - 读全文件到内存
   - 重新打开以 `'w'` 模式覆盖（先备份再覆盖）
   - 用新 schema 写 header + 旧 records + 新 records
4. **并发安全**：main.py 同一进程内串行执行，无并发问题。**不处理跨进程并发**（单实例 live mode）
5. **DictWriter 处理**：旧 schema 文件已被重写为新 schema，后续 `writerow` 不再触发 `extrasaction="ignore"` 陷阱
6. **未来字段扩展**：建议在 generator 顶部加版本号常量（如 `_CSV_SCHEMA_VERSION = 2`）便于追踪

#### 6.2.3 `_event_to_record` 扩展（修复 Issue 1）

**修改位置**：`signals/generator.py:_event_to_record`

```python
def _event_to_record(event: SignalEvent) -> dict:
    """把 SignalEvent 转换成可输出的字典。"""
    date_str = event.timestamp.strftime("%Y-%m-%d")
    direction = event.data.get("direction", "watch")
    strength = event.data.get("strength", 0)
    reason = event.data.get("reason", "")
    stop_loss = event.data.get("stop_loss")
    signal_type = event.data.get("signal_type", "v_eps_v2_buy")  # ← 新增

    stop_str = f"${stop_loss:.2f}" if stop_loss is not None else "—"
    stars = "★" * strength + "☆" * (5 - strength)

    return {
        "日期": date_str,
        "股票": event.symbol,
        "信号": _DIRECTION_CN.get(direction, direction),
        "信号类型": signal_type,  # ← 新增
        "强度(1-5)": strength,
        "强度星级": stars,
        "触发原因": reason,
        "参考止损": stop_str,
    }
```

#### 6.2.4 RS 计算集成（修复 Issue 18）

**修改位置**：`run_ema_crossback_strategy` 内部

Crossback 信号在 emit 前需要计算 RS（10D/10W），与 v_eps_v2 信号字段保持一致（用户偏好 "完整：强度 + RS + 量比"）。

**RS 函数选择（修复 Iteration 2 #2 + Iteration 3 #13）**：
- **不引入新模块**。使用 v_eps_v2 既有函数：`data.rs.compute_rs`
- **实际签名**（来自 `data/rs.py:94-100`）：
  ```python
  def compute_rs(
      ticker: str,
      as_of_date: str | datetime,
      market_data: dict[str, pd.DataFrame],
      sector_cache_path: str | Path | None = None,
      rs_daily_cache_path: str | Path | None = None,
  ) -> RSResult | None:
  ```
- **返回类型**：`RSResult` dataclass（含 `rs_spy`, `rs_10w_spy`, `rs_10d_spy`, `sector`, `sector_rank`, `sector_total`）
- **不要传 `lookback_days` kwarg**（函数不接受，内部固定 63/126/189/252 加权 + rs_10w_spy/rs_10d_spy）
- **可复用 `RSResult.format_reason_segment()`** 生成标准化的 `[RS] RS/SPY=X.XXx / RS-10W=Y.YYx / RS-10D=Z.ZZx` 字符串

```python
from data.rs import compute_rs  # v_eps_v2 既有依赖

def run_ema_crossback_strategy(...) -> list[SignalEvent]:
    for sym, entry in watchlist.items():
        df = _get_df_for_symbol(sym, market_data, cache_dir)  # 见 §6.1.1
        crossbacks = detect_crossback(df, pd.Timestamp(entry['signal_date']))
        for cb in crossbacks:
            # 计算 RS（10D/10W/SPY）—— 使用正确签名 (ticker, as_of_date, market_data)
            rs_result = compute_rs(sym, cb['date'], market_data)
            if rs_result is None:
                # 数据不足或 SPY 缺失：跳过 RS 字段，仍发信号（强度基于量能）
                rs_segment = ""
            else:
                rs_segment = " | " + rs_result.format_reason_segment()

            # 防御性检查（修复 Iteration 4 #16）：必须先于除法
            if cb['ema10'] <= 0:
                continue  # EMA10 异常数据（理论上不可能 > 0，但防御性跳过）

            crossback_pct = (cb['close'] - cb['ema10']) / cb['ema10'] * 100

            signal = SignalEvent.create(
                symbol=sym,
                timestamp=cb['date'],
                direction="buy",
                strength=cb['strength'],
                reason=(
                    f"[EMA10回踩] 上穿EMA10(${cb['ema10']:.2f}, +{crossback_pct:.1f}%) "
                    f"+ 量能{cb['volume_ratio']:.1f}x均量"
                    f"{rs_segment}"
                ),
                stop_loss=entry['stage2_stop'],
                signal_type="crossback_buy",
            )
            events.append(signal)
```

**`signal_price` 来源**：crossback 信号的 `signal_price` 在创建 SignalEvent 时取 `cb['close']`（crossback 当日收盘价）。在 §6.2 中 `add_signals_to_watchlist` 调用时不需要 crossback 的价格（crossback 是基于现有 watchlist 条目运行，不更新 watchlist），所以 crossback 不写回 watchlist.json（仅更新 `crossback_count` 和 `last_crossback_date`）。

---

## 7. 配置

`config.yaml` 新增：

```yaml
ema_crossback:
  enabled: true                    # 总开关
  ema_period: 10                   # EMA 周期
  volume_mult: 1.5                 # 量能倍数阈值
  volume_lookback: 20              # 量能回看天数
  watchlist_expire_days: 60        # watch list 过期天数
  output:
    append_to_main_csv: true       # 是否追加到 signals.csv
    generate_independent_report: true  # 是否生成独立 crossback 报告
    append_section_to_v_eps_v2_report: true  # 是否在 v_eps_v2 报告末尾加 crossback 区段
```

> **YAGNI 修复**：删除原 `break_strictness: loose`（目前仅实现 loose，无 strict 路径）。如未来需要 strict 模式（连续 N 天跌破才算），再加回。

---

## 8. 错误处理

| 场景 | 处理 |
|---|---|
| watchlist.json 不存在 | 首次运行：创建空文件，返回 `{}` |
| watchlist.json 格式损坏 | 打印警告 + 自动备份为 `.bak` + 返回 `{}` |
| 数据缺失（OHLCV < 30 天） | 跳过该股票，不发信号，记录到 debug 日志 |
| EMA10 计算周期数据不足 | 同上 |
| 信号日当天 close < EMA10 | detect_crossback 启动时先检测 signal_date 当日 state 决定初值：若 close < EMA10 → state="below"；这样后续价格从 EMA10 下方的恢复才会被算作 crossback。详见 §6.1 |
| watch list 中股票已退市（OHLCV 为空） | catch exception，跳过该股票 |
| 同一天多次触发 | 物理上不可能（每天 close 只对应一个状态） |
| watch list 中同一股票被多次 v_eps_v2 触发 | 始终刷新 expires_at = today + 60d（避免"已过期但今天又触发"的 race condition）；更新 signal_date / signal_price / stage2_stop 为最新 |
| 之前已过期的 symbol 今天又被 v_eps_v2 触发 | 重新加入，expires_at = today + 60d（刷新） |
| 信号日当天 state="below"，后续 60 天内 close 持续 < EMA10 | 不发信号（state 一直 below，没有 above → below → above 转换） |
| 信号日当天 close < EMA10 且后续 60 天只横盘不上穿 | 不发信号（同上） |

---

## 9. 测试策略

### 9.1 单元测试

- `watchlist.py`：
  - `load_watchlist()` 缺失文件 / 损坏文件
  - `add_signals_to_watchlist()`：
    - 新增 symbol
    - 已存在 symbol（未过期）刷新 metadata + expires_at
    - **已过期 symbol 被重新激活**（expires_at 刷新，crossback_count 累计不重置）
    - **signal_date / signal_price / stage2_stop 在重新触发时被更新到最新值**
  - `prune_expired()` 过期 / 未过期
- `ema_crossback_strategy.py`：
  - `detect_crossback()` 各种场景：
    - 全程在 EMA10 上方 → 无信号
    - 跌破后未回 → 无信号
    - 跌破 + 上穿但量能不足 → 无信号
    - 跌破 + 上穿 + 量能达标 → 1 个信号
    - 多次跌破 + 上穿 → 多个信号
    - **信号日 close < EMA10 但后续上穿 + 量能达标 → 1 个信号**（Issue 3 修复回归）
    - **信号日 close < EMA10 且后续 60 天只横盘不上穿 → 无信号**
    - volume=0（停牌）→ 无信号

#### 9.1.2 测试 fixture：信号日跌破边界

```python
def test_detect_crossback_signal_date_below_then_crossback():
    """Issue 3 回归测试：signal_date 当日 close < EMA10，后续上穿 + 量能达标 → 应发信号"""
    # 构造 30 天 OHLCV：信号日 close 在 EMA10 下方，后续上穿且量能 1.5x
    df = make_fixture(
        days=30,
        close_pattern="below_then_above",
        volume_at_crossback_mult=2.0,  # 触发 1.5x 阈值
    )
    signals = detect_crossback(df, signal_date=df.index[5])  # signal_date close < EMA10
    assert len(signals) == 1
    assert signals[0]['volume_ratio'] >= 1.5
```

### 9.2 集成测试

- 用历史 fixture 数据回放：
  - 给定 watch list（3 只股票）
  - 给定 30 天 OHLCV
  - 期望：检测到 N 个 crossback 信号
- 全流程集成：
  - 跑一次 main.py
  - 验证 watchlist.json 被更新
  - 验证 signals.csv 追加新行
  - 验证报告生成

---

## 10. 实施步骤（概要）

**Prerequisite（schema 扩展）**：
1. 修改 `events/base.py:SignalEvent.create()` 增加 `signal_type` 参数（默认 `"v_eps_v2_buy"`）
2. 修改 `signals/generator.py`：
   - `_CSV_HEADERS` 加 "信号类型" 列（第 4 位）
   - `_append_to_csv` 启动时检测旧 schema（6 列）→ 自动重写为 7 列 + 备份为 `.bak.schema_v1`
   - `_event_to_record` 读取 `event.data['signal_type']`

**Crossback 功能实现**：
3. 创建 `strategies/ema_crossback/__init__.py` 和目录结构
4. 实现 `strategies/ema_crossback/watchlist.py`（load / save / add_signals_to_watchlist / prune_expired）
5. 实现 `strategies/ema_crossback/ema_crossback_strategy.py`：
   - `detect_crossback()` 函数（含 Issue 3 修复：state 初值根据 signal_date 当日 close 决定）
   - `_crossback_strength()` 量能 → 强度映射
   - `run_ema_crossback_strategy()` 含 RS 计算 + market_data 优先级
6. 修改 `config.yaml` 新增 `ema_crossback` 配置块
7. 修改 `main.py`：
   - v_eps_v2 跑完后 drain SignalEvent（保留事件对象）
   - 更新 watchlist（add → prune → save）
   - 运行 crossback 策略
   - 合并 v_eps + crossback events → process_signals → generate_daily_report
8. 修改 `signals/report.py`：
   - 在 `_parse_reason` 增加 `[EMA10回踩]` token 识别
   - v_eps_v2 报告末尾加 crossback 区段
   - 生成独立 crossback 报告 `报告_EMA10回踩_<date>.md`

**测试与验证**：
9. 单元测试（§9.1 + §9.1.2 边界用例）
10. 集成测试（§9.2）
11. 跑一次真实 live test 验证

---

## 11. Review 问题回应（Q&A）

**Q1（watchlist.json 丢失恢复）**：
若 watchlist.json 丢失，重新创建空文件 + 空 watchlist。下次 v_eps_v2 触发时重新填充。**不试图从 signals.csv 历史记录回溯**（CSV 中没有完整的 `signal_price`/`stage2_stop` 字段，回溯会引入不一致）。

**Q2（backtest 模式集成）**：
本 spec **只覆盖 live 模式**。backtest 模式集成留给后续任务，原因：
- backtest 通常以"已知时间窗口 + 已知 universe"运行，watch list 的"滚动过期"语义不适用
- live 模式已覆盖主要场景，backtest 可后续单独验证算法正确性

**Q3（时区与 close 时点）**：
- live 模式运行时间：`launchd` 配置 5:30 AM 北京时间（= 21:30 UTC 前一交易日）= 美股收盘后 1 小时
- 此时 `df.index[-1]` 已是**前一个交易日**的 close
- crossback 检测应扫描 `signal_date` 到 `df.index[-1]`（即前一个交易日）
- 这是与 v_eps_v2 完全一致的口径，**不需要额外时区处理**

**Q4（`break_strictness` YAGNI）**：
已删除。详见 §7 注。

**Q5（volume_mult 可配置）**：
已经是独立配置项（`volume_mult: 1.5`），与 v_eps_v2 的 breakout `volume_mult` 互不影响。

**Q6（"跌破"是 signal_date 当天还是之后）**：
- spec §6.1 把 `signal_date` 当天**包括**在扫描范围内
- 如果 signal_date 当天 close < EMA10，**也算作"跌破"**（这是 Issue 3 修复后的正确行为）
- 这与用户原始描述"在加入 watch list 之后，经历过 close < EMA10"一致 —— "加入 watch list" 指 `signal_date`（即加入那一天）

**Q7（signal_date close < EMA10 的测试覆盖）**：
已在 §9.1 单元测试中明确加入"信号日 close < EMA10，但后续上穿"这一边界用例（详见 §9.1.2）。

**Q8（volume=0 的停牌股）**：
`volume_ratio = 0 / avg_vol_20 = 0 < 1.5`，自然不触发。不需要额外处理。

---

## 12. 未来扩展（YAGNI）

- 跌破严格度（`strict` 模式：连续 N 天 close < EMA10 才算跌破）
- 多 EMA 周期（5/10/20/50）
- 多个量能阈值（如 2x 强势 / 1.5x 普通）
- 不同的"跌破"形态定义（影线跌破 / 实体跌破）
- Watch list 手动编辑 CLI

> YAGNI：以上功能本次不实现，等用户实际需要再加。
