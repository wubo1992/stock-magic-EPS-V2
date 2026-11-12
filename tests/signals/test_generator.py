"""Tests for signals/generator.py helpers."""
from datetime import datetime, timezone

import pytest

from events import EventQueue, SignalEvent
from events.base import V_EPS_V2_BUY_SIGNAL_TYPE, CROSSBACK_BUY_SIGNAL_TYPE


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
        signal_type=CROSSBACK_BUY_SIGNAL_TYPE,
    ))

    events = drain_signals(q)
    assert events[0].data["signal_type"] == CROSSBACK_BUY_SIGNAL_TYPE


def test_event_to_record_includes_signal_type():
    """_event_to_record should return a dict with '信号类型' key."""
    from signals.generator import _event_to_record

    evt = SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="crossback",
        signal_type=CROSSBACK_BUY_SIGNAL_TYPE,
    )
    record = _event_to_record(evt)
    assert "信号类型" in record
    assert record["信号类型"] == CROSSBACK_BUY_SIGNAL_TYPE


def test_event_to_record_signal_type_default():
    """_event_to_record should default to V_EPS_V2_BUY_SIGNAL_TYPE when signal_type is missing."""
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
    assert record["信号类型"] == V_EPS_V2_BUY_SIGNAL_TYPE


def test_csv_migration_adds_signal_type_column(tmp_path, monkeypatch):
    """When v_eps_v2/signals.csv exists with 6-col header, migrate to 12-col."""
    from signals import generator as gen_module
    from signals.generator import V_EPS_V2_STRATEGY_ID

    # Redirect module-level OUTPUT_DIR to tmp_path so tests are isolated
    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    # Set up: write old-schema CSV (6 columns, no "信号类型")
    csv_dir = tmp_path / V_EPS_V2_STRATEGY_ID
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
        "信号类型": V_EPS_V2_BUY_SIGNAL_TYPE,  # inferred for old records
        "强度(1-5)": 3,
        "强度星级": "★★★☆☆",
        "触发原因": "new signal",
        "参考止损": "$1.30",
    }
    gen_module._append_to_csv([new_record], strategy_id=V_EPS_V2_STRATEGY_ID)

    # Verify: file has 12 columns, old record has V_EPS_V2_BUY_SIGNAL_TYPE as signal_type
    text = csv_file.read_text(encoding="utf-8-sig")
    lines = text.strip().split("\n")
    header = lines[0].split(",")
    assert len(header) == 12, f"Expected 12-col header, got: {lines[0]}"
    assert "信号类型" in header
    assert "RS相对强度" in header, "RS Rating column missing from migrated header"
    assert V_EPS_V2_BUY_SIGNAL_TYPE in lines[1], f"Old record should be backfilled: {lines[1]}"
    assert "CYPH" in lines[2]

    # Verify backup was created
    backup = csv_dir / "signals.csv.bak.schema_v1"
    assert backup.exists()


def test_csv_migration_backfills_rs_columns_from_reason(tmp_path, monkeypatch):
    """7-col CSV migration should backfill RS/SPY 等列 from the 触发原因 text."""
    import csv as _csv

    from signals import generator as gen_module
    from signals.generator import V_EPS_V2_STRATEGY_ID

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    csv_dir = tmp_path / V_EPS_V2_STRATEGY_ID
    csv_dir.mkdir(parents=True)
    csv_file = csv_dir / "signals.csv"
    reason = (
        "[EPS] EPS+$0.25 | [Stage2] SMA150上升30天 | [量能] 5.6x均量 | "
        "[RS] RS/SPY=2.31x / RS-10W=1.12x / RS-10D=1.10x (Information Technology 排名2/45)"
    )
    csv_file.write_text(
        "日期,股票,信号,信号类型,强度(1-5),触发原因,参考止损\n"
        f'2026-10-20,CYPH,买入,v_eps_v2_buy,3,"{reason}",$1.54\n',
        encoding="utf-8-sig",
    )

    gen_module._append_to_csv([], strategy_id=V_EPS_V2_STRATEGY_ID)

    with open(csv_file, encoding="utf-8-sig") as f:
        row = next(_csv.DictReader(f))

    assert row["RS/SPY"] == "2.31"
    assert row["RS-10W"] == "1.12"
    assert row["RS-10D"] == "1.10"
    assert row["板块排名"] == "2/45"
    # RS Rating 是后加的字段，老记录算不出来，必须留空而不是瞎填
    assert row["RS相对强度"] == ""
    assert (csv_dir / "signals.csv.bak.schema_v2").exists()


def test_event_to_record_uses_structured_rs_data():
    """rs_data passed on the event should land in the CSV columns."""
    from signals.generator import _event_to_record

    evt = SignalEvent.create(
        symbol="TKNO",
        timestamp=datetime(2026, 10, 22, tzinfo=timezone.utc),
        direction="buy",
        strength=5,
        reason="[RS] RS/SPY=3.92x / RS-Rating=99 (Health Care 排名1/180)",
        stop_loss=8.0,
        signal_price=8.7,
        rs_data={
            "RS相对强度": "99",
            "RS/SPY": "3.92",
            "RS-10W": "0.83",
            "RS-10D": "0.71",
            "板块排名": "1/180",
        },
    )
    record = _event_to_record(evt)
    assert record["RS相对强度"] == "99"
    assert record["RS/SPY"] == "3.92"
    assert record["板块排名"] == "1/180"


def test_event_to_record_without_rs_data_falls_back_to_reason():
    """老事件没有 rs_data 时，从 触发原因 回填 RS 列。"""
    from signals.generator import _event_to_record

    evt = SignalEvent.create(
        symbol="TKNO",
        timestamp=datetime(2026, 10, 22, tzinfo=timezone.utc),
        direction="buy",
        strength=5,
        reason="[RS] RS/SPY=3.92x / RS-Rating=99 (Health Care 排名1/180)",
        stop_loss=8.0,
    )
    record = _event_to_record(evt)
    assert record["RS/SPY"] == "3.92"
    assert record["RS相对强度"] == "99"
    assert record["板块排名"] == "1/180"


def test_csv_migration_applies_to_other_strategies_too(tmp_path, monkeypatch):
    """迁移必须对所有策略生效 —— 追加写的是 12 列，旧文件不迁移就会列错位。

    2026-10-30 真实故障：迁移被 gate 在 v_eps_v2 上，v_weinstein 的 7 列文件
    没迁移，12 字段的行塞进 7 列表头 → 读回来「强度(1-5)」= 'v_eps_v2_buy'
    → 报告里 int() 抛 ValueError → 整轮 --all-strategies 扫描死掉。
    """
    from signals import generator as gen_module

    # Redirect module-level OUTPUT_DIR to tmp_path so tests are isolated
    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    csv_dir = tmp_path / "other_strategy"
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
        "信号类型": "other_strategy_buy",
        "强度(1-5)": 3,
        "强度星级": "★★★☆☆",
        "触发原因": "test",
        "参考止损": "$20",
    }
    gen_module._append_to_csv([new_record], strategy_id="other_strategy")

    text = csv_file.read_text(encoding="utf-8-sig")
    lines = text.strip().split("\n")
    assert len(lines[0].split(",")) == 12, f"Other strategies must be migrated too: {lines[0]}"

    import csv as _csv
    with open(csv_file, encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    # 列没被挤歪
    assert rows[0]["强度(1-5)"] == "3"
    assert rows[0]["信号类型"] == "other_strategy_buy"

# ── 重跑 livetest 时同键覆盖（2026-11-12 真实问题）─────────────────────
#
# `_append_to_csv` 原来对 (日期, 股票, 信号类型) 已存在的行**直接跳过**，
# 于是重跑同一天时，CSV 里留的还是**第一次**写入时算出来的值：
# 数据刷新后重算的 量能/涨幅/强度/RS 跟旧值对不上（2026-11-12 实测：
# AEHL 量比 63.9x → 实际 15.6x；AFRM 强度 2.0x → 实际 2.2x；UMC RS 98 → 实际算不出）。
#
# 报告读的是 CSV → 报告跟着旧值跑。正确做法是**同键覆盖写入**：
# 行数不变（不重复追加），值刷新成当次算出来的。


def _cb_record(reason="[EMA10回踩] 上穿EMA10($10.18, +12.8%) 量能15.6x均量",
               strength=5, rs="8"):
    return {
        "日期": "2026-10-14",
        "股票": "AEHL",
        "信号": "买入",
        "信号类型": CROSSBACK_BUY_SIGNAL_TYPE,
        "强度(1-5)": strength,
        "强度星级": "★" * strength + "☆" * (5 - strength),
        "触发原因": reason,
        "参考止损": "$11.18",
        "RS相对强度": rs,
        "RS/SPY": "1.20",
        "RS-10W": "0.80",
        "RS-10D": "0.90",
        "板块排名": "10/133",
    }


def _seed_csv(csv_file, rows):
    import csv as _csv

    from signals.generator import _CSV_HEADERS

    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = _csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _read_rows(csv_file):
    import csv as _csv

    with open(csv_file, encoding="utf-8-sig", newline="") as f:
        return list(_csv.DictReader(f))


def test_rerun_overwrites_existing_row_instead_of_keeping_the_first(tmp_path, monkeypatch):
    """同 (日期,股票,信号类型) 的行必须被**新值覆盖**，不是保留旧值。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    csv_file = tmp_path / "v_eps_v2" / "signals.csv"
    stale = _cb_record(reason="[EMA10回踩] 上穿EMA10($10.35, +11.9%) 量能63.9x均量",
                        strength=4, rs="9")
    _seed_csv(csv_file, [stale])

    fresh = _cb_record()   # 量能 15.6x / 强度 5 / RS 8
    gen_module._append_to_csv([fresh], strategy_id="v_eps_v2")

    rows = _read_rows(csv_file)
    assert len(rows) == 1, "覆盖不能变成追加（行数必须不变）"
    assert "量能15.6x" in rows[0]["触发原因"], "触发原因应刷新成当次算出的值"
    assert "63.9x" not in rows[0]["触发原因"]
    assert int(rows[0]["强度(1-5)"]) == 5
    assert rows[0]["RS相对强度"] == "8"


def test_rerun_twice_still_keeps_one_row(tmp_path, monkeypatch):
    """连跑两次也不能变两行。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    csv_file = tmp_path / "v_eps_v2" / "signals.csv"
    _seed_csv(csv_file, [_cb_record()])

    gen_module._append_to_csv([_cb_record()], strategy_id="v_eps_v2")
    gen_module._append_to_csv([_cb_record()], strategy_id="v_eps_v2")

    assert len(_read_rows(csv_file)) == 1


def test_overwrite_leaves_other_rows_and_order_untouched(tmp_path, monkeypatch):
    """覆盖只动目标行 —— 其他行的内容和先后顺序都不能变。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    csv_file = tmp_path / "v_eps_v2" / "signals.csv"
    other = {
        "日期": "2026-11-10", "股票": "BNGO", "信号": "买入",
        "信号类型": CROSSBACK_BUY_SIGNAL_TYPE, "强度(1-5)": 5,
        "触发原因": "[EMA10回踩] 上穿EMA10($1.18, +3.9%) 量能3.0x均量",
        "参考止损": "$1.60", "RS相对强度": "23",
    }
    _seed_csv(csv_file, [other, _cb_record(
        reason="[EMA10回踩] 上穿EMA10($10.35, +11.9%) 量能63.9x均量")])
    before = _read_rows(csv_file)

    gen_module._append_to_csv([_cb_record()], strategy_id="v_eps_v2")

    after = _read_rows(csv_file)
    assert len(after) == 2
    assert after[0] == before[0], "非目标行必须逐字段不变"
    assert [r["股票"] for r in after] == ["BNGO", "AEHL"], "行顺序不能变"
    assert "15.6x" in after[1]["触发原因"], "目标行应被刷新"
    assert "63.9x" not in after[1]["触发原因"]


def test_new_key_is_appended_not_overwritten(tmp_path, monkeypatch):
    """键对不上 → 正常追加（老行为不能被破坏）。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    csv_file = tmp_path / "v_eps_v2" / "signals.csv"
    _seed_csv(csv_file, [_cb_record()])

    new = _cb_record()
    new["股票"] = "MTCH"
    new["日期"] = "2026-11-11"
    gen_module._append_to_csv([new], strategy_id="v_eps_v2")

    rows = _read_rows(csv_file)
    assert len(rows) == 2
    assert [r["股票"] for r in rows] == ["AEHL", "MTCH"]


def test_overwrite_keeps_bom_and_header(tmp_path, monkeypatch):
    """重写不能把 BOM / 12 列表头弄丢 —— 读方都靠 utf-8-sig。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    csv_file = tmp_path / "v_eps_v2" / "signals.csv"
    _seed_csv(csv_file, [_cb_record(
        reason="[EMA10回踩] 上穿EMA10($10.35, +11.9%) 量能63.9x均量")])

    gen_module._append_to_csv([_cb_record()], strategy_id="v_eps_v2")

    raw = csv_file.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM 丢了"
    header = raw.decode("utf-8-sig").splitlines()[0].split(",")
    assert len(header) == 12 and "RS相对强度" in header
    assert "15.6x" in raw.decode("utf-8-sig"), "重写后应是新值"
