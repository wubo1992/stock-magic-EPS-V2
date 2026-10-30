"""旧 schema 的 signals.csv 会让整轮扫描崩掉（2026-10-30 真实故障）。

现场：`uv run python main.py --mode live --all-strategies` 在第一个非
v_eps_v2 策略（v_weinstein）生成报告时抛

    ValueError: invalid literal for int() with base 10: 'v_eps_v2_buy'

链路：
  1. `_append_to_csv` 的迁移逻辑被 gate 在 `strategy_id == "v_eps_v2"` 上，
     于是 v_weinstein 的 7 列 signals.csv 从来没被迁移过；
  2. 但追加写入用的是全局 `_CSV_HEADERS`（12 列）→ 12 字段的行被塞进
     7 列表头的文件里 → **列错位**；
  3. DictReader 按**表头名字**取值 → 第 4 列的值落到「强度(1-5)」头上，
     而第 4 列的值是「信号类型」= 'v_eps_v2_buy'；
  4. 报告里 `int(r.get("强度(1-5)", 0))` → ValueError → 整轮扫描死掉。

同时暴露第二个问题：`_event_to_record` 的信号类型默认值是
"V_EPS_V2_BUY_SIGNAL_TYPE"，于是**别的策略的信号在 CSV 里全被标成
v_eps_v2_buy**。
"""
import csv

from datetime import datetime, timezone

import pytest

from events import SignalEvent
from events.base import CROSSBACK_BUY_SIGNAL_TYPE, V_EPS_V2_BUY_SIGNAL_TYPE


LEGACY_HEADER = "日期,股票,信号,强度(1-5),触发原因,参考止损,持仓股数"


def _write_legacy_csv(path, strategy_rows=("2026-10-29,AEHR,买入,3,old reason,$56.88,100",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [LEGACY_HEADER, *strategy_rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _new_record(symbol="CNTA", signal_type=V_EPS_V2_BUY_SIGNAL_TYPE):
    return {
        "日期": "2026-10-30",
        "股票": symbol,
        "信号": "买入",
        "信号类型": signal_type,
        "强度(1-5)": 3,
        "强度星级": "★★★☆☆",
        "触发原因": "[Stage2] SMA150(31)上升30天 | [量能] 2.8x均量",
        "参考止损": "$36.50",
    }


def test_legacy_csv_is_migrated_for_any_strategy(tmp_path, monkeypatch):
    """任何策略的旧 schema 文件都要迁移 —— 不只是 v_eps_v2。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    csv_file = tmp_path / "v_weinstein" / "signals.csv"
    _write_legacy_csv(csv_file)

    gen_module._append_to_csv([_new_record()], strategy_id="v_weinstein")

    header = csv_file.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert len(header) == 12, f"legacy header should be migrated to 12 cols, got: {header}"
    assert "信号类型" in header
    assert "RS相对强度" in header


def test_appended_row_does_not_shift_columns_in_a_legacy_file(tmp_path, monkeypatch):
    """追加的行不能把旧行的列挤歪 —— 这正是今天崩溃的那一步。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    csv_file = tmp_path / "v_weinstein" / "signals.csv"
    _write_legacy_csv(csv_file)

    gen_module._append_to_csv([_new_record()], strategy_id="v_weinstein")

    with open(csv_file, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    old = next(r for r in rows if r["股票"] == "AEHR")
    new = next(r for r in rows if r["股票"] == "CNTA")

    # 关键断言：强度列必须还是数字，不是被挤过来的「信号类型」
    assert old["强度(1-5)"] == "3"
    assert int(new["强度(1-5)"]) == 3
    assert new["参考止损"] == "$36.50"
    assert new["触发原因"].startswith("[Stage2]")


def test_migrated_legacy_rows_get_a_strategy_specific_signal_type(tmp_path, monkeypatch):
    """旧行补的「信号类型」要跟着策略走，不能一律 v_eps_v2_buy。"""
    from signals import generator as gen_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)

    csv_file = tmp_path / "v_weinstein" / "signals.csv"
    _write_legacy_csv(csv_file)

    gen_module._append_to_csv([], strategy_id="v_weinstein")

    with open(csv_file, encoding="utf-8-sig") as f:
        row = next(csv.DictReader(f))

    assert row["信号类型"] == "v_weinstein_buy"


def test_event_to_record_signal_type_follows_the_strategy():
    """信号类型按策略推导；crossback 显式指定的保持不变。"""
    from signals.generator import _event_to_record

    evt = SignalEvent.create(
        symbol="CNTA",
        timestamp=datetime(2026, 10, 30, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="[Stage2] SMA150(31)上升30天",
    )

    assert _event_to_record(evt, strategy_id="v_eps_v2")["信号类型"] == V_EPS_V2_BUY_SIGNAL_TYPE
    assert _event_to_record(evt, strategy_id="v_weinstein")["信号类型"] == "v_weinstein_buy"

    cb = SignalEvent.create(
        symbol="CRDL",
        timestamp=datetime(2026, 10, 30, tzinfo=timezone.utc),
        direction="buy",
        strength=3,
        reason="[EMA10回踩] 上穿EMA10",
        signal_type=CROSSBACK_BUY_SIGNAL_TYPE,
    )
    assert _event_to_record(cb, strategy_id="v_eps_v2")["信号类型"] == CROSSBACK_BUY_SIGNAL_TYPE


def test_report_renders_a_legacy_strategy_csv_without_crashing(tmp_path, monkeypatch):
    """端到端复现今天的崩溃：legacy CSV → 追加 → 生成报告。"""
    from signals import generator as gen_module
    from signals import report as report_module

    monkeypatch.setattr(gen_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(report_module, "OUTPUT_DIR", tmp_path)

    csv_file = tmp_path / "v_weinstein" / "signals.csv"
    _write_legacy_csv(csv_file)
    gen_module._append_to_csv([_new_record()], strategy_id="v_weinstein")

    records = [gen_module._event_to_record(
        SignalEvent.create(
            symbol="CNTA",
            timestamp=datetime(2026, 10, 30, tzinfo=timezone.utc),
            direction="buy",
            strength=3,
            reason="[Stage2] SMA150(31)上升30天 | [量能] 2.8x均量",
            stop_loss=36.50,
        ),
        strategy_id="v_weinstein",
    )]

    report_path = report_module.generate_daily_report(
        records, strategy_id="v_weinstein", strategy_name="Weinstein"
    )
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "CNTA" in content
