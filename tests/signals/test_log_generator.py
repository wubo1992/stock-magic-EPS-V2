"""SIGNALS_LOG.md 必须真的被写进去（2026-10-30 实测：静默失效 7 个月）。

现场：`output/SIGNALS_LOG.md` 最后更新停在 **2026-04-01**，之后每次 live run
都打印「[信号日志] 2026-10-30 无信号，跳过更新」—— 其实那天有信号。

根因：`load_signals_from_csv()` 按**英文列名**取值
（`row.get("date")` / `row.get("direction")`），而 signals.csv 的列名是
**中文**（日期 / 信号）→ 取到的永远是空串 → 一条都匹配不上；
再加上 `except Exception: pass` 吞掉一切，日志里看不出任何异常。

修法：按中文列名取值 + 映射成生成器要的英文 key（date/symbol/direction/…）。
"""
import csv
from pathlib import Path

import pytest

from signals import log_generator as lg

HEADER = [
    "日期", "股票", "信号", "信号类型", "强度(1-5)", "触发原因", "参考止损",
    "RS相对强度", "RS/SPY", "RS-10W", "RS-10D", "板块排名",
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _row(date="2026-10-30", symbol="DFIN", direction="买入"):
    return {
        "日期": date, "股票": symbol, "信号": direction, "信号类型": "v_eps_v2_buy",
        "强度(1-5)": 3, "触发原因": "[EPS] EPS+$1.44 | [突破] +8.5% | [量能] 4.4x",
        "参考止损": "$55.95", "RS相对强度": "74", "RS/SPY": "1.03",
        "RS-10W": "1.14", "RS-10D": "1.18", "板块排名": "314/994",
    }


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(lg, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(lg, "SIGNALS_LOG_FILE", tmp_path / "SIGNALS_LOG.md")
    return tmp_path


def test_load_signals_from_csv_reads_chinese_columns(log_env):
    """中文列名必须能读出来（原来读的是英文列名 → 永远读不到）。"""
    csv_path = log_env / "v_eps_v2" / "signals.csv"
    _write_csv(csv_path, [_row()])

    buy, sell = lg.load_signals_from_csv("v_eps_v2", "2026-10-30")

    assert len(buy) == 1
    assert buy[0]["symbol"] == "DFIN"
    assert buy[0]["direction"] == "buy"
    assert buy[0]["reason"].startswith("[EPS]")
    assert buy[0]["stop_loss"] == "55.95"   # 不带 $，生成器会自己加
    assert not sell


def test_load_signals_from_csv_ignores_other_dates(log_env):
    csv_path = log_env / "v_eps_v2" / "signals.csv"
    _write_csv(csv_path, [_row(date="2026-10-29", symbol="LUNG")])

    buy, _ = lg.load_signals_from_csv("v_eps_v2", "2026-10-30")
    assert buy == []


def test_update_signals_log_writes_today_section(log_env):
    """有信号的日子，日志文件里必须出现这一段。"""
    csv_path = log_env / "v_eps_v2" / "signals.csv"
    _write_csv(csv_path, [_row()])

    lg.update_signals_log("2026-10-30")

    content = lg.SIGNALS_LOG_FILE.read_text(encoding="utf-8")
    assert "## 2026-10-30" in content
    assert "DFIN" in content
    assert "$$" not in content, "止损前面不要出现两个 $"


def test_update_signals_log_keeps_a_single_header(log_env):
    """旧文件的表头是「# 全策略信号汇总日志」—— 不能叠成两个表头。"""
    csv_path = log_env / "v_eps_v2" / "signals.csv"
    _write_csv(csv_path, [_row()])
    lg.SIGNALS_LOG_FILE.write_text(
        "# 全策略信号汇总日志\n\n> 最后更新：2026-04-01 14:56\n\n---\n\n"
        "## 2026-04-01\n\n### 买入信号汇总\n\n旧内容\n",
        encoding="utf-8",
    )

    lg.update_signals_log("2026-10-30")

    content = lg.SIGNALS_LOG_FILE.read_text(encoding="utf-8")
    assert content.count("信号汇总日志") == 1, "表头不能叠成两个"
    assert "## 2026-04-01" in content, "旧内容不能丢"
    assert "## 2026-10-30" in content
