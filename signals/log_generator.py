"""
signals/log_generator.py — v_eps_v2 信号日志生成器

每次 live test 后自动生成/更新 SIGNALS_LOG.md，
按日期记录 v_eps_v2 的买入和卖出信号，方便复盘和业绩复核。
"""

from datetime import datetime
from pathlib import Path
import re

OUTPUT_DIR = Path(__file__).parent.parent / "output"
SIGNALS_LOG_FILE = OUTPUT_DIR / "SIGNALS_LOG.md"

# 策略 ID
ALL_STRATEGIES = ["v_eps_v2"]

# 策略显示名称
STRATEGY_NAMES = {
    "v_eps_v2": "v_eps_v2",
}


def load_signals_from_csv(strategy_id: str, date_str: str) -> tuple[list[dict], list[dict]]:
    """
    从策略的 signals.csv 加载指定日期的买入和卖出信号。

    返回: (buy_signals, sell_signals)
    """
    csv_path = OUTPUT_DIR / strategy_id / "signals.csv"
    if not csv_path.exists():
        return [], []

    buy_signals = []
    sell_signals = []

    try:
        import csv
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date = row.get("date", "")[:10]
                if date != date_str:
                    continue
                direction = row.get("direction", "").strip()
                if direction == "buy":
                    buy_signals.append(row)
                elif direction in ("sell", "sell_partial"):
                    sell_signals.append(row)
    except Exception:
        pass

    return buy_signals, sell_signals


def extract_breakout_info(reason: str) -> tuple[str, str]:
    """从 reason 字符串中提取突破幅度和放量倍数"""
    breakout_pct = "-"
    volume_ratio = "-"

    if "突破" in reason or "涨幅" in reason:
        m = re.search(r"([+\-]?\d+\.?\d*)%", reason)
        if m:
            breakout_pct = m.group(1) + "%"

    if "倍" in reason or "x" in reason.lower():
        m = re.search(r"(\d+\.?\d*)\s*[倍xX]", reason)
        if m:
            volume_ratio = m.group(1) + "x"

    return breakout_pct, volume_ratio


def generate_daily_section(
    date_str: str,
    all_buy_signals: dict[str, list],
    all_sell_signals: dict[str, list],
) -> str:
    """生成某一天的信号汇总部分"""

    lines = []
    lines.append(f"## {date_str}")
    lines.append("")

    # 收集所有买入信号的股票
    buy_symbols = set()
    for signals in all_buy_signals.values():
        for sig in signals:
            buy_symbols.add(sig["symbol"])

    # 收集所有卖出信号的股票
    sell_symbols = set()
    for signals in all_sell_signals.values():
        for sig in signals:
            sell_symbols.add(sig["symbol"])

    # ========== 买入信号汇总表 ==========
    if buy_symbols:
        lines.append("### 买入信号汇总")
        lines.append("")
        header = "| 股票 | " + " | ".join([STRATEGY_NAMES[s] for s in ALL_STRATEGIES]) + " | 综合评分 |"
        lines.append(header)
        separator = "|------|" + "|".join(["----"] * len(ALL_STRATEGIES)) + "|----|"
        lines.append(separator)

        for symbol in sorted(buy_symbols):
            row = f"| **{symbol}** |"
            count = 0
            for strategy in ALL_STRATEGIES:
                signals = all_buy_signals.get(strategy, [])
                matched = [s for s in signals if s["symbol"] == symbol]
                if matched:
                    stars = matched[0]["strength"]
                    row += f" {stars} |"
                    count += 1
                else:
                    row += " - |"
            row += f" {count}策略 |"
            lines.append(row)

        lines.append("")

    # ========== 买入信号详情 ==========
    if buy_symbols:
        lines.append("### 买入信号详情")
        lines.append("")

        for symbol in sorted(buy_symbols):
            strategies_with_signal = []
            for strategy in ALL_STRATEGIES:
                signals = all_buy_signals.get(strategy, [])
                matched = [s for s in signals if s["symbol"] == symbol]
                if matched:
                    sig = matched[0]
                    breakout, vol = extract_breakout_info(sig["reason"])
                    strategies_with_signal.append({
                        "strategy": strategy,
                        "strength": sig["strength"],
                        "breakout": breakout,
                        "volume": vol,
                        "stop_loss": sig["stop_loss"],
                    })

            if strategies_with_signal:
                lines.append(f"#### {symbol}")
                lines.append("")
                lines.append("| 策略 | 强度 | 突破幅度 | 放量倍数 | 参考止损 |")
                lines.append("|------|------|---------|---------|---------|")
                for s in strategies_with_signal:
                    lines.append(f"| {STRATEGY_NAMES[s['strategy']]} | {s['strength']} | {s['breakout']} | {s['volume']} | ${s['stop_loss']} |")
                lines.append("")

    # ========== 卖出信号汇总表 ==========
    if sell_symbols:
        lines.append("### 卖出信号汇总")
        lines.append("")

        header = "| 股票 | " + " | ".join([STRATEGY_NAMES[s] for s in ALL_STRATEGIES]) + " | 原因 |"
        lines.append(header)
        separator = "|------|" + "|".join(["----"] * len(ALL_STRATEGIES)) + "|----|"
        lines.append(separator)

        for symbol in sorted(sell_symbols):
            row = f"| **{symbol}** |"
            reasons = []
            for strategy in ALL_STRATEGIES:
                signals = all_sell_signals.get(strategy, [])
                matched = [s for s in signals if s["symbol"] == symbol]
                if matched:
                    row += " 卖 |"
                    if matched[0]["reason"] not in reasons:
                        reasons.append(matched[0]["reason"])
                else:
                    row += " - |"
            reason_short = reasons[0] if reasons else ""
            if "止损" in reason_short and "跌幅" in reason_short:
                m = re.search(r"跌幅([-\d.]+%)", reason_short)
                if m:
                    reason_short = f"止损({m.group(1)})"
                else:
                    reason_short = "止损"
            elif "追踪止盈" in reason_short:
                reason_short = "追踪止盈"
            elif "时间止损" in reason_short:
                reason_short = "时间止损"
            row += f" {reason_short} |"
            lines.append(row)

        lines.append("")

    # ========== 卖出信号详情 ==========
    if sell_symbols:
        lines.append("### 卖出信号详情")
        lines.append("")
        lines.append("| 股票 | 策略 | 出场原因 | 入场价 | 当前价 |")
        lines.append("|------|------|---------|-------|-------|")

        for symbol in sorted(sell_symbols):
            for strategy in ALL_STRATEGIES:
                signals = all_sell_signals.get(strategy, [])
                matched = [s for s in signals if s["symbol"] == symbol]
                if matched:
                    sig = matched[0]
                    reason = sig["reason"][:50] + "..." if len(sig["reason"]) > 50 else sig["reason"]
                    lines.append(f"| {symbol} | {STRATEGY_NAMES[strategy]} | {reason} | - | - |")

        lines.append("")

    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def update_signals_log(date_str: str | None = None) -> None:
    """
    更新信号汇总日志。

    参数:
        date_str: 日期字符串，默认使用今天
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 加载所有策略的信号
    all_buy_signals: dict[str, list] = {}
    all_sell_signals: dict[str, list] = {}

    for strategy in ALL_STRATEGIES:
        buy, sell = load_signals_from_csv(strategy, date_str)
        if buy:
            all_buy_signals[strategy] = buy
        if sell:
            all_sell_signals[strategy] = sell

    # 如果没有任何信号，跳过
    if not all_buy_signals and not all_sell_signals:
        print(f"[信号日志] {date_str} 无信号，跳过更新")
        return

    # 生成新的一天部分
    new_section = generate_daily_section(date_str, all_buy_signals, all_sell_signals)

    # 读取现有文件
    if SIGNALS_LOG_FILE.exists():
        with open(SIGNALS_LOG_FILE, "r", encoding="utf-8") as f:
            existing_content = f.read()
    else:
        existing_content = ""

    # 检查该日期是否已存在
    if f"## {date_str}" in existing_content:
        pattern = rf"## {date_str}\n.*?(?=\n## |\n\*文件生成|$)"
        existing_content = re.sub(pattern, new_section.rstrip("\n") + "\n", existing_content, flags=re.DOTALL)
        print(f"[信号日志] 更新 {date_str} 的信号记录")
    else:
        if "## " in existing_content:
            first_section_pos = existing_content.find("## ")
            existing_content = (
                existing_content[:first_section_pos] +
                new_section + "\n" +
                existing_content[first_section_pos:]
            )
        else:
            existing_content = existing_content.rstrip() + "\n\n" + new_section
        print(f"[信号日志] 添加 {date_str} 的信号记录")

    # 更新文件头部的生成时间
    header = f"""# 信号汇总日志（v_eps_v2）

> 自动生成，每次 live test 后更新
> 用于复盘和业绩复核
> 最后更新：{datetime.now().strftime("%Y-%m-%d %H:%M")}

---

"""
    # 移除旧头部
    if existing_content.startswith("# 信号汇总日志"):
        match = re.search(r"^---\n+", existing_content, re.MULTILINE)
        if match:
            existing_content = existing_content[match.end():]

    # 写入文件
    with open(SIGNALS_LOG_FILE, "w", encoding="utf-8") as f:
        f.write(header + existing_content)

    print(f"[信号日志] 已保存到 {SIGNALS_LOG_FILE}")


if __name__ == "__main__":
    update_signals_log()
