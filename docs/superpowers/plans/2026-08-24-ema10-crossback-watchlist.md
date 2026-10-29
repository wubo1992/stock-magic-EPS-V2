# EMA 10 Crossback + Watch List Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a watch list that auto-populates from v_eps_v2 buy signals, plus an EMA 10 crossback strategy that detects when watch-list stocks break EMA 10 from above and re-cross it with volume ≥ 1.5x 20-day average.

**Architecture:**
- Watch list persists to `output/v_eps_v2/watchlist.json` (CRUD via `strategies/ema_crossback/watchlist.py`)
- Crossback detection runs after v_eps_v2 in `main.py`, before report generation
- Both v_eps_v2 and crossback signals share `signals.csv` (7 columns: add `信号类型`)
- Two Markdown reports: v_eps_v2 report gets a crossback section appended, plus a standalone crossback report

**Tech Stack:** Python 3.13, pandas, pytest, existing v_eps_v2 strategy, existing `data.rs.compute_rs` for RS metrics

**Spec:** `docs/superpowers/specs/2026-08-24-ema10-crossback-watchlist-design.md`

**Pre-flight checks before starting:**
- Working directory: `/Users/wubo/Desktop/codes/signal_system_v_eps_v2`
- NOT a git repo (no commits needed; use timestamps in CHANGELOG instead)
- pytest installed via pyproject.toml
- No `tests/` directory exists — will create `tests/` and `tests/conftest.py`

**File Structure:**

```
NEW FILES:
  strategies/ema_crossback/__init__.py
  strategies/ema_crossback/watchlist.py          # CRUD: load/save/add/prune
  strategies/ema_crossback/ema_crossback_strategy.py  # detect + run
  tests/__init__.py
  tests/conftest.py
  tests/events/__init__.py
  tests/events/test_signal_event.py             # Chunk 1
  tests/signals/__init__.py
  tests/signals/test_generator.py               # Chunk 1
  tests/strategies/__init__.py
  tests/strategies/ema_crossback/__init__.py
  tests/strategies/ema_crossback/test_watchlist.py   # Chunk 2
  tests/strategies/ema_crossback/test_detect_crossback.py  # Chunk 3
  tests/strategies/ema_crossback/test_run_crossback.py    # Chunk 3

MODIFIED FILES:
  events/base.py                                 # SignalEvent.create: add signal_type + signal_price
  strategies/v_eps_v2/eps_v2_strategy.py:101-104 # Pass signal_price
  signals/generator.py                           # drain_signals, _CSV_HEADERS, _event_to_record, migration
  signals/report.py:343-353                      # _parse_reason: add [EMA10回踩] token
  signals/report.py                              # Generate crossback report + append section
  main.py:148-178                                # Integrate crossback between v_eps_v2 run and report
  config.yaml                                    # New ema_crossback section
```

**Implementation order (cross-chunk dependencies):**
1. Chunk 1: Schema extensions (foundation — everything depends on SignalEvent changes)
2. Chunk 2: Watchlist module (depends on SignalEvent)
3. Chunk 3: Crossback strategy (depends on watchlist)
4. Chunk 4: main.py + report.py integration (depends on chunks 2, 3)
5. Chunk 5: Config + E2E live test verification

---

## Chunk 1: Schema Extensions (Foundation)

**Goal:** Extend `SignalEvent` to carry `signal_type` and `signal_price` fields; extend `_CSV_HEADERS` and `_event_to_record` to surface `信号类型` column; add `drain_signals` helper to preserve events for crossback pipeline.

**Files modified in this chunk:**
- `events/base.py:100-122` — SignalEvent.create signature
- `strategies/v_eps_v2/eps_v2_strategy.py:101-104` — pass signal_price
- `signals/generator.py` — add `drain_signals`, extend `_CSV_HEADERS`, extend `_event_to_record`, add CSV migration logic in `_append_to_csv`

**Files created in this chunk:**
- `tests/__init__.py`
- `tests/conftest.py` — pytest fixtures for tmp paths and EventQueue
- `tests/events/__init__.py`
- `tests/events/test_signal_event.py`
- `tests/signals/__init__.py`
- `tests/signals/test_generator.py`

---

### Task 1.1: Create tests directory structure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `tests/__init__.py` (empty package marker)**

```bash
touch tests/__init__.py
```

- [ ] **Step 2: Create `tests/conftest.py` with shared fixtures**

```python
"""Shared pytest fixtures for signal_system_v_eps_v2 tests."""
import sys
from pathlib import Path

# Ensure project root is on sys.path so tests can import strategies.*, signals.*, etc.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Return a tmp path mimicking output/{strategy_id}/ structure."""
    out = tmp_path / "output" / "v_eps_v2"
    out.mkdir(parents=True)
    return out


@pytest.fixture
def make_ohlcv_df():
    """Factory: build a simple OHLCV DataFrame with given close prices."""

    def _make(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
        n = len(close_prices)
        if volumes is None:
            volumes = [1_000_000] * n
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        return pd.DataFrame(
            {
                "open": close_prices,
                "high": [p * 1.01 for p in close_prices],
                "low": [p * 0.99 for p in close_prices],
                "close": close_prices,
                "volume": volumes,
            },
            index=dates,
        )

    return _make
```

- [ ] **Step 3: Verify pytest discovers the new structure**

Run: `pytest --collect-only -q 2>&1 | head -20`
Expected: shows "no tests ran" but pytest exits cleanly (no import errors).

- [ ] **Step 4: No commit (not a git repo)**

Skip git commit. Proceed to Task 1.2.

---

### Task 1.2: Extend `SignalEvent.create()` with `signal_type` + `signal_price`

**Files:**
- Modify: `events/base.py:100-122` — SignalEvent.create signature
- Create: `tests/events/__init__.py`
- Create: `tests/events/test_signal_event.py`

- [ ] **Step 1: Write failing test for `signal_type` default**

Create `tests/events/__init__.py`:
```bash
touch tests/events/__init__.py
```

Create `tests/events/test_signal_event.py`:
```python
"""Tests for SignalEvent.create() schema extensions."""
from datetime import datetime, timezone

import pytest

from events import SignalEvent


def test_signal_event_default_signal_type_is_v_eps_v2_buy():
    """SignalEvent.create() should default signal_type to 'v_eps_v2_buy' for backward compat."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    )
    assert evt.data["signal_type"] == "v_eps_v2_buy"


def test_signal_event_accepts_explicit_signal_type():
    """SignalEvent.create() should accept signal_type='crossback_buy'."""
    evt = SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="crossback test",
        signal_type="crossback_buy",
    )
    assert evt.data["signal_type"] == "crossback_buy"


def test_signal_event_accepts_signal_price():
    """SignalEvent.create() should store signal_price in event.data."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
        signal_price=152.34,
    )
    assert evt.data["signal_price"] == 152.34


def test_signal_event_signal_price_defaults_to_none():
    """If signal_price not provided, it should be None (backward compat)."""
    evt = SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    )
    assert evt.data["signal_price"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/events/test_signal_event.py -v 2>&1 | tail -25`
Expected: 4 tests, all FAIL with errors like `'signal_type'` (KeyError) or similar.

- [ ] **Step 3: Modify `events/base.py:100-122` to extend signature**

Replace the `SignalEvent.create` classmethod (lines 100-122) with:

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
        shares: float = 0.0,
        signal_type: str = "v_eps_v2_buy",  # NEW: distinguishes v_eps_v2 vs crossback
        signal_price: float | None = None,  # NEW: close on signal day, for watchlist
    ) -> "SignalEvent":
        return cls(
            type=EventType.SIGNAL,
            symbol=symbol,
            timestamp=timestamp,
            data={
                "direction": direction,
                "strength": strength,
                "reason": reason,
                "stop_loss": stop_loss,
                "shares": shares,
                "signal_type": signal_type,
                "signal_price": signal_price,
            },
        )
```

Also update the docstring at lines 87-98 to document the two new fields:

```python
@ dataclass
class SignalEvent(Event):
    """
    SIGNAL 事件：策略产生了交易信号。

    data 字段包含：
        direction:  "buy"（买入）/ "sell"（卖出）/ "watch"（观望）
        strength:   1-5（信号强度，5最强）
        reason:     触发原因（一句话描述）
        stop_loss:  建议止损价
        shares:     持仓股数（v_eps_v2 不使用）
        signal_type: 信号类型标识，默认 "v_eps_v2_buy"，
                    crossback 信号设为 "crossback_buy"
        signal_price: 信号当日收盘价（USD），用于 watchlist 持久化
    """
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/events/test_signal_event.py -v 2>&1 | tail -15`
Expected: 4 tests, all PASS.

- [ ] **Step 5: Verify existing call sites still work**

Run: `python -c "from events import SignalEvent; from datetime import datetime, timezone; e = SignalEvent.create('AAPL', datetime(2026,8,24,tzinfo=timezone.utc), 'buy', 3, 'test', stop_loss=100.0, shares=10.0); print(e.data)"`
Expected: dict has `signal_type='v_eps_v2_buy'`, `signal_price=None`, plus all original fields (direction, strength, reason, stop_loss, shares).

---

### Task 1.3: Pass `signal_price` from `eps_v2_strategy.py`

**Files:**
- Modify: `strategies/v_eps_v2/eps_v2_strategy.py:101-104`

- [ ] **Step 1: Verify current code**

The `SignalEvent.create` call at line 101-104 looks like:
```python
return SignalEvent.create(
    symbol=symbol, timestamp=date, direction="buy",
    strength=signal.data["strength"], reason=reason, stop_loss=stop_loss,
)
```

Line 78 already computes `close = float(df["close"].iloc[-1])` and line 79 computes `stop_loss = round(close * (1 - self.stop_loss_pct), 2)`.

- [ ] **Step 2: Add `signal_price=close` to the SignalEvent.create call**

Edit `strategies/v_eps_v2/eps_v2_strategy.py:101-104` to:

```python
        return SignalEvent.create(
            symbol=symbol, timestamp=date, direction="buy",
            strength=signal.data["strength"], reason=reason, stop_loss=stop_loss,
            signal_price=close,  # NEW: close from line 78, for watchlist persistence
        )
```

- [ ] **Step 3: Smoke-test the change**

Run: `python -c "
import sys
sys.path.insert(0, '.')
from strategies.v_eps_v2 import EPSV2Strategy
# Just verify the module imports without error
print('Import OK')
"`

Expected: prints "Import OK" with no errors.

- [ ] **Step 4: Re-run event tests to confirm no regression**

Run: `pytest tests/events/test_signal_event.py -v 2>&1 | tail -10`
Expected: 4 tests still PASS.

---

### Task 1.4: Add `drain_signals` helper to `signals/generator.py`

**Files:**
- Modify: `signals/generator.py` — add `drain_signals` function

- [ ] **Step 1: Write failing test for `drain_signals`**

Add to `tests/signals/test_generator.py` (create file first with `touch tests/signals/__init__.py` and the test file):

```python
"""Tests for signals/generator.py helpers."""
from datetime import datetime, timezone

import pytest

from events import EventQueue, SignalEvent


def test_drain_signals_returns_all_signal_events():
    """drain_signals(queue) should return all SIGNAL events in the queue."""
    from signals.generator import drain_signals

    q = EventQueue()
    for i in range(3):
        q.put(SignalEvent.create(
            symbol=f"SYM{i}",
            timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
            direction="buy",
            strength=3,
            reason=f"test {i}",
        ))

    events = drain_signals(q)
    assert len(events) == 3
    assert [e.symbol for e in events] == ["SYM0", "SYM1", "SYM2"]


def test_drain_signals_empties_queue():
    """After drain_signals, the queue should be empty."""
    from signals.generator import drain_signals

    q = EventQueue()
    q.put(SignalEvent.create(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="test",
    ))

    drain_signals(q)
    assert q.empty()


def test_drain_signals_preserves_signal_type():
    """drain_signals should return events with their signal_type intact."""
    from signals.generator import drain_signals

    q = EventQueue()
    q.put(SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="crossback",
        signal_type="crossback_buy",
    ))

    events = drain_signals(q)
    assert events[0].data["signal_type"] == "crossback_buy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/signals/test_generator.py -v 2>&1 | tail -15`
Expected: 3 tests FAIL with `ImportError: cannot import name 'drain_signals'`.

- [ ] **Step 3: Add `drain_signals` to `signals/generator.py`**

Add this function below `_event_to_record` (around line 104, after the return statement):

```python
def drain_signals(queue: EventQueue) -> list[SignalEvent]:
    """
    从事件队列中取出所有 SIGNAL 事件并返回，不做任何格式化。

    与 process_signals 的区别：drain_signals 只 drain，不打印 / 不写 CSV。
    用于 main.py 把 v_eps_v2 信号先取出，传递给 watchlist 和 crossback 流程，
    之后再由 process_signals 统一输出到控制台和 CSV。

    参数：
        queue: 事件队列

    返回：
        list[SignalEvent]：所有 SIGNAL 事件（保持原始顺序）
    """
    events: list[SignalEvent] = []
    while not queue.empty():
        event = queue.get(timeout=0.05)
        if event is None:
            break
        if event.type == EventType.SIGNAL:
            events.append(event)
    return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/signals/test_generator.py -v 2>&1 | tail -10`
Expected: 3 tests PASS.

---

### Task 1.5: Extend `_CSV_HEADERS` and `_event_to_record` to surface `信号类型`

**Files:**
- Modify: `signals/generator.py:33` — `_CSV_HEADERS`
- Modify: `signals/generator.py:85-104` — `_event_to_record`

- [ ] **Step 1: Write failing tests for new schema fields**

Add to `tests/signals/test_generator.py`:

```python
def test_event_to_record_includes_signal_type():
    """_event_to_record should return a dict with '信号类型' key."""
    from signals.generator import _event_to_record

    evt = SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="crossback",
        signal_type="crossback_buy",
    )
    record = _event_to_record(evt)
    assert "信号类型" in record
    assert record["信号类型"] == "crossback_buy"


def test_event_to_record_signal_type_default():
    """_event_to_record should default to 'v_eps_v2_buy' when signal_type is missing."""
    from signals.generator import _event_to_record

    # Construct event manually without signal_type (simulates old data)
    from events import Event, EventType
    from dataclasses import fields
    evt = Event(
        type=EventType.SIGNAL,
        symbol="AAPL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        data={"direction": "buy", "strength": 3, "reason": "old"},
    )
    record = _event_to_record(evt)
    assert record["信号类型"] == "v_eps_v2_buy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/signals/test_generator.py -v 2>&1 | tail -15`
Expected: 2 new tests FAIL with `KeyError: '信号类型'` or `AssertionError: 'v_eps_v2_buy' != ...`.

- [ ] **Step 3: Modify `_CSV_HEADERS` in `signals/generator.py:33`**

Change line 33 from:
```python
_CSV_HEADERS = ["日期", "股票", "信号", "强度(1-5)", "触发原因", "参考止损"]
```

To:
```python
# CSV 列名：v_eps_v2 仅做选股，不追踪持仓 → 移除 "持仓股数" 列
# 7 列 schema: 含 "信号类型"（v_eps_v2_buy / crossback_buy）以区分两类信号
_CSV_HEADERS = [
    "日期", "股票", "信号", "信号类型",
    "强度(1-5)", "触发原因", "参考止损",
]
_OLD_SCHEMA_COLUMN_COUNT = 6  # 迁移检测：旧 CSV 是 6 列（无"信号类型"）
```

- [ ] **Step 4: Modify `_event_to_record` in `signals/generator.py:85-104`**

Replace the function body to add `信号类型` to the returned dict. The new version:

```python
def _event_to_record(event: SignalEvent) -> dict:
    """把 SignalEvent 转换成可输出的字典。"""
    date_str = event.timestamp.strftime("%Y-%m-%d")
    direction = event.data.get("direction", "watch")
    strength = event.data.get("strength", 0)
    reason = event.data.get("reason", "")
    stop_loss = event.data.get("stop_loss")
    signal_type = event.data.get("signal_type", "v_eps_v2_buy")  # NEW

    stop_str = f"${stop_loss:.2f}" if stop_loss is not None else "—"
    stars = "★" * strength + "☆" * (5 - strength)

    return {
        "日期": date_str,
        "股票": event.symbol,
        "信号": _DIRECTION_CN.get(direction, direction),
        "信号类型": signal_type,  # NEW
        "强度(1-5)": strength,
        "强度星级": stars,
        "触发原因": reason,
        "参考止损": stop_str,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/signals/test_generator.py -v 2>&1 | tail -10`
Expected: all 5 tests in test_generator.py PASS.

- [ ] **Step 6: Verify existing print_table still works (no "信号类型" leakage)**

Run: `python -c "
import sys
sys.path.insert(0, '.')
from signals.generator import _event_to_record, process_signals, _print_table
from events import EventQueue, SignalEvent
from datetime import datetime, timezone

q = EventQueue()
q.put(SignalEvent.create('AAPL', datetime(2026,8,24,tzinfo=timezone.utc), 'buy', 3, 'test', stop_loss=100.0))
records = process_signals(q, {'output': {'print_to_console': False, 'save_to_csv': False}})
print('Record keys:', list(records[0].keys()))
print('Signal type:', records[0]['信号类型'])
"`
Expected: dict has '信号类型' = 'v_eps_v2_buy'.

---

### Task 1.6: Add CSV migration logic for v_eps_v2 strategy only

**Files:**
- Modify: `signals/generator.py:142-169` — `_append_to_csv`

- [ ] **Step 1: Write failing test for CSV migration**

Add to `tests/signals/test_generator.py`:

```python
def test_csv_migration_adds_signal_type_column(tmp_path):
    """When v_eps_v2/signals.csv exists with 6-col header, migrate to 7-col."""
    from signals.generator import _append_to_csv

    # Set up: write old-schema CSV (6 columns, no "信号类型")
    csv_dir = tmp_path / "output" / "v_eps_v2"
    csv_dir.mkdir(parents=True)
    csv_file = csv_dir / "signals.csv"
    csv_file.write_text(
        "日期,股票,信号,强度(1-5),触发原因,参考止损\n"
        "2026-08-20,CF,买入,3,old signal,$100\n",
        encoding="utf-8-sig",
    )

    # Append a new record (triggers migration)
    new_record = {
        "日期": "2026-08-24",
        "股票": "CYPH",
        "信号": "买入",
        "信号类型": "v_eps_v2_buy",  # inferred for old records
        "强度(1-5)": 3,
        "强度星级": "★★★☆☆",
        "触发原因": "new signal",
        "参考止损": "$1.30",
    }
    _append_to_csv([new_record], strategy_id="v_eps_v2")

    # Verify: file has 7 columns, old record has "v_eps_v2_buy" as signal_type
    text = csv_file.read_text(encoding="utf-8-sig")
    lines = text.strip().split("\n")
    assert len(lines[0].split(",")) == 7, f"Expected 7-col header, got: {lines[0]}"
    assert "信号类型" in lines[0]
    assert "v_eps_v2_buy" in lines[1], f"Old record should be backfilled: {lines[1]}"
    assert "CYPH" in lines[2]

    # Verify backup was created
    backup = csv_dir / "signals.csv.bak.schema_v1"
    assert backup.exists()


def test_csv_migration_does_not_affect_other_strategies(tmp_path):
    """When strategy_id is NOT 'v_eps_v2', don't trigger migration."""
    from signals.generator import _append_to_csv

    csv_dir = tmp_path / "output" / "other_strategy"
    csv_dir.mkdir(parents=True)
    csv_file = csv_dir / "signals.csv"
    csv_file.write_text(
        "日期,股票,信号,强度(1-5),触发原因,参考止损\n"
        "2026-08-20,X,买入,3,test,$10\n",
        encoding="utf-8-sig",
    )

    new_record = {
        "日期": "2026-08-24",
        "股票": "Y",
        "信号": "买入",
        "信号类型": "v_eps_v2_buy",
        "强度(1-5)": 3,
        "强度星级": "★★★☆☆",
        "触发原因": "test",
        "参考止损": "$20",
    }
    _append_to_csv([new_record], strategy_id="other_strategy")

    # File should still have 6-column header (no migration triggered)
    text = csv_file.read_text(encoding="utf-8-sig")
    lines = text.strip().split("\n")
    assert len(lines[0].split(",")) == 6, f"Other strategies should not be migrated: {lines[0]}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/signals/test_generator.py::test_csv_migration_adds_signal_type_column -v 2>&1 | tail -15`
Expected: FAIL — the current `_append_to_csv` doesn't migrate.

- [ ] **Step 3: Add migration logic to `_append_to_csv`**

Replace the `_append_to_csv` function (lines 142-169 of current `signals/generator.py`) with:

```python
def _append_to_csv(records: list[dict], strategy_id: str = "v1_wizard") -> None:
    """把信号追加写入 CSV 文件（如果不存在则创建并写入表头）。

    迁移逻辑（仅 v_eps_v2）：
        - 若 signals.csv 已存在且 header 列数 == _OLD_SCHEMA_COLUMN_COUNT (6)
        - 自动重写整个文件：在第 4 位插入 "信号类型" 列，旧记录填 "v_eps_v2_buy"
        - 备份原文件为 signals.csv.bak.schema_v1
    """
    strategy_dir = OUTPUT_DIR / strategy_id
    strategy_dir.mkdir(parents=True, exist_ok=True)
    signals_csv = strategy_dir / "signals.csv"

    # v_eps_v2 migration: 6-col → 7-col header
    if (
        strategy_id == "v_eps_v2"
        and signals_csv.exists()
    ):
        with open(signals_csv, "r", encoding="utf-8-sig") as f:
            first_line = f.readline().strip()
        if len(first_line.split(",")) == _OLD_SCHEMA_COLUMN_COUNT:
            # Backup + rewrite
            backup_path = signals_csv.with_suffix(".csv.bak.schema_v1")
            signals_csv.rename(backup_path)
            # Read all old records
            import csv as _csv
            with open(backup_path, "r", encoding="utf-8-sig") as f:
                reader = _csv.DictReader(f)
                old_records = list(reader)
            # Rewrite with new schema
            with open(signals_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = _csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
                writer.writeheader()
                for old in old_records:
                    new_row = {
                        "日期": old.get("日期", ""),
                        "股票": old.get("股票", ""),
                        "信号": old.get("信号", ""),
                        "信号类型": "v_eps_v2_buy",  # backfill
                        "强度(1-5)": old.get("强度(1-5)", ""),
                        "触发原因": old.get("触发原因", ""),
                        "参考止损": old.get("参考止损", ""),
                    }
                    writer.writerow(new_row)

    # Normal append
    file_exists_after = signals_csv.exists()
    with open(signals_csv, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=_CSV_HEADERS,
            extrasaction="ignore",
        )
        if not file_exists_after:
            writer.writeheader()
        for r in records:
            writer.writerow({
                "日期": r["日期"],
                "股票": r["股票"],
                "信号": r["信号"],
                "信号类型": r.get("信号类型", "v_eps_v2_buy"),
                "强度(1-5)": r["强度(1-5)"],
                "触发原因": r["触发原因"],
                "参考止损": r["参考止损"],
            })

    print(f"[输出] 信号已保存到 {signals_csv}")
```

- [ ] **Step 4: Run all Chunk 1 tests**

Run: `pytest tests/ -v 2>&1 | tail -25`
Expected: all tests pass (event tests + generator tests including migration).

- [ ] **Step 5: Verify the migration on a real-ish scenario**

Run: `python -c "
import sys
sys.path.insert(0, '.')
import os, shutil
# Clean up any existing output dir for v_eps_v2
shutil.rmtree('output/v_eps_v2', ignore_errors=True)

# Run the function once to create the CSV fresh
from signals.generator import _append_to_csv
new_record = {
    '日期': '2026-08-24',
    '股票': 'CF',
    '信号': '买入',
    '信号类型': 'v_eps_v2_buy',
    '强度(1-5)': 3,
    '强度星级': '★★★☆☆',
    '触发原因': 'test',
    '参考止损': '\$119.24',
}
_append_to_csv([new_record], strategy_id='v_eps_v2')
print(open('output/v_eps_v2/signals.csv').read())

# Cleanup
shutil.rmtree('output/v_eps_v2', ignore_errors=True)
"`
Expected: CSV with 7-column header "日期,股票,信号,信号类型,强度(1-5),触发原因,参考止损" and one data row.

- [ ] **Step 6: Verify the full event → record → CSV pipeline**

Run: `python -c "
import sys
sys.path.insert(0, '.')
import shutil
shutil.rmtree('output/v_eps_v2', ignore_errors=True)

from signals.generator import process_signals
from events import EventQueue, SignalEvent
from datetime import datetime, timezone

q = EventQueue()
q.put(SignalEvent.create('CRDL', datetime(2026,8,24,tzinfo=timezone.utc), 'buy', 3, 'crossback', stop_loss=1.05, signal_type='crossback_buy', signal_price=1.30))
records = process_signals(q, {'output': {'print_to_console': False, 'save_to_csv': True}}, strategy_id='v_eps_v2')
print('Records:', records)
print('CSV:', open('output/v_eps_v2/signals.csv').read())
shutil.rmtree('output/v_eps_v2', ignore_errors=True)
"`
Expected: record has 信号类型=crossback_buy; CSV has 7 columns and CRDL row.

---

**End of Chunk 1.**

After Chunk 1 is fully implemented and all tests pass, dispatch plan-document-reviewer for this chunk before proceeding to Chunk 2.
---

## Chunk 2: Watchlist Module

**Goal:** Implement the watchlist CRUD operations that persist `output/v_eps_v2/watchlist.json`. The watchlist is the data backbone that feeds the crossback strategy in Chunk 3.

**Files created in this chunk:**
- `strategies/ema_crossback/__init__.py`
- `strategies/ema_crossback/watchlist.py`
- `tests/strategies/__init__.py`
- `tests/strategies/ema_crossback/__init__.py`
- `tests/strategies/ema_crossback/test_watchlist.py`

**Watchlist JSON schema** (from spec §4.1):
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

---

### Task 2.1: Create package skeleton + tests directory

**Files:**
- Create: `strategies/ema_crossback/__init__.py`
- Create: `tests/strategies/__init__.py`
- Create: `tests/strategies/ema_crossback/__init__.py`

- [ ] **Step 1: Create directories and `__init__.py` files**

```bash
mkdir -p strategies/ema_crossback
touch strategies/ema_crossback/__init__.py
mkdir -p tests/strategies/ema_crossback
touch tests/strategies/__init__.py
touch tests/strategies/ema_crossback/__init__.py
```

- [ ] **Step 2: Verify imports**

Run: `python -c "from strategies.ema_crossback import watchlist; print('OK')"`
Expected: prints "OK" (even though watchlist.py doesn't exist yet, the import will succeed if the package exists; if not, we'll see ImportError when we actually try `from .watchlist`).

If the import fails because watchlist.py doesn't exist, that's expected — we'll create it in Task 2.2.

---

### Task 2.2: Implement `load_watchlist()` and `save_watchlist()`

**Files:**
- Create: `strategies/ema_crossback/watchlist.py`
- Modify: `strategies/ema_crossback/__init__.py`

- [ ] **Step 1: Write failing tests**

Create `tests/strategies/ema_crossback/test_watchlist.py`:

```python
"""Tests for strategies/ema_crossback/watchlist.py CRUD operations."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from strategies.ema_crossback.watchlist import (
    load_watchlist,
    save_watchlist,
    add_signals_to_watchlist,
    prune_expired,
)


def test_load_watchlist_missing_file_returns_empty(tmp_path: Path):
    """When watchlist.json doesn't exist, load_watchlist should return {}."""
    watchlist = load_watchlist(path=tmp_path / "watchlist.json")
    assert watchlist == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    """save_watchlist then load_watchlist should return identical data."""
    path = tmp_path / "watchlist.json"
    data = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-18",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
            "added_at": "2026-08-18T09:37:00+00:00",
            "expires_at": "2026-10-17T09:37:00+00:00",
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }
    save_watchlist(data, path=path)
    loaded = load_watchlist(path=path)
    assert loaded == data


def test_load_watchlist_corrupt_file_returns_empty_and_backup(tmp_path: Path):
    """When watchlist.json is corrupt, backup as .bak and return empty dict."""
    path = tmp_path / "watchlist.json"
    path.write_text("{ this is not valid JSON")
    watchlist = load_watchlist(path=path)
    assert watchlist == {}
    backup = path.with_suffix(".json.bak")
    assert backup.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -15`
Expected: 3 tests FAIL with `ImportError: cannot import name 'load_watchlist' from 'strategies.ema_crossback.watchlist'`.

- [ ] **Step 3: Implement `load_watchlist` and `save_watchlist`**

Create `strategies/ema_crossback/watchlist.py`:

```python
"""
strategies/ema_crossback/watchlist.py — Watch List CRUD

持久化 v_eps_v2 买入信号到 output/v_eps_v2/watchlist.json，
供 EMA 10 crossback 策略每天扫描。

设计要点：
- functional API（不修改入参，返回新 dict）
- 缺失文件 → 返回 {}
- 损坏文件 → 备份为 .bak + 返回 {}
- 时区：所有 datetime 用 UTC ISO 字符串
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def load_watchlist(path: Path) -> dict[str, dict[str, Any]]:
    """
    加载 watchlist.json。

    Args:
        path: watchlist.json 的完整路径

    Returns:
        dict[symbol, entry]: 若文件不存在或损坏，返回 {}
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data)}")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        # 损坏：备份 + 返回空
        backup = path.with_suffix(".json.bak")
        try:
            path.rename(backup)
            print(f"[watchlist] WARNING: corrupt file backed up to {backup}: {e}")
        except Exception as e2:
            print(f"[watchlist] WARNING: corrupt file, backup failed: {e2}")
        return {}


def save_watchlist(watchlist: dict[str, dict[str, Any]], path: Path) -> None:
    """
    把 watchlist 写回磁盘。

    Args:
        watchlist: {symbol: entry}
        path: watchlist.json 的完整路径
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, indent=2, ensure_ascii=False, default=str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -10`
Expected: 3 tests PASS.

---

### Task 2.3: Implement `add_signals_to_watchlist()` with refresh semantics

**Files:**
- Modify: `strategies/ema_crossback/watchlist.py` — add function

- [ ] **Step 1: Write failing tests**

Add to `tests/strategies/ema_crossback/test_watchlist.py`:

```python
def test_add_signals_new_symbol_creates_entry(tmp_path: Path):
    """New symbols should get expires_at = today + expire_days."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    new_signals = [
        {
            "symbol": "CRDL",
            "signal_date": "2026-08-24",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
        }
    ]
    result = add_signals_to_watchlist({}, new_signals, expire_days=60, today=today, path=path)
    assert "CRDL" in result
    entry = result["CRDL"]
    assert entry["signal_price"] == 1.67
    assert entry["stage2_stop"] == 1.5364
    assert entry["crossback_count"] == 0
    assert entry["last_crossback_date"] is None
    # expires_at = today + 60 days
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry


def test_add_signals_existing_symbol_refreshes_metadata(tmp_path: Path):
    """Existing symbol: update signal_date/price/stop AND refresh expires_at."""
    today = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"

    # Pre-existing entry from 30 days ago (expires_at = today - 30, i.e., expired)
    initial = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-10",
            "signal_price": 1.50,
            "stage2_stop": 1.38,
            "added_at": "2026-08-10T09:00:00+00:00",
            "expires_at": "2026-10-09T09:00:00+00:00",  # expires in 1 day, NOT expired yet
            "crossback_count": 1,
            "last_crossback_date": "2026-08-25",
        }
    }
    save_watchlist(initial, path=path)

    # Today's new signal for the same symbol (with different price)
    new_signals = [
        {"symbol": "CRDL", "signal_date": "2026-09-10", "signal_price": 1.67, "stage2_stop": 1.5364}
    ]
    result = add_signals_to_watchlist(
        load_watchlist(path), new_signals, expire_days=60, today=today, path=path
    )
    entry = result["CRDL"]
    # Metadata updated to latest
    assert entry["signal_price"] == 1.67
    assert entry["stage2_stop"] == 1.5364
    assert entry["signal_date"] == "2026-09-10"
    # expires_at refreshed to today + 60d
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry
    # crossback_count preserved (not reset)
    assert entry["crossback_count"] == 1
    assert entry["last_crossback_date"] == "2026-08-25"


def test_add_signals_reactivates_expired_symbol(tmp_path: Path):
    """An expired symbol re-triggered today should be refreshed with new expires_at."""
    today = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    initial = {
        "X": {
            "symbol": "X",
            "signal_date": "2026-07-01",
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": "2026-07-01T09:00:00+00:00",
            "expires_at": "2026-08-30T09:00:00+00:00",  # expired (Aug 30 < Oct 1)
            "crossback_count": 2,
            "last_crossback_date": "2026-08-15",
        }
    }
    save_watchlist(initial, path=path)

    new_signals = [{"symbol": "X", "signal_date": "2026-10-01", "signal_price": 12.0, "stage2_stop": 11.04}]
    result = add_signals_to_watchlist(
        load_watchlist(path), new_signals, expire_days=60, today=today, path=path
    )
    entry = result["X"]
    assert entry["signal_price"] == 12.0
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry
    # crossback_count preserved across reactivation
    assert entry["crossback_count"] == 2


def test_add_signals_persists_to_disk(tmp_path: Path):
    """add_signals_to_watchlist should write the result to disk."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    add_signals_to_watchlist(
        {}, [{"symbol": "ABC", "signal_date": "2026-08-24", "signal_price": 50.0, "stage2_stop": 46.0}],
        expire_days=60, today=today, path=path,
    )
    # Reload from disk
    reloaded = load_watchlist(path)
    assert "ABC" in reloaded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -20`
Expected: 4 new tests FAIL with `ImportError: cannot import name 'add_signals_to_watchlist'`.

- [ ] **Step 3: Implement `add_signals_to_watchlist`**

Add to `strategies/ema_crossback/watchlist.py` (below `save_watchlist`):

```python
def add_signals_to_watchlist(
    watchlist: dict[str, dict[str, Any]],
    new_signals: list[dict[str, Any]],
    expire_days: int,
    today: datetime,
    path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """
    把新的 v_eps_v2 信号加入 watchlist。

    行为规则（spec §4.1 + Iteration 2 #5 fix）：
    - 新 symbol: expires_at = today + expire_days
    - 已存在的 symbol（无论是否过期）: 刷新 metadata (signal_date/price/stop)
      + 刷新 expires_at = today + expire_days
      + 保留 crossback_count 和 last_crossback_date（累计不重置）

    Args:
        watchlist: 当前 watchlist dict（不修改）
        new_signals: list of {symbol, signal_date, signal_price, stage2_stop}
        expire_days: watch list 过期天数（默认 60）
        today: 当前时间（用于计算 expires_at）
        path: 可选，写回磁盘的路径

    Returns:
        更新后的 watchlist（functional，返回新 dict）
    """
    result = dict(watchlist)  # 复制，不修改入参
    new_expiry = (today + timedelta(days=expire_days)).isoformat()

    for sig in new_signals:
        symbol = sig["symbol"]
        if symbol in result:
            # 保留历史统计字段
            existing = result[symbol]
            crossback_count = existing.get("crossback_count", 0)
            last_crossback_date = existing.get("last_crossback_date")
        else:
            crossback_count = 0
            last_crossback_date = None

        result[symbol] = {
            "symbol": symbol,
            "signal_date": sig["signal_date"],
            "signal_price": sig["signal_price"],
            "stage2_stop": sig["stage2_stop"],
            "added_at": today.isoformat(),
            "expires_at": new_expiry,
            "crossback_count": crossback_count,
            "last_crossback_date": last_crossback_date,
        }

    if path is not None:
        save_watchlist(result, path)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -10`
Expected: all 7 tests PASS (3 from Task 2.2 + 4 new).

---

### Task 2.4: Implement `prune_expired()`

**Files:**
- Modify: `strategies/ema_crossback/watchlist.py` — add function

- [ ] **Step 1: Write failing tests**

Add to `tests/strategies/ema_crossback/test_watchlist.py`:

```python
def test_prune_expired_removes_expired_entries(tmp_path: Path):
    """Entries with expires_at < today should be removed."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {
        "EXPIRED": {
            "symbol": "EXPIRED",
            "expires_at": "2026-08-20T09:00:00+00:00",  # before today
            "signal_date": "2026-06-20",
        },
        "FRESH": {
            "symbol": "FRESH",
            "expires_at": "2026-10-20T09:00:00+00:00",  # after today
            "signal_date": "2026-08-20",
        },
    }
    pruned = prune_expired(watchlist, today=today)
    assert "EXPIRED" not in pruned
    assert "FRESH" in pruned


def test_prune_expired_keeps_entries_exactly_at_today(tmp_path: Path):
    """Entries with expires_at == today should be KEPT (boundary)."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {
        "BOUNDARY": {
            "symbol": "BOUNDARY",
            "expires_at": "2026-08-24T09:00:00+00:00",  # == today
            "signal_date": "2026-08-20",
        },
    }
    pruned = prune_expired(watchlist, today=today)
    assert "BOUNDARY" in pruned


def test_prune_expired_returns_new_dict(tmp_path: Path):
    """prune_expired should not mutate the input watchlist."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {"X": {"symbol": "X", "expires_at": "2026-01-01T00:00:00+00:00"}}
    original_copy = dict(watchlist)
    prune_expired(watchlist, today=today)
    assert watchlist == original_copy
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -15`
Expected: 3 new tests FAIL with `ImportError: cannot import name 'prune_expired'`.

- [ ] **Step 3: Implement `prune_expired`**

Add to `strategies/ema_crossback/watchlist.py` (below `add_signals_to_watchlist`):

```python
def prune_expired(
    watchlist: dict[str, dict[str, Any]],
    today: datetime,
) -> dict[str, dict[str, Any]]:
    """
    移除 expires_at < today 的条目（boundary case: expires_at == today 保留）。

    Args:
        watchlist: 当前 watchlist
        today: 当前时间

    Returns:
        修剪后的 watchlist（新 dict，不修改入参）
    """
    today_iso = today.isoformat()
    return {
        symbol: entry
        for symbol, entry in watchlist.items()
        if entry.get("expires_at", "") >= today_iso
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/ema_crossback/test_watchlist.py -v 2>&1 | tail -10`
Expected: all 10 tests PASS.

- [ ] **Step 5: Update `strategies/ema_crossback/__init__.py` to export watchlist functions**

Edit `strategies/ema_crossback/__init__.py`:

```python
"""EMA 10 crossback strategy + watch list."""
from strategies.ema_crossback.watchlist import (
    load_watchlist,
    save_watchlist,
    add_signals_to_watchlist,
    prune_expired,
)

__all__ = [
    "load_watchlist",
    "save_watchlist",
    "add_signals_to_watchlist",
    "prune_expired",
]
```

- [ ] **Step 6: Run all Chunk 1 + Chunk 2 tests**

Run: `pytest tests/ -v 2>&1 | tail -30`
Expected: all tests pass (event + generator + watchlist = 19+ tests).

---

**End of Chunk 2.**

After Chunk 2 is fully implemented, dispatch plan-document-reviewer for this chunk before proceeding to Chunk 3.

---

## Chunk 3: Crossback Strategy

**Goal:** Implement the EMA 10 crossback detection algorithm and the `run_ema_crossback_strategy` orchestrator that produces `SignalEvent`s for the watchlist pipeline.

**Files created in this chunk:**
- `strategies/ema_crossback/ema_crossback_strategy.py`
- `tests/strategies/ema_crossback/test_detect_crossback.py`
- `tests/strategies/ema_crossback/test_run_crossback.py`

**Algorithm reference:** Spec §6.0 + §6.1 + §6.1.1 + §6.2.4

---

### Task 3.1: Implement `detect_crossback()` with state initialization fix

**Files:**
- Create: `strategies/ema_crossback/ema_crossback_strategy.py`

- [ ] **Step 1: Write failing tests**

Create `tests/strategies/ema_crossback/test_detect_crossback.py`:

```python
"""Tests for detect_crossback() — EMA 10 crossback detection with volume confirmation."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    detect_crossback,
    _crossback_strength,
)


def _make_df(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Build a simple OHLCV DataFrame from close prices."""
    n = len(close_prices)
    if volumes is None:
        volumes = [1_000_000] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": close_prices,
            "high": [p * 1.01 for p in close_prices],
            "low": [p * 0.99 for p in close_prices],
            "close": close_prices,
            "volume": volumes,
        },
        index=dates,
    )


def test_detect_crossback_insufficient_data_returns_empty():
    """When df has fewer rows than ema_period + vol_lookback, return []."""
    short_close = [10.0] * 20  # only 20 rows, need 30
    df = _make_df(short_close)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_all_above_returns_empty():
    """When price stays above EMA10 the whole time, no signal."""
    # Monotonically rising prices from 10 to 20 over 60 days
    closes = list(np.linspace(10, 20, 60))
    df = _make_df(closes)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_no_recovery_returns_empty():
    """When price breaks EMA10 down and never recovers, no signal."""
    # First 40 days rising, then drops below and stays
    closes = list(np.linspace(10, 20, 40)) + [10] * 20  # last 20 days at 10
    df = _make_df(closes)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_then_recovery_no_volume_no_signal():
    """When price recovers but volume < 1.5x avg, no signal."""
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + list(np.linspace(10.5, 12, 15))
    # All volumes uniform → ratio = 1.0
    df = _make_df(closes, volumes=[1_000_000] * 60)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


def test_detect_crossback_breakdown_recovery_with_volume_emits_signal():
    """Full case: breakdown + recovery + 2x volume → 1 signal."""
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + [12.0]  # 1-day recovery
    # Recovery day volume = 2x baseline
    vols = [1_000_000] * 45 + [2_000_000] * 15
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert len(signals) == 1
    assert signals[0]["close"] == 12.0
    assert signals[0]["volume_ratio"] >= 1.5


def test_detect_crossback_multiple_breakdowns_emit_multiple_signals():
    """Multiple breakdown-recovery cycles → multiple signals (per spec §2)."""
    # Pattern: above → below → above (recovery) → below → above (recovery)
    closes = (
        list(np.linspace(10, 20, 10))    # 10 days above
        + [10] * 3                        # 3 days below
        + [12]                           # recovery (volume spike)
        + list(np.linspace(12, 22, 10))   # 10 days above
        + [11] * 3                        # 3 days below
        + [13]                           # recovery (volume spike)
        + [13] * 10                       # 10 days above
    )
    n = len(closes)
    assert n >= 60, f"Need at least 60 days for EMA + volume lookback, got {n}"
    vols = [1_000_000] * (n - 10) + [2_000_000] * 10
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert len(signals) == 2


def test_detect_crossback_signal_date_close_below_ema10_still_works():
    """Issue 3 fix: signal_date close < EMA10 → state starts as 'below'."""
    # First 30 days: close rising from 10 to 20 (will compute EMA10)
    # Day 30 (signal_date): close DROPS below EMA10
    # Days 31-35: stays below
    # Day 36: recovers above EMA10 with volume spike
    closes = list(np.linspace(10, 20, 30)) + [12] * 6 + [25] + [25] * 5
    vols = [1_000_000] * 36 + [2_000_000] * 6
    df = _make_df(closes, volumes=vols)
    signal_date = df.index[30]  # close=12, below EMA10
    signals = detect_crossback(df, signal_date=signal_date)
    # Should emit signal on day 36 (close=25 crosses above EMA10)
    assert len(signals) >= 1
    assert signals[0]["date"] == df.index[36]


def test_detect_crossback_volume_zero_no_signal():
    """When volume=0 (halted), no signal even if price pattern is right."""
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + [12] * 15
    vols = [1_000_000] * 45 + [0] * 15  # halted
    df = _make_df(closes, volumes=vols)
    signals = detect_crossback(df, signal_date=df.index[0])
    assert signals == []


# Strength helper
def test_crossback_strength_thresholds():
    """_crossback_strength maps volume_ratio to 1-5 rating."""
    assert _crossback_strength(1.5) == 3
    assert _crossback_strength(1.9) == 3
    assert _crossback_strength(2.0) == 4
    assert _crossback_strength(2.9) == 4
    assert _crossback_strength(3.0) == 5
    assert _crossback_strength(10.0) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_detect_crossback.py -v 2>&1 | tail -25`
Expected: 8 tests FAIL with `ImportError: cannot import name 'detect_crossback'`.

- [ ] **Step 3: Implement `detect_crossback` and `_crossback_strength`**

Create `strategies/ema_crossback/ema_crossback_strategy.py`:

```python
"""
strategies/ema_crossback/ema_crossback_strategy.py — EMA 10 crossback 检测

核心算法（spec §6.1）：
- 在 df[signal_date..today] 范围内扫描
- state 初值根据 signal_date 当日 close vs EMA10 决定（spec Iteration 1 #3 修复）
- close < EMA10 → state = "below"
- state == "below" 且 close > EMA10 → crossback 候选
- volume >= vol_mult × avg_vol_20 → emit signal
- state = "above"（无论是否触发）

数据来源优先级（spec §6.1.1）：
1. in-memory market_data[sym]
2. data/cache/{sym}.pkl
3. fetch() 重新拉取
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data.fetcher import fetch
from data.rs import compute_rs

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache")


def detect_crossback(
    df: pd.DataFrame,
    signal_date: pd.Timestamp,
    ema_period: int = 10,
    vol_mult: float = 1.5,
    vol_lookback: int = 20,
) -> list[dict[str, Any]]:
    """
    在 df[signal_date..today] 范围内检测 EMA crossback + 量能确认。

    Returns:
        list of dict: [{date, close, ema10, volume, avg_vol_20, volume_ratio, strength}]
        失败/数据不足时返回 []
    """
    if df is None or len(df) < ema_period + vol_lookback:
        return []

    df = df.copy()
    df["ema10"] = df["close"].ewm(span=ema_period, adjust=False).mean()
    # shift(1)：今日成交量 ÷ 过去 20 日均量（不含今日）
    df["avg_vol_20"] = df["volume"].rolling(vol_lookback).mean().shift(1)

    df = df[df.index >= signal_date]
    if df.empty:
        return []

    # Issue 3 修复：初值根据 signal_date 当日状态决定
    first_row = df.iloc[0]
    state = "below" if first_row["close"] <= first_row["ema10"] else "above"

    signals = []
    for date, row in df.iterrows():
        if pd.isna(row["avg_vol_20"]):
            continue

        if row["close"] < row["ema10"]:
            state = "below"
        elif state == "below" and row["close"] > row["ema10"]:
            vol_ratio = row["volume"] / row["avg_vol_20"]
            if vol_ratio >= vol_mult:
                signals.append({
                    "date": date,
                    "close": row["close"],
                    "ema10": row["ema10"],
                    "volume": row["volume"],
                    "avg_vol_20": row["avg_vol_20"],
                    "volume_ratio": vol_ratio,
                    "strength": _crossback_strength(vol_ratio),
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/ema_crossback/test_detect_crossback.py -v 2>&1 | tail -15`
Expected: 9 tests PASS.

---

### Task 3.2: Implement `_get_df_for_symbol()` data source priority

**Files:**
- Modify: `strategies/ema_crossback/ema_crossback_strategy.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/strategies/ema_crossback/test_run_crossback.py` (create file first):

```python
"""Tests for run_ema_crossback_strategy orchestrator and helpers."""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategies.ema_crossback.ema_crossback_strategy import (
    _get_df_for_symbol,
    run_ema_crossback_strategy,
)


def _make_df(close_prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(close_prices)
    if volumes is None:
        volumes = [1_000_000] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": close_prices,
            "high": [p * 1.01 for p in close_prices],
            "low": [p * 0.99 for p in close_prices],
            "close": close_prices,
            "volume": volumes,
        },
        index=dates,
    )


def test_get_df_for_symbol_prefers_in_memory(tmp_path: Path):
    """_get_df_for_symbol should return in-memory market_data first."""
    df_in_mem = _make_df([10.0] * 60)
    df_disk = _make_df([99.0] * 60)
    df_disk.to_pickle(tmp_path / "SYMBOL.pkl")

    result = _get_df_for_symbol("SYMBOL", market_data={"SYMBOL": df_in_mem}, cache_dir=tmp_path)
    pd.testing.assert_frame_equal(result, df_in_mem)


def test_get_df_for_symbol_falls_back_to_cache(tmp_path: Path):
    """When symbol not in memory, when symbol not in market_data, read from cache pkl."""
    df_disk = _make_df([10.0] * 60)
    df_disk.to_pickle(tmp_path / "SYM.pkl")

    result = _get_df_for_symbol("SYM", market_data={}, cache_dir=tmp_path)
    pd.testing.assert_frame_equal(result, df_disk)


def test_get_df_for_symbol_returns_none_when_missing(tmp_path: Path):
    """When symbol not in memory and not in cache, return None."""
    result = _get_df_for_symbol("MISSING", market_data={}, cache_dir=tmp_path)
    # Without fetch() fallback in tests, this should return None
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_run_crossback.py -v 2>&1 | tail -15`
Expected: 3 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement `_get_df_for_symbol`**

Add to `strategies/ema_crossback/ema_crossback_strategy.py` (below `_crossback_strength`):

```python
def _get_df_for_symbol(
    sym: str,
    market_data: dict[str, pd.DataFrame] | None,
    cache_dir: Path,
    today: datetime | None = None,
) -> pd.DataFrame | None:
    """
    三级数据查找：in-memory → cache pkl → fetch fallback（修复 spec §6.1.1）。

    Returns:
        DataFrame 或 None（拉取失败时返回 None）
    """
    # 优先级 1：内存
    if market_data and sym in market_data:
        return market_data[sym]

    # 优先级 2：cache pkl
    pkl_path = cache_dir / f"{sym}.pkl"
    if pkl_path.exists():
        try:
            return pd.read_pickle(pkl_path)
        except Exception as e:
            logger.warning(f"[crossback] {sym} cache read failed: {e}")

    # 优先级 3：fetch 重新拉取（spec §6.1.1 + Iteration 4 #17）
    try:
        df_new = fetch(
            [sym],
            history_days=60,
            live_mode=False,
            end_date=today,
        )
        if df_new and sym in df_new:
            return df_new[sym]
    except Exception as e:
        logger.warning(f"[crossback] {sym} fetch failed: {e}")

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/strategies/ema_crossback/test_run_crossback.py -v 2>&1 | tail -10`
Expected: 3 tests PASS.

---

### Task 3.3: Implement `run_ema_crossback_strategy()` orchestrator

**Files:**
- Modify: `strategies/ema_crossback/ema_crossback_strategy.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/strategies/ema_crossback/test_run_crossback.py`:

```python
def test_run_returns_signal_event_for_crossback(tmp_path: Path):
    """run_ema_crossback_strategy should emit SignalEvent when crossback detected."""
    # Build a watchlist with one symbol, signal_date from 30 days ago
    signal_date_str = "2026-07-15"
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": signal_date_str,
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": today.isoformat(),
            "expires_at": today.isoformat(),
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }

    # Build OHLCV: 60 days, pattern with crossback on last day
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + [12.0] + [12.0] * 9
    vols = [1_000_000] * 45 + [2_000_000] * 15
    df = _make_df(closes, volumes=vols)
    # Make sure signal_date aligns with the df index
    df.index = pd.date_range(start="2026-06-01", periods=len(closes), freq="B")
    signal_ts = pd.Timestamp(signal_date_str)

    market_data = {"CRDL": df, "SPY": _make_df([400.0] * 60)}

    # Mock data.rs.compute_rs to return None (skip RS for tests)
    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        events = run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    assert len(events) == 1
    evt = events[0]
    assert evt.symbol == "CRDL"
    assert evt.data["signal_type"] == "crossback_buy"
    assert evt.data["strength"] >= 3
    assert evt.data["stop_loss"] == 9.2


def test_run_skips_watchlist_with_no_data(tmp_path: Path):
    """When watchlist symbol has no data anywhere, skip silently."""
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "MISSING": {
            "symbol": "MISSING",
            "signal_date": "2026-07-15",
            "stage2_stop": 9.2,
        }
    }
    market_data = {"SPY": _make_df([400.0] * 60)}

    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        events = run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    assert events == []


def test_run_updates_watchlist_crossback_count(tmp_path: Path):
    """After detecting, run_ema_crossback_strategy should return updated watchlist (or update in place)."""
    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-07-15",
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": today.isoformat(),
            "expires_at": today.isoformat(),
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }
    closes = list(np.linspace(10, 20, 40)) + [10] * 5 + [12.0] + [12.0] * 9
    vols = [1_000_000] * 45 + [2_000_000] * 15
    df = _make_df(closes, volumes=vols)
    df.index = pd.date_range(start="2026-06-01", periods=len(closes), freq="B")
    market_data = {"CRDL": df, "SPY": _make_df([400.0] * 60)}

    from unittest.mock import patch
    with patch("strategies.ema_crossback.ema_crossback_strategy.compute_rs", return_value=None):
        run_ema_crossback_strategy(
            watchlist, market_data=market_data, today=today, cache_dir=tmp_path,
        )
    # Watchlist should be updated in-place (crossback_count++)
    assert watchlist["CRDL"]["crossback_count"] == 1
    assert watchlist["CRDL"]["last_crossback_date"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/strategies/ema_crossback/test_run_crossback.py -v 2>&1 | tail -15`
Expected: 3 new tests FAIL with `ImportError: cannot import name 'run_ema_crossback_strategy'`.

- [ ] **Step 3: Implement `run_ema_crossback_strategy`**

Add to `strategies/ema_crossback/ema_crossback_strategy.py` (below `_get_df_for_symbol`):

```python
def run_ema_crossback_strategy(
    watchlist: dict[str, dict[str, Any]],
    market_data: dict[str, pd.DataFrame] | None = None,
    today: datetime | None = None,
    cache_dir: Path | None = None,
    ema_period: int = 10,
    vol_mult: float = 1.5,
    vol_lookback: int = 20,
) -> list:
    """
    遍历 watchlist，对每只股票检测 crossback（spec §6.1.1 + §6.2.4）。

    Returns:
        list[SignalEvent]: 含 signal_type="crossback_buy"
    Side effect:
        更新入参 watchlist 中触发的 symbol 的 crossback_count / last_crossback_date
    """
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    if today is None:
        today = datetime.now(tz=timezone.utc)

    from events import SignalEvent

    events = []
    for sym, entry in watchlist.items():
        signal_date = pd.Timestamp(entry["signal_date"])
        df = _get_df_for_symbol(sym, market_data, cache_dir, today=today)
        if df is None:
            logger.warning(f"[crossback] {sym} no data, skipping")
            continue

        crossbacks = detect_crossback(
            df, signal_date, ema_period=ema_period,
            vol_mult=vol_mult, vol_lookback=vol_lookback,
        )
        for cb in crossbacks:
            # 计算 RS（10D/10W/SPY）—— spec §6.2.4，使用正确签名
            rs_result = compute_rs(sym, cb["date"], market_data)
            if rs_result is None:
                rs_segment = ""
            else:
                rs_segment = " | " + rs_result.format_reason_segment()

            # 防御性检查（spec Iteration 4 #16）：必须先于除法
            if cb["ema10"] <= 0:
                continue

            crossback_pct = (cb["close"] - cb["ema10"]) / cb["ema10"] * 100

            signal = SignalEvent.create(
                symbol=sym,
                timestamp=cb["date"],
                direction="buy",
                strength=cb["strength"],
                reason=(
                    f"[EMA10回踩] 上穿EMA10(${cb['ema10']:.2f}, +{crossback_pct:.1f}%) "
                    f"+ 量能{cb['volume_ratio']:.1f}x均量"
                    f"{rs_segment}"
                ),
                stop_loss=entry["stage2_stop"],
                signal_type="crossback_buy",
            )
            events.append(signal)

            # 更新 watchlist 统计字段（spec §6.1.1: crossback_count++ / last_crossback_date）
            entry["crossback_count"] = entry.get("crossback_count", 0) + 1
            entry["last_crossback_date"] = cb["date"].strftime("%Y-%m-%d")

    return events
```

- [ ] **Step 4: Update `strategies/ema_crossback/__init__.py` exports**

Edit `strategies/ema_crossback/__init__.py`:

```python
"""EMA 10 crossback strategy + watch list."""
from strategies.ema_crossback.watchlist import (
    load_watchlist,
    save_watchlist,
    add_signals_to_watchlist,
    prune_expired,
)
from strategies.ema_crossback.ema_crossback_strategy import (
    detect_crossback,
    run_ema_crossback_strategy,
)

__all__ = [
    "load_watchlist",
    "save_watchlist",
    "add_signals_to_watchlist",
    "prune_expired",
    "detect_crossback",
    "run_ema_crossback_strategy",
]
```

- [ ] **Step 5: Run all Chunk 3 tests**

Run: `pytest tests/strategies/ema_crossback/ -v 2>&1 | tail -25`
Expected: all detect_crossback + run_ema_crossback_strategy tests pass.

- [ ] **Step 6: Run all tests in repo**

Run: `pytest tests/ -v 2>&1 | tail -30`
Expected: ALL tests across chunks 1, 2, 3 pass (~30+ tests).

---

**End of Chunk 3.**

After Chunk 3 is fully implemented, dispatch plan-document-reviewer for this chunk before proceeding to Chunk 4.

---

## Chunk 4: Integration (main.py + report.py)

**Goal:** Wire crossback into the live-mode pipeline in main.py and update the Markdown report to include a crossback section + generate a standalone crossback report.

**Files modified in this chunk:**
- `main.py:148-178` — integrate crossback between v_eps_v2 run and report
- `signals/report.py:343-353` — `_parse_reason` add `[EMA10回踩]` token
- `signals/report.py` — append crossback section to v_eps_v2 report
- `signals/report.py` — add `generate_crossback_report()` function

**Reference:** Spec §6.2 (main.py integration), §4.3 (Markdown report)

**Prerequisites (must be complete before starting this chunk):**
- ✅ Chunk 1.4 — `drain_signals` helper exists in `signals/generator.py`
- ✅ Chunk 1.5 — `_CSV_HEADERS` is 7 cols and `_event_to_record` includes `信号类型` key
- ✅ Chunk 1.6 — CSV migration logic handles 6→7 col transition
- ✅ Chunk 2 — `strategies/ema_crossback/watchlist.py` (load/save/add/prune) implemented
- ✅ Chunk 3.3 — `run_ema_crossback_strategy` returns `list[SignalEvent]` with `signal_type="crossback_buy"`
- ✅ Chunk 3.4 — `strategies/ema_crossback/__init__.py` exports `load_watchlist`, `add_signals_to_watchlist`, `prune_expired`, `run_ema_crossback_strategy`
- ✅ `tests/` directory and `tests/signals/__init__.py` exist (Chunk 1.1)

---

### Task 4.1: Modify `main.py` to integrate crossback pipeline

**Files:**
- Modify: `main.py:148-178` — wire in crossback after v_eps_v2 run, before report

- [ ] **Step 1: Verify current main.py structure**

Re-read `main.py:148-178` to confirm the structure:
- Line 148: `queue = EventQueue()`
- Line 149: `today = datetime.now(tz=timezone.utc)`
- Line 150: `strategy.run_date(today, queue)`
- Lines 167-177: process_signals, generate_daily_report

- [ ] **Step 2: Add crossback imports + logger at top of main.py**

Edit `main.py` to add new imports (find the existing imports section and add these):

```python
# Crossback feature imports
import logging  # NEW: needed for logger.warning() in crossback flow
from pathlib import Path  # NEW: Path("data/cache") for crossback
from signals.generator import OUTPUT_DIR, _event_to_record, drain_signals, process_signals
from signals.report import generate_crossback_report
from strategies.ema_crossback import (
    load_watchlist,
    add_signals_to_watchlist,
    prune_expired,
    run_ema_crossback_strategy,
)
from strategies.ema_crossback.watchlist import save_watchlist

logger = logging.getLogger(__name__)  # NEW: define module-level logger
```

- [ ] **Step 3: Replace lines 167-177 with crossback-integrated flow**

Replace this block in `main.py`:

```python
    # 输出信号
    records = process_signals(queue, config, strategy_id=strategy_id)
    if not records:
        print("\n今日无有效信号。")
        print("（SEPA 条件严格，无信号是正常的。有信号时才需要关注。）")

    # 生成每日 Markdown 报告（无论有无信号都生成）
    report_path = generate_daily_report(
        records,
        strategy_id=strategy_id,
        strategy_name=strategy_name,
    )
    print(f"\n[报告] 今日报告：{report_path}")
```

With:

```python
    # === Crossback 集成（spec §6.2） ===
    # 1. Drain v_eps_v2 信号（保留 events 供 watchlist 更新 + crossback 输入）
    v_eps_events = drain_signals(queue)

    # 2. 更新 watch list（spec §4.1）
    watchlist_path = OUTPUT_DIR / strategy_id / "watchlist.json"
    watchlist = load_watchlist(watchlist_path)

    # 把 v_eps_v2 信号转为 watchlist 条目（spec §6.2 core要点 #4）
    new_entries = []
    for evt in v_eps_events:
        if evt.data.get("signal_type") == "v_eps_v2_buy":
            signal_price = evt.data.get("signal_price")
            if signal_price is None:
                logger.warning(f"[main] {evt.symbol} missing signal_price, skipping watchlist")
                continue
            stage2_stop = evt.data.get("stop_loss")  # already = signal_price * 0.92
            new_entries.append({
                "symbol": evt.symbol,
                "signal_date": evt.timestamp.strftime("%Y-%m-%d"),
                "signal_price": signal_price,
                "stage2_stop": stage2_stop,
            })

    watchlist = add_signals_to_watchlist(
        watchlist, new_entries, expire_days=60, today=today, path=watchlist_path
    )
    watchlist = prune_expired(watchlist, today)

    # 3. 运行 crossback 策略
    crossback_events = []
    if config.get("ema_crossback", {}).get("enabled", True):
        crossback_events = run_ema_crossback_strategy(
            watchlist,
            market_data=market_data,
            today=today,
            cache_dir=Path("data/cache"),
        )

    # CRITICAL：必须无条件持久化 watchlist —— run_ema_crossback_strategy
    # 会在内存中直接修改入参 watchlist 的 crossback_count / last_crossback_date
    # 即使今天没有 crossback 信号，这些字段也可能被其他路径修改
    save_watchlist(watchlist, watchlist_path)

    # 4. 合并 v_eps_v2 + crossback events，统一输出
    combined_queue = EventQueue()
    for e in v_eps_events:
        combined_queue.put(e)
    for e in crossback_events:
        combined_queue.put(e)
    records = process_signals(combined_queue, config, strategy_id=strategy_id)
    if not records:
        print("\n今日无有效信号。")
        print("（SEPA 条件严格，无信号是正常的。有信号时才需要关注。）")

    # 5. 生成报告（含 v_eps_v2 + crossback 两类）
    report_path = generate_daily_report(
        records,
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        crossback_records=[r for r in records if r.get("信号类型") == "crossback_buy"],
        watchlist_status={
            "total": len(watchlist),
            "added_today": len(new_entries),
            "triggered_today": len(crossback_events),
        },
    )
    print(f"\n[报告] 今日报告：{report_path}")

    # 6. 独立 crossback 报告（spec §4.3）
    if config.get("ema_crossback", {}).get("output", {}).get("generate_independent_report", True):
        cb_report_path = generate_crossback_report(
            records=[_event_to_record(e) for e in crossback_events],
            watchlist=watchlist,
            strategy_id=strategy_id,
            today=today,
        )
        if cb_report_path:
            print(f"[报告] Crossback 报告：{cb_report_path}")
```

- [ ] **Step 4: Verify main.py syntax**

Run: `python -c "import ast; ast.parse(open('main.py').read()); print('Syntax OK')"`
Expected: prints "Syntax OK".

- [ ] **Step 5: Smoke-test main.py imports**

Run: `python -c "
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('main', 'main.py')
module = importlib.util.module_from_spec(spec)
# Just check it parses and imports
spec.loader.exec_module(module)
print('main.py imports OK')
"`
Expected: prints "main.py imports OK".

---

### Task 4.2: Update `_parse_reason` in `signals/report.py` to recognize `[EMA10回踩]`

**Files:**
- Modify: `signals/report.py:343-353` — `_parse_reason` function

- [ ] **Step 1: Write failing test**

Create `tests/signals/test_report.py` (assumes `tests/signals/__init__.py` already exists from Chunk 1.1):

```python
"""Tests for signals/report.py reason parser and crossback sections."""
from signals.report import _parse_reason


def test_parse_reason_extracts_ema10_crossback_token():
    """_parse_reason should recognize [EMA10回踩] token."""
    reason = (
        "[EMA10回踩] 上穿EMA10($1.18, +10.3%) + 量能2.5x均量 "
        "| [RS] RS/SPY=1.05x / RS-10W=1.10x / RS-10D=1.15x"
    )
    sections = _parse_reason(reason)
    assert "[EMA10回踩]" in sections
    assert "上穿EMA10($1.18" in sections["[EMA10回踩]"]


def test_parse_reason_still_extracts_other_tokens():
    """Existing tokens ([趋势], [RS], etc.) should still work."""
    reason = "[EPS] 最新季度EPS$2.59 | [Stage2] SMA150上升30天 | [量能] 1.5x均量 | [RS] RS/SPY=1.05x"
    sections = _parse_reason(reason)
    assert "[EPS]" in sections
    assert "[Stage2]" in sections
    assert "[量能]" in sections
    assert "[RS]" in sections
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/signals/test_report.py -v 2>&1 | tail -15`
Expected: 2 tests FAIL — `test_parse_reason_extracts_ema10_crossback_token` FAILS with `[EMA10回踩]` not in sections.

- [ ] **Step 3: Modify `_parse_reason` to add `[EMA10回踩]` token**

Edit `signals/report.py:349` to add `[EMA10回踩]` to the recognized token list:

```python
def _parse_reason(reason: str) -> dict:
    """把 '[趋势] ... | [RS] ... | [VCP] ...' 拆分成字典。"""
    sections = {}
    parts = reason.split(" | ")
    for part in parts:
        part = part.strip()
        for key in ["[趋势]", "[RS]", "[VCP]", "[突破]", "[量能]", "[EMA10回踩]"]:
            if part.startswith(key):
                sections[key] = part[len(key):].strip()
                break
    return sections
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/signals/test_report.py -v 2>&1 | tail -10`
Expected: 2 tests PASS.

---

### Task 4.3: Add crossback section to v_eps_v2 report

**Files:**
- Modify: `signals/report.py` — `generate_daily_report` function

- [ ] **Step 1: Read current `generate_daily_report` signature**

Read `signals/report.py` to find `generate_daily_report`. The function currently:
- Takes `(records, strategy_id, strategy_name)`
- Returns a markdown file path

- [ ] **Step 2: Extend signature with optional crossback parameters**

Modify `generate_daily_report` to accept:

```python
def generate_daily_report(
    records: list[dict],
    strategy_id: str,
    strategy_name: str,
    crossback_records: list[dict] | None = None,
    watchlist_status: dict | None = None,
) -> str:
    """
    生成 v_eps_v2 每日 Markdown 报告。

    新增参数（spec §4.3）：
        crossback_records: 今日触发的 crossback 信号（来自 run_ema_crossback_strategy）
        watchlist_status: {"total": N, "added_today": M, "triggered_today": K}
    """
```

- [ ] **Step 3: Add helper + append crossback section at end of report**

First, add this helper function at module level in `signals/report.py` (place it near `_parse_reason`):

```python
import re

def _parse_crossback_metrics(cb_text: str) -> dict:
    """从 [EMA10回踩] 段文本里抽取 现价 / 上穿涨幅 / 量比。

    Returns:
        dict with keys: close_str, pct_str, vol_str (任一字段缺失时为 '—')
    """
    m_close = re.search(r"\$(\d+\.\d+)", cb_text)
    close_str = f"${m_close.group(1)}" if m_close else "—"
    m_pct = re.search(r"\+(-?\d+\.\d+)%", cb_text)
    pct_str = f"+{m_pct.group(1)}%" if m_pct else "—"
    m_vol = re.search(r"量能(\d+\.\d+)x", cb_text)
    vol_str = f"{m_vol.group(1)}x" if m_vol else "—"
    return {"close_str": close_str, "pct_str": pct_str, "vol_str": vol_str}
```

Then, find the end of `generate_daily_report` (where the markdown body is assembled). After the existing tail content (but before the file write), add:

```python
    # === Crossback 区段（spec §4.3） ===
    if crossback_records or watchlist_status:
        lines.append("")
        lines.append("## EMA 10 Crossback 信号")
        lines.append("")
        if crossback_records:
            lines.append("| 日期 | 股票 | 强度 | 现价 | 参考止损 | 量比 | 上穿 EMA10 | 上穿涨幅 |")
            lines.append("|------|------|------|------|----------|------|-----------|---------|")
            for r in crossback_records:
                # 从 reason 解析 [EMA10回踩] 内容
                sections = _parse_reason(r.get("触发原因", ""))
                cb_text = sections.get("[EMA10回踩]", "")
                metrics = _parse_crossback_metrics(cb_text)
                lines.append(
                    f"| {r['日期']} | {r['股票']} | {r['强度星级']} "
                    f"| {metrics['close_str']} | {r.get('参考止损', '—')} "
                    f"| {metrics['vol_str']} | — | {metrics['pct_str']} |"
                )
        else:
            lines.append("今日无 crossback 信号。")

        if watchlist_status:
            lines.append("")
            lines.append("### Watch List 状态")
            lines.append("")
            lines.append(f"- 当前监控：**{watchlist_status.get('total', 0)}** 只")
            lines.append(f"- 今日新增：**{watchlist_status.get('added_today', 0)}** 只（来自 v_eps_v2_buy）")
            lines.append(f"- 今日触发 crossback：**{watchlist_status.get('triggered_today', 0)}** 只")
            lines.append("")
```

- [ ] **Step 4: Smoke-test the report generation with monkeypatched OUTPUT_DIR**

Run: `python -c "
import sys
sys.path.insert(0, '.')
import tempfile
from pathlib import Path

# Monkeypatch OUTPUT_DIR to a tmp dir to avoid polluting real output/
import signals.report as report_mod
tmpdir = Path(tempfile.mkdtemp())
original = report_mod.OUTPUT_DIR
report_mod.OUTPUT_DIR = tmpdir
try:
    from signals.report import generate_daily_report
    records = [{'日期': '2026-08-24', '股票': 'CYPH', '信号': '买入', '信号类型': 'v_eps_v2_buy', '强度星级': '★★★☆☆', '参考止损': '\$1.30', '触发原因': '[EPS] test'}]
    cb_records = [{'日期': '2026-08-24', '股票': 'CYPH', '信号': '买入', '信号类型': 'crossback_buy', '强度星级': '★★★☆☆', '参考止损': '\$1.05', '触发原因': '[EMA10回踩] 上穿EMA10(\$1.18, +10.3%) + 量能2.5x均量'}]
    path = generate_daily_report(
        records, strategy_id='v_eps_v2', strategy_name='Weinstein + EPS',
        crossback_records=cb_records,
        watchlist_status={'total': 45, 'added_today': 8, 'triggered_today': 1},
    )
    content = Path(path).read_text(encoding='utf-8')
    assert 'EMA 10 Crossback 信号' in content, 'crossback section missing'
    assert 'Watch List 状态' in content, 'watchlist status missing'
    assert 'CYPH' in content
    print('Report generated OK:', path)
    print('Length:', len(content), 'chars')
finally:
    report_mod.OUTPUT_DIR = original
"
`
Expected: prints "Report generated OK" + length > 0.

---

### Task 4.4: Add standalone crossback report generator

**Files:**
- Modify: `signals/report.py` — add `generate_crossback_report` function

- [ ] **Step 1: Write failing test**

Add to `tests/signals/test_report.py`:

```python
def test_generate_crossback_report(tmp_path):
    """generate_crossback_report should create a markdown file at the expected path."""
    import shutil
    from pathlib import Path
    from datetime import datetime, timezone
    from signals.report import generate_crossback_report

    # Use tmp output dir
    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-18",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
            "added_at": "2026-08-18T09:37:00+00:00",
            "expires_at": "2026-10-17T09:37:00+00:00",
            "crossback_count": 1,
            "last_crossback_date": "2026-08-24",
        }
    }
    crossback_records = [{
        "日期": "2026-08-24",
        "股票": "CYPH",
        "信号": "买入",
        "信号类型": "crossback_buy",
        "强度星级": "★★★☆☆",
        "强度(1-5)": 3,
        "参考止损": "$1.05",
        "触发原因": "[EMA10回踩] 上穿EMA10($1.18, +10.3%) + 量能2.5x均量 | [RS] RS/SPY=1.05x",
    }]

    today = datetime(2026, 8, 24, tzinfo=timezone.utc)
    # Monkey-patch OUTPUT_DIR for the test
    import signals.report as report_mod
    original = report_mod.OUTPUT_DIR if hasattr(report_mod, "OUTPUT_DIR") else None
    report_mod.OUTPUT_DIR = tmp_path
    try:
        path = generate_crossback_report(
            records=crossback_records,
            watchlist=watchlist,
            strategy_id="v_eps_v2",
            today=today,
        )
        assert path is not None
        assert Path(path).exists()
        content = Path(path).read_text(encoding="utf-8")
        assert "EMA 10 回踩" in content or "Crossback" in content
        assert "CYPH" in content
        assert "CRDL" in content  # watchlist entry
    finally:
        if original is not None:
            report_mod.OUTPUT_DIR = original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/signals/test_report.py::test_generate_crossback_report -v 2>&1 | tail -10`
Expected: FAIL — `generate_crossback_report` doesn't exist.

- [ ] **Step 3: Implement `generate_crossback_report`**

Add to `signals/report.py` (at the end of the file, below other functions):

```python
def generate_crossback_report(
    records: list[dict],
    watchlist: dict,
    strategy_id: str,
    today: datetime,
) -> Path | None:
    """
    生成独立的 EMA 10 Crossback 报告（spec §4.3）。

    输出路径：output/{strategy_id}/{YYYY-MM-DD}/报告_EMA10回踩_{YYYY-MM-DD}.md
    若 records 为空且 watchlist 也为空，返回 None（不生成）。

    Returns:
        Path | None：写入的报告文件路径；若不生成则返回 None
    """
    if not records and not watchlist:
        return None

    today_str = today.strftime("%Y-%m-%d")
    report_dir = OUTPUT_DIR / strategy_id / today_str
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"报告_EMA10回踩_{today_str}.md"

    lines = []
    lines.append(f"# EMA 10 回踩报告 ({today_str})")
    lines.append("")
    lines.append(f"**策略**：EMA 10 Crossback（{strategy_id}）")
    lines.append("")
    lines.append("## 字段说明")
    lines.append("")
    lines.append("- **现价**：crossback 当日收盘价")
    lines.append("- **EMA10**：crossback 当日的 EMA(10)")
    lines.append("- **上穿涨幅**：crossback 当日 close 相对 EMA10 的涨幅 (%)")
    lines.append("- **量比**：crossback 当日成交量 ÷ 过去 20 日均量")
    lines.append("- **强度**：1-5 ★（基于量能倍数）")
    lines.append("")
    lines.append("## 今日 Crossback 信号")
    lines.append("")
    if records:
        lines.append("| 日期 | 股票 | 强度 | 现价 | 参考止损 | 量比 | 上穿 EMA10 | 上穿涨幅 |")
        lines.append("|------|------|------|------|----------|------|-----------|---------|")
        for r in records:
            sections = _parse_reason(r.get("触发原因", ""))
            cb_text = sections.get("[EMA10回踩]", "")
            metrics = _parse_crossback_metrics(cb_text)
            lines.append(
                f"| {r['日期']} | {r['股票']} | {r['强度星级']} "
                f"| {metrics['close_str']} | {r.get('参考止损', '—')} "
                f"| {metrics['vol_str']} | — | {metrics['pct_str']} |"
            )
    else:
        lines.append("今日无 crossback 信号。")

    lines.append("")
    lines.append("## 看盘建议")
    lines.append("")
    lines.append("- 首次 crossback（crossback_count == 0 → 1）：重点关注")
    lines.append("- 二次 crossback：观察量能是否继续放大")
    lines.append("- 参考止损（stage2_stop）= v_eps_v2 信号日的 close × (1 - 8%)")
    lines.append("")
    lines.append("## Watch List 状态汇总")
    lines.append("")
    lines.append(f"- 当前监控：**{len(watchlist)}** 只")
    if records:
        triggered_symbols = {r['股票'] for r in records}
        lines.append(f"- 今日触发 crossback：**{len(triggered_symbols)}** 只（{', '.join(sorted(triggered_symbols))}）")
    else:
        lines.append("- 今日触发 crossback：**0** 只")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path
```

Note: this assumes `OUTPUT_DIR` is defined at module level in `signals/report.py`. If not, add:
```python
from pathlib import Path
OUTPUT_DIR = Path(__file__).parent.parent / "output"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/signals/test_report.py -v 2>&1 | tail -10`
Expected: 3 tests PASS (parse_reason × 2 + generate_crossback_report).

---

### Task 4.5: Run all Chunk 4 tests + verify no regressions

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v 2>&1 | tail -30`
Expected: ALL tests pass across chunks 1, 2, 3, 4.

- [ ] **Step 2: Verify main.py can be imported without errors**

Run: `python -c "
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('main', 'main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print('main.py imports OK')
"`
Expected: prints "main.py imports OK".

---

**End of Chunk 4.**

After Chunk 4 is fully implemented, dispatch plan-document-reviewer for this chunk before proceeding to Chunk 5.

## Chunk 5: Config + End-to-End Verification

**Goal:** Wire `config.yaml` `ema_crossback` block so values are not hardcoded, then run a full end-to-end live test to verify the pipeline produces correct outputs.

**Files modified in this chunk:**
- `config.yaml` — add `ema_crossback` block
- `main.py` — replace hardcoded `expire_days=60` with config value

**Files verified in this chunk:**
- `output/v_eps_v2/watchlist.json` — must exist after live run
- `output/v_eps_v2/signals.csv` — must have 7-column header
- `output/v_eps_v2/{YYYY-MM-DD}/报告_*.md` — main report generated
- `output/v_eps_v2/{YYYY-MM-DD}/报告_EMA10回踩_*.md` — crossback report generated

**Reference:** Spec §7 (configuration), §9.2 (integration test)

**Prerequisites (must be complete before starting this chunk):**
- ✅ Chunk 1 — `SignalEvent` schema extended, CSV 7-col migration done
- ✅ Chunk 2 — `strategies/ema_crossback/watchlist.py` (load/save/add/prune) implemented
- ✅ Chunk 3 — `detect_crossback` + `run_ema_crossback_strategy` implemented
- ✅ Chunk 4 — `main.py` crossback integration block exists, `signals/report.py` has `_parse_crossback_metrics` + `generate_crossback_report`

**Cleanup safety note:** Task 5.3 uses a backup-rename approach (NOT `rm -rf`) to preserve any historical data. Do NOT use `rm -rf` on `output/` — see MEMORY.md 2026-04-13 + 2026-05-14 lessons about destructive operations.

---

### Task 5.1: Add `ema_crossback` block to `config.yaml`

**Files:**
- Modify: `config.yaml`

- [ ] **Step 1: Read current `config.yaml` to find insertion point**

Read `config.yaml` and identify the end of the file. We'll append the `ema_crossback` block at the **end** (least risk of breaking other config).

- [ ] **Step 2: Append the `ema_crossback` block at the end of `config.yaml`**

```yaml
# EMA 10 Crossback strategy (spec §7)
ema_crossback:
  enabled: true                    # 总开关
  ema_period: 10                   # EMA 周期
  volume_mult: 1.5                 # 量能倍数阈值（crossback 当日量 ÷ 20日均量）
  volume_lookback: 20              # 量能回看天数
  watchlist_expire_days: 60        # watch list 过期天数
  output:
    append_to_main_csv: true       # 是否追加到 signals.csv
    generate_independent_report: true  # 是否生成独立 crossback 报告
    append_section_to_v_eps_v2_report: true  # 是否在 v_eps_v2 报告末尾加 crossback 区段
```

- [ ] **Step 3: Verify YAML parses correctly**

Run: `python -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
cb = cfg.get('ema_crossback', {})
assert cb.get('enabled') is True
assert cb.get('ema_period') == 10
assert cb.get('volume_mult') == 1.5
assert cb.get('watchlist_expire_days') == 60
print('config.yaml ema_crossback block OK')
print('Keys:', list(cb.keys()))
"`
Expected: prints "config.yaml ema_crossback block OK" with 6 keys (enabled, ema_period, volume_mult, volume_lookback, watchlist_expire_days, output).

---

### Task 5.2: Update `main.py` to use config values instead of hardcoded ones

**Files:**
- Modify: `main.py` (crossback integration block from Chunk 4)

- [ ] **Step 1: Read current main.py crossback integration**

Open `main.py` and find the crossback block from Chunk 4 Task 4.1 Step 3. Identify hardcoded values:
- Line 2042: `expire_days=60` — should be `cb_cfg.get("watchlist_expire_days", 60)`
- Line 2053: `cache_dir=Path("data/cache")` — acceptable, but should use config if defined
- Line 2048: `if config.get("ema_crossback", {}).get("enabled", True):` — already uses config ✓
- Line 2087: `if config.get("ema_crossback", {}).get("output", {}).get("generate_independent_report", True):` — already uses config ✓

- [ ] **Step 2: Replace hardcoded `expire_days=60` with config-driven value**

Edit the crossback integration block in `main.py`:

```python
    # === Crossback 集成（spec §6.2） ===
    cb_cfg = config.get("ema_crossback", {})
    cb_enabled = cb_cfg.get("enabled", True)
    expire_days = cb_cfg.get("watchlist_expire_days", 60)

    # 1. Drain v_eps_v2 信号（保留 events 供 watchlist 更新 + crossback 输入）
    v_eps_events = drain_signals(queue)

    # 2. 更新 watch list（spec §4.1）
    watchlist_path = OUTPUT_DIR / strategy_id / "watchlist.json"
    watchlist = load_watchlist(watchlist_path)

    # 把 v_eps_v2 信号转为 watchlist 条目（spec §6.2 core要点 #4）
    new_entries = []
    for evt in v_eps_events:
        if evt.data.get("signal_type") == "v_eps_v2_buy":
            signal_price = evt.data.get("signal_price")
            if signal_price is None:
                logger.warning(f"[main] {evt.symbol} missing signal_price, skipping watchlist")
                continue
            stage2_stop = evt.data.get("stop_loss")  # already = signal_price * 0.92
            new_entries.append({
                "symbol": evt.symbol,
                "signal_date": evt.timestamp.strftime("%Y-%m-%d"),
                "signal_price": signal_price,
                "stage2_stop": stage2_stop,
            })

    watchlist = add_signals_to_watchlist(
        watchlist, new_entries, expire_days=expire_days, today=today
    )
    watchlist = prune_expired(watchlist, today)

    # 3. 运行 crossback 策略
    crossback_events = []
    if cb_enabled:
        crossback_events = run_ema_crossback_strategy(
            watchlist,
            market_data=market_data,
            today=today,
            cache_dir=Path("data/cache"),
            ema_period=cb_cfg.get("ema_period", 10),
            vol_mult=cb_cfg.get("volume_mult", 1.5),
            vol_lookback=cb_cfg.get("volume_lookback", 20),
        )

    # CRITICAL：必须无条件持久化 watchlist —— run_ema_crossback_strategy
    # 会在内存中直接修改入参 watchlist 的 crossback_count / last_crossback_date
    save_watchlist(watchlist, watchlist_path)

    # 4. 合并 v_eps_v2 + crossback events，统一输出
    combined_queue = EventQueue()
    for e in v_eps_events:
        combined_queue.put(e)
    for e in crossback_events:
        combined_queue.put(e)
    records = process_signals(combined_queue, config, strategy_id=strategy_id)
    if not records:
        print("\n今日无有效信号。")
        print("（SEPA 条件严格，无信号是正常的。有信号时才需要关注。）")

    # 5. 生成报告（含 v_eps_v2 + crossback 两类）
    report_path = generate_daily_report(
        records,
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        crossback_records=[r for r in records if r.get("信号类型") == "crossback_buy"],
        watchlist_status={
            "total": len(watchlist),
            "added_today": len(new_entries),
            "triggered_today": len(crossback_events),
        },
    )
    print(f"\n[报告] 今日报告：{report_path}")

    # 6. 独立 crossback 报告（spec §4.3）
    if cb_cfg.get("output", {}).get("generate_independent_report", True):
        cb_report_path = generate_crossback_report(
            records=[_event_to_record(e) for e in crossback_events],
            watchlist=watchlist,
            strategy_id=strategy_id,
            today=today,
        )
        if cb_report_path:
            print(f"[报告] Crossback 报告：{cb_report_path}")
```

- [ ] **Step 3: Verify main.py syntax**

Run: `python -c "import ast; ast.parse(open('main.py').read()); print('Syntax OK')"`
Expected: prints "Syntax OK".

---

### Task 5.3: Run full end-to-end live test

**Goal:** Verify the entire pipeline (v_eps_v2 → watchlist → crossback → CSV → reports) works against real data.

**IMPORTANT:** Use backup-rename (NOT `rm -rf`) to preserve historical data.

- [ ] **Step 1: Capture pre-run state for comparison**

Run: `python -c "
import json
import os
from pathlib import Path
out = Path('output/v_eps_v2')
if out.exists():
    print('PRE_WATCHLIST_EXISTS:', out.exists())
    wl_path = out / 'watchlist.json'
    if wl_path.exists():
        wl = json.loads(wl_path.read_text())
        print('PRE_WATCHLIST_COUNT:', len(wl))
        print('PRE_WATCHLIST_SYMBOLS:', sorted(wl.keys()))
    else:
        print('PRE_WATCHLIST_COUNT: 0')
else:
    print('PRE_WATCHLIST_EXISTS: False')
print('PRE_TODAY_DIR:', 'output/v_eps_v2/' + __import__('datetime').datetime.now(tz=__import__('datetime').timezone.utc).strftime('%Y-%m-%d'))
" | tee /tmp/pre_run_chunk5.txt`
Expected: prints PRE_WATCHLIST_EXISTS, PRE_WATCHLIST_COUNT, and PRE_WATCHLIST_SYMBOLS.

- [ ] **Step 2: Rename any existing output (NOT delete) for clean test**

Run: `BACKUP_NAME="output/v_eps_v2.bak.chunk5-$(date -u +%Y%m%d-%H%M%S)"; if [ -d "output/v_eps_v2" ]; then mv "output/v_eps_v2" "$BACKUP_NAME"; echo "Renamed to: $BACKUP_NAME"; else echo "No pre-existing output/v_eps_v2 to rename"; fi | tee /tmp/backup_chunk5.txt`
Expected: prints "Renamed to: <timestamped name>" OR "No pre-existing output/v_eps_v2 to rename". NEVER uses `rm -rf`.

- [ ] **Step 3: Run the live test**

Run: `python main.py --mode live --strategy v_eps_v2 2>&1 | tee /tmp/livetest_chunk5.log; echo "EXIT_CODE: ${PIPESTATUS[0]}"`
Expected:
- exit code 0 (printed as EXIT_CODE: 0)
- stdout contains `[报告] 今日报告：` line (always)
- stdout MAY contain `[报告] Crossback 报告：` line (only if crossback signals were triggered; absence on quiet days is normal)
- no Python exceptions in log

- [ ] **Step 4: Verify `watchlist.json` was created**

Run: `python -c "
import json
with open('output/v_eps_v2/watchlist.json') as f:
    wl = json.load(f)
print(f'Watchlist has {len(wl)} entries')
assert isinstance(wl, dict)
if wl:
    sample_sym = next(iter(wl))
    entry = wl[sample_sym]
    print(f'Sample: {sample_sym}')
    for key in ['symbol', 'signal_date', 'signal_price', 'stage2_stop', 'expires_at', 'crossback_count']:
        assert key in entry, f'Missing key: {key}'
        print(f'  {key}: {entry[key]}')
"`
Expected: prints watchlist count and sample entry with all 6 required keys.

- [ ] **Step 5: Verify watchlist grew vs. pre-run state**

Run: `python -c "
import json, re
from pathlib import Path
pre = Path('/tmp/pre_run_chunk5.txt').read_text()
m = re.search(r'PRE_WATCHLIST_COUNT: (\d+)', pre)
pre_count = int(m.group(1)) if m else 0

cur_path = Path('output/v_eps_v2/watchlist.json')
cur_count = len(json.loads(cur_path.read_text())) if cur_path.exists() else 0
print(f'Pre: {pre_count}, Post: {cur_count}, Growth: {cur_count - pre_count}')
assert cur_count >= pre_count, f'Watchlist shrunk: {pre_count} -> {cur_count}'
print('PASS: watchlist did not shrink')
"`
Expected: prints `Pre: N, Post: M, Growth: M-N` where M >= N (count may be 0+0 on signal-less day, that's OK).

- [ ] **Step 6: Verify `signals.csv` has 7-column header (if file exists)**

Run: `python -c "
from pathlib import Path
csv_path = Path('output/v_eps_v2/signals.csv')
if not csv_path.exists():
    print('SKIP: signals.csv does not exist (no signals today is normal)')
else:
    with open(csv_path) as f:
        header = f.readline().strip()
    expected = '日期,股票,信号,信号类型,强度(1-5),触发原因,参考止损'
    print(f'Header: {header}')
    assert header == expected, f'Header mismatch:\\n  got: {header}\\n  want: {expected}'
    print('PASS: 7-column header OK')
"`
Expected: prints "SKIP: signals.csv does not exist..." OR "PASS: 7-column header OK".

- [ ] **Step 7: Verify signal rows have valid `信号类型` (if CSV exists)**

Run: `python -c "
import csv
from pathlib import Path
csv_path = Path('output/v_eps_v2/signals.csv')
if not csv_path.exists():
    print('SKIP: no CSV today')
else:
    with open(csv_path, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    valid_types = {'v_eps_v2_buy', 'crossback_buy'}
    actual_types = set(r.get('信号类型', '') for r in rows)
    print(f'Rows: {len(rows)}, types: {actual_types}')
    assert actual_types.issubset(valid_types), f'Invalid types: {actual_types - valid_types}'
    print('PASS: all rows have valid 信号类型')
"`
Expected: prints "SKIP: no CSV today" OR "PASS: all rows have valid 信号类型".

- [ ] **Step 8: Verify daily main report was generated**

Run: `python -c "
import os
from datetime import datetime, timezone
from pathlib import Path
today = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
day_dir = Path(f'output/v_eps_v2/{today}')
if not day_dir.exists():
    print(f'FAIL: {day_dir} does not exist — E2E failed silently. Check /tmp/livetest_chunk5.log')
    exit(1)
main_reports = list(day_dir.glob('报告_*.md'))
main_reports = [p for p in main_reports if 'EMA10回踩' not in p.name]
print(f'Main reports: {[p.name for p in main_reports]}')
assert main_reports, 'No main report generated'
print('PASS: main report exists')
"`
Expected: prints "PASS: main report exists" with the report filename.

- [ ] **Step 9: Verify crossback report (conditional on crossback events)**

Run: `python -c "
from datetime import datetime, timezone
from pathlib import Path
today = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
day_dir = Path(f'output/v_eps_v2/{today}')
cb_reports = list(day_dir.glob('报告_EMA10回踩_*.md')) if day_dir.exists() else []
if cb_reports:
    print(f'Crossback report exists: {cb_reports[0].name}')
    print('PASS: crossback report generated')
else:
    print('INFO: no crossback report today (normal if no crossback signals triggered)')
"`
Expected: prints "Crossback report exists..." OR "INFO: no crossback report today (normal...)". NEVER asserts failure.

- [ ] **Step 10: Verify crossback report content has expected sections (if exists)**

Run: `python -c "
from datetime import datetime, timezone
from pathlib import Path
import re
today = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
day_dir = Path(f'output/v_eps_v2/{today}')
cb_reports = list(day_dir.glob('报告_EMA10回踩_*.md')) if day_dir.exists() else []
if not cb_reports:
    print('SKIP: no crossback report')
else:
    content = cb_reports[0].read_text(encoding='utf-8')
    expected = ['## 字段说明', '## 今日 Crossback 信号', '## 看盘建议', '## Watch List 状态汇总']
    found = [h for h in expected if h in content]
    print(f'Found {len(found)}/{len(expected)} expected sections')
    for h in expected:
        marker = 'OK' if h in content else 'MISSING'
        print(f'  [{marker}] {h}')
    assert len(found) == len(expected), 'Missing required sections'
    print('PASS: all 4 sections present')
"`
Expected: prints "SKIP: no crossback report" OR "PASS: all 4 sections present" with per-section status.

- [ ] **Step 11: Verify v_eps_v2 report has crossback section appended (if main report exists)**

Run: `python -c "
from datetime import datetime, timezone
from pathlib import Path
today = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
day_dir = Path(f'output/v_eps_v2/{today}')
main_reports = [p for p in day_dir.glob('报告_*.md') if 'EMA10回踩' not in p.name] if day_dir.exists() else []
if not main_reports:
    print('SKIP: no main report')
else:
    content = main_reports[0].read_text(encoding='utf-8')
    has_section = '## EMA 10 Crossback 信号' in content
    has_status = '### Watch List 状态' in content
    print(f'Has crossback section: {has_section}')
    print(f'Has watchlist status: {has_status}')
    assert has_section and has_status, 'Missing crossback section or watchlist status'
    print('PASS: main report has crossback section + watchlist status')
"`
Expected: prints "SKIP: no main report" OR "PASS: main report has crossback section + watchlist status".

- [ ] **Step 12: Verify exit code was 0**

Run: `grep 'EXIT_CODE:' /tmp/livetest_chunk5.log`
Expected: prints "EXIT_CODE: 0".

- [ ] **Step 13: Decide whether to keep or restore the backup**

Run: `ls -d output/v_eps_v2.bak.chunk5-* 2>/dev/null`
If empty (no backup was created): skip.
If non-empty:
- If all previous E2E steps PASSED: ask user whether to keep backup (`rm -rf output/v_eps_v2.bak.chunk5-*`) or restore it (`mv output/v_eps_v2.bak.chunk5-* output/v_eps_v2`).
- If any step FAILED: keep the backup for debugging — do NOT delete it.

---

### Task 5.4: Final test suite verification

**Goal:** Confirm all chunks (1, 2, 3, 4, 5) tests pass together.

- [ ] **Step 1: Run full test suite (write to file to avoid truncation)**

Run: `pytest tests/ -v --tb=short > /tmp/pytest_chunk5.log 2>&1; echo "EXIT_CODE: $?" | tee -a /tmp/pytest_chunk5.log`
Expected: exit code 0 (last line shows `EXIT_CODE: 0`).

- [ ] **Step 2: Verify pass/fail counts**

Run: `PASS_COUNT=$(grep -c "PASS" /tmp/pytest_chunk5.log); FAIL_COUNT=$(grep -cE "FAIL|ERROR" /tmp/pytest_chunk5.log); echo "PASS: $PASS_COUNT, FAIL/ERROR: $FAIL_COUNT"; if [ "$FAIL_COUNT" -gt 0 ]; then echo "FAILURES:"; grep -E "FAIL|ERROR" /tmp/pytest_chunk5.log | head -20; exit 1; fi`
Expected: `FAIL/ERROR: 0`. PASS count should be ~30+.

- [ ] **Step 3: Show summary of all test names**

Run: `grep "::" /tmp/pytest_chunk5.log | grep -v "PASS\|FAIL" | head -40`
Expected: list of test names without truncation (full log is in /tmp/pytest_chunk5.log if more than 40).

---

**End of Chunk 5.**

After Chunk 5 is fully implemented and verified, the plan is complete.

---

## Plan Completion Summary

**Total Chunks:** 5

**Files Created (NEW):**
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/events/__init__.py`
- `tests/events/test_signal_event.py`
- `tests/signals/__init__.py`
- `tests/signals/test_generator.py`
- `tests/signals/test_report.py`
- `tests/strategies/__init__.py`
- `tests/strategies/ema_crossback/__init__.py`
- `tests/strategies/ema_crossback/test_watchlist.py`
- `tests/strategies/ema_crossback/test_detect_crossback.py`
- `tests/strategies/ema_crossback/test_run_crossback.py`
- `strategies/ema_crossback/__init__.py`
- `strategies/ema_crossback/watchlist.py`
- `strategies/ema_crossback/ema_crossback_strategy.py`

**Files Modified:**
- `events/base.py` — `SignalEvent.create()` adds `signal_type` + `signal_price`
- `strategies/v_eps_v2/eps_v2_strategy.py` — passes `signal_price=close`
- `signals/generator.py` — adds `drain_signals`, 7-col CSV schema, migration logic
- `signals/report.py` — `_parse_reason` adds `[EMA10回踩]` token, new `_parse_crossback_metrics` helper, extended `generate_daily_report` signature, new `generate_crossback_report` function
- `main.py` — wires crossback pipeline between v_eps_v2 run and report
- `config.yaml` — adds `ema_crossback` block

**Execution Approach:** Use `superpowers:subagent-driven-development` skill (preferred) for parallel per-task execution, or `superpowers:executing-plans` for sequential in-session execution.
