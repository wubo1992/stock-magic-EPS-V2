"""
signals/generator.py — 信号输出模块

职责：接收 SIGNAL 事件，格式化后输出到控制台和/或 CSV 文件。

输出格式（来自 SYSTEM_SPEC.md）：
    时间       | 股票 | 信号 | 强度  | RS  | 触发原因           | 参考止损
    2026-03-03 | NVDA | 买入 | ★★★★ | 92  | 突破30日高点，...  | $109.02

输出路径：
    output/{strategy_id}/signals.csv
    （每个策略独立存储，互不干扰）
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from data.rs import RS_COLUMN_NAMES, parse_rs_columns_from_reason
from events import EventQueue, EventType, SignalEvent
from events.base import V_EPS_V2_BUY_SIGNAL_TYPE

# 输出目录（父目录）
OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 策略 ID 常量（v_eps_v2 特定，signals/generator.py 模块内部使用）
V_EPS_V2_STRATEGY_ID = "v_eps_v2"

# 方向翻译
_DIRECTION_CN = {
    "buy": "买入",
    "sell": "卖出",
    "watch": "观望",
}

# CSV 列名：v_eps_v2 仅做选股，不追踪持仓 → 移除 "持仓股数" 列
# 12 列 schema: 含 "信号类型"（v_eps_v2_buy / crossback_buy）+ 5 个 RS 列
_CSV_HEADERS = [
    "日期", "股票", "信号", "信号类型",
    "强度(1-5)", "触发原因", "参考止损",
] + RS_COLUMN_NAMES

# 迁移检测：旧 CSV 的列数 → 备份文件后缀
#   6 列 = 无 "信号类型"
#   7 列 = 有 "信号类型"、无 RS 列
_OLD_SCHEMA_VERSIONS = {
    6: "v1",   # 6 列
    7: "v2",   # 7 列
}


def process_signals(
    queue: EventQueue,
    config: dict,
    strategy_id: str = "v1_wizard",
) -> list[dict]:
    """
    从事件队列中取出所有 SIGNAL 事件，输出到控制台和 CSV。

    参数：
        queue:       事件队列
        config:      从 config.yaml 读取的配置
        strategy_id: 策略文件夹 ID（如 "v1_wizard"），决定 CSV 存储路径

    返回：
        当次处理的所有信号记录（字典列表）
    """
    output_cfg = config.get("output", {})
    print_to_console = output_cfg.get("print_to_console", True)
    save_to_csv = output_cfg.get("save_to_csv", True)

    records = []
    while not queue.empty():
        event = queue.get(timeout=0.05)
        if event is None:
            break
        if event.type == EventType.SIGNAL:
            record = _event_to_record(event, strategy_id=strategy_id)
            records.append(record)

    if not records:
        return []

    if print_to_console:
        _print_table(records)

    if save_to_csv:
        _append_to_csv(records, strategy_id)

    return records


def format_signals(signals: list[SignalEvent]) -> list[dict]:
    """
    把 SignalEvent 列表直接转换为记录（不经过队列）。
    用于回测引擎直接调用。
    """
    return [_event_to_record(s) for s in signals]


def _default_signal_type(strategy_id: str | None, direction: str) -> str:
    """按「策略 ID + 方向」推导信号类型。

    以前所有策略都共用 "v_eps_v2_buy" 这一个默认值 —— 于是 v_weinstein 的信号
    在自己的 signals.csv 里也被标成 v_eps_v2_buy。除了标签难看，更麻烦的是
    它会挤歪旧 schema 文件的列（见 _append_to_csv 的迁移逻辑）。
    """
    if not strategy_id:
        return V_EPS_V2_BUY_SIGNAL_TYPE
    suffix = {"buy": "buy", "sell": "sell", "watch": "watch"}.get(direction, "buy")
    return f"{strategy_id}_{suffix}"


def _event_to_record(event: SignalEvent, strategy_id: str | None = None) -> dict:
    """把 SignalEvent 转换成可输出的字典。"""
    date_str = event.timestamp.strftime("%Y-%m-%d")
    direction = event.data.get("direction", "watch")
    strength = event.data.get("strength", 0)
    reason = event.data.get("reason", "")
    stop_loss = event.data.get("stop_loss")
    signal_type = event.data.get("signal_type") or _default_signal_type(strategy_id, direction)

    stop_str = f"${stop_loss:.2f}" if stop_loss is not None else "—"
    stars = "★" * strength + "☆" * (5 - strength)

    record = {
        "日期": date_str,
        "股票": event.symbol,
        "信号": _DIRECTION_CN.get(direction, direction),
        "信号类型": signal_type,
        "强度(1-5)": strength,
        "强度星级": stars,
        "触发原因": reason,
        "参考止损": stop_str,
    }

    # RS 列：策略侧算好的结构化数据优先；缺失时从 触发原因 回填
    rs_data = event.data.get("rs_data") or parse_rs_columns_from_reason(reason)
    for col in RS_COLUMN_NAMES:
        record[col] = rs_data.get(col, "")

    return record


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


def _print_table(records: list[dict]) -> None:
    """把信号以表格形式打印到控制台。"""
    # 分组：买入信号和卖出信号分开显示
    buy_records = [r for r in records if r["信号"] == "买入"]
    sell_records = [r for r in records if r["信号"] == "卖出"]

    print()
    print("=" * 75)
    print("信号报告")
    print("=" * 75)

    if buy_records:
        print(f"\n【买入信号】共 {len(buy_records)} 个")
        print(f"{'日期':<12} {'股票':<8} {'强度':<8} {'RS':<6} {'参考止损':<10} 触发原因")
        print("-" * 75)
        for r in sorted(buy_records, key=lambda x: -x["强度(1-5)"]):
            print(
                f"{r['日期']:<12} {r['股票']:<8} {r['强度星级']:<8} "
                f"{r.get('RS相对强度') or '—':<6} "
                f"{r['参考止损']:<10} {r['触发原因']}"
            )

    if sell_records:
        print(f"\n【出场信号】共 {len(sell_records)} 个")
        print(f"{'日期':<12} {'股票':<8} 原因")
        print("-" * 75)
        for r in sell_records:
            print(f"{r['日期']:<12} {r['股票']:<8} {r['触发原因']}")

    if not buy_records and not sell_records:
        print("\n今日无有效信号")

    print("=" * 75)
    print()


def _append_to_csv(records: list[dict], strategy_id: str = "v1_wizard") -> None:
    """把信号追加写入 CSV 文件（如果不存在则创建并写入表头）。

    迁移逻辑（所有策略）：
        - 若 signals.csv 已存在且 header 列数在 _OLD_SCHEMA_VERSIONS 中（6 或 7）
        - 自动重写整个文件：补齐缺失的 "信号类型" 列和 5 个 RS 列
          （RS 列从 触发原因 自由文本回填，老记录没有 RS-Rating 就留空）
        - 备份原文件为 signals.csv.bak.schema_v1 / .v2

    ⚠️ 迁移必须对**所有**策略生效。追加用的是全局 `_CSV_HEADERS`（12 列），
    旧文件不迁移就会「12 字段的行塞进 7 列表头的文件」→ 列错位 →
    读回来「强度(1-5)」= 信号类型的值 → int() 抛 ValueError，整轮扫描死掉。
    """
    strategy_dir = OUTPUT_DIR / strategy_id
    strategy_dir.mkdir(parents=True, exist_ok=True)
    signals_csv = strategy_dir / "signals.csv"

    # migration: 6-col / 7-col → 12-col header（不限策略）
    if signals_csv.exists():
        backup_path = None
        try:
            with open(signals_csv, "r", encoding="utf-8-sig", newline="") as f:
                header_row = next(csv.reader(f))
            schema_version = _OLD_SCHEMA_VERSIONS.get(len(header_row))
            if schema_version:
                # Backup + rewrite
                backup_path = signals_csv.with_suffix(f".csv.bak.schema_{schema_version}")
                signals_csv.rename(backup_path)
                # Read all old records
                with open(backup_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    old_records = list(reader)
                # Rewrite with new schema
                with open(signals_csv, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore")
                    writer.writeheader()
                    for old in old_records:
                        reason = old.get("触发原因", "")
                        direction = {
                            "买入": "buy", "卖出": "sell", "观望": "watch",
                        }.get(old.get("信号", ""), "buy")
                        new_row = {
                            "日期": old.get("日期", ""),
                            "股票": old.get("股票", ""),
                            "信号": old.get("信号", ""),
                            "信号类型": old.get("信号类型")
                            or _default_signal_type(strategy_id, direction),
                            "强度(1-5)": old.get("强度(1-5)", ""),
                            "触发原因": reason,
                            "参考止损": old.get("参考止损", ""),
                        }
                        # 老记录只能从 触发原因 回填 RS/SPY 等；RS-Rating 当时还没算
                        for col, val in parse_rs_columns_from_reason(reason).items():
                            new_row[col] = val
                        writer.writerow(new_row)
        except Exception as e:
            backup_path_str = backup_path or signals_csv.with_suffix(".csv.bak.schema_v1")
            print(f"[ERROR] CSV migration failed for {signals_csv}: {e}")
            print(f"        Original preserved at {backup_path_str}. Manual recovery required.")
            return

    # Normal append
    file_exists_after = signals_csv.exists()

    # 去重：按（日期，股票，信号类型）跳过已有记录，避免 crossback 等历史事件重复追加
    existing_keys: set[tuple] = set()
    if file_exists_after:
        try:
            with open(signals_csv, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames and "日期" in reader.fieldnames and "股票" in reader.fieldnames and "信号类型" in reader.fieldnames:
                    for row in reader:
                        existing_keys.add((row.get("日期"), row.get("股票"), row.get("信号类型")))
        except Exception:
            existing_keys = set()

    with open(signals_csv, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=_CSV_HEADERS,
            extrasaction="ignore",
        )
        if not file_exists_after:
            writer.writeheader()

        for r in records:
            key = (r["日期"], r["股票"], r.get("信号类型", V_EPS_V2_BUY_SIGNAL_TYPE))
            if key in existing_keys:
                continue
            writer.writerow({
                "日期": r["日期"],
                "股票": r["股票"],
                "信号": r["信号"],
                "信号类型": r.get("信号类型", V_EPS_V2_BUY_SIGNAL_TYPE),
                "强度(1-5)": r["强度(1-5)"],
                "触发原因": r["触发原因"],
                "参考止损": r["参考止损"],
                **{col: r.get(col, "") for col in RS_COLUMN_NAMES},
            })
            existing_keys.add(key)

    print(f"[输出] 信号已保存到 {signals_csv}")
