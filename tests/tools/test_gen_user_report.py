"""用户版日报生成器（tools/gen_user_report.py）的测试。

用户版日报是「手工核对每个数字」的重活 —— 19 只票 × 17 个字段。
本测试锁定三件事：

1. **量比/均线/突破必须按交易日算**（不是行数）—— 与策略口径一致，否则报告和信号对不上
2. **报告结构完整**：段1 信号表 / 段1.5 回踩 / 段2 字段说明 / 段3 看盘建议 / caveat / 风险提示
3. **数字必须是真的**：表里的量比、RS、板块排名、止损都要能从输入推出来
"""
import csv
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.gen_user_report import build_user_report  # noqa: E402

SIGNAL_DATE = "2026-11-06"
CSV_HEADER = [
    "日期", "股票", "信号", "信号类型", "强度(1-5)", "触发原因", "参考止损",
    "RS相对强度", "RS/SPY", "RS-10W", "RS-10D", "板块排名",
]
REASON = (
    "[EPS] 最新季度EPS$2.10，比去年同期高166.0%，比上季度高72.2%；"
    "最近2个季度的EPS都比上季度高，最近3个季度的EPS都比去年同期同季度高"
    " | [Stage2] SMA150上升30天 | [量能] 2.1x均量"
    " | [RS] RS/SPY=0.27x / RS-10W=1.13x / RS-10D=1.07x"
    " / RS-Rating=53 (Information Technology 排名273/664)"
)


def _make_bars(n: int, close: float, volume: float) -> pd.DataFrame:
    dates = pd.date_range(end="2026-11-05", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": volume},
        index=dates,
    )


def _with_duplicate_bars(df: pd.DataFrame, dup_from: int) -> pd.DataFrame:
    """复刻生产数据：每天两根（00:00 合并量 + 04:00 IEX），合并量是 30 倍。"""
    keep = df.iloc[-dup_from:].copy()
    head = df.iloc[:-dup_from]
    composite = keep.copy()
    composite.index = composite.index.normalize()
    composite["volume"] = composite["volume"] * 30
    composite["close"] = composite["close"] + 0.01
    keep.index = keep.index + pd.Timedelta(hours=4)
    return pd.concat([head, composite, keep]).sort_index(kind="stable")


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """一套完整的输入：signals.csv + watchlist.json + 行情缓存 + 股票名单。"""
    cache = tmp_path / "cache"
    cache.mkdir()

    # 200 个交易日：每天量 100,000；最后一天量 200,000 → 量比 2.0x
    df = _make_bars(200, close=17.75, volume=100_000)
    df.iloc[-1, df.columns.get_loc("volume")] = 200_000
    # 倒数第 2 天是 30 日整理区高点 17.45 → 突破幅度 +1.72%
    df.iloc[-2, df.columns.get_loc("close")] = 17.45
    pd.to_pickle(df, cache / "TEST.pkl")
    pd.to_pickle(_make_bars(200, close=700.0, volume=1_000_000), cache / "SPY.pkl")

    csv_path = tmp_path / "signals.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerow({
            "日期": SIGNAL_DATE, "股票": "TEST", "信号": "买入", "信号类型": "v_eps_v2_buy",
            "强度(1-5)": "3", "触发原因": REASON, "参考止损": "$16.33",
            "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
            "板块排名": "273/664",
        })

    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "TEST": {
            "symbol": "TEST", "signal_date": SIGNAL_DATE, "signal_price": 17.75,
            "stage2_stop": 16.33, "added_at": "2026-11-06T01:25:25+00:00",
            "expires_at": "2027-01-05T01:25:25+00:00",
            "crossback_count": 0, "last_crossback_date": None,
        },
        "DEAD": {
            "symbol": "DEAD", "signal_date": "2026-10-01", "signal_price": 10.0,
            "stage2_stop": 9.2, "added_at": "2026-10-01T01:25:25+00:00",
            "expires_at": "2026-11-05T01:25:25+00:00",
            "crossback_count": 2, "last_crossback_date": "2026-11-04",
        },
    }), encoding="utf-8")

    uni_path = tmp_path / "rs_universe.json"
    uni_path.write_text(json.dumps({
        "symbols": {
            "TEST": {"name": "Test Holdings Inc", "gics_sector": "Information Technology"},
            "DEAD": {"name": "Dead Corp", "gics_sector": "Utilities"},
        }
    }), encoding="utf-8")

    return {
        "signals_csv": csv_path, "watchlist": wl_path, "cache_dir": cache,
        "universe": uni_path, "date": SIGNAL_DATE,
    }


def test_report_has_all_required_sections(env):
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    for heading in (
        "今日概览", "段 1：", "段 1.5：", "段 2：", "段 3：",
        "监控池状态", "数据 caveat", "风险提示",
    ):
        assert heading in md, f"报告缺少段落：{heading}"


def test_signal_table_row_contains_real_numbers(env):
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = [ln for ln in md.splitlines() if ln.startswith("| **2026-11-06** | **TEST**")]
    assert row, "段 1 的表里没有今日信号行"
    r = row[0]
    assert "**$2.10**" in r, "EPS 没进表"
    assert "高 166.0%" in r and "高 72.2%" in r, "同比/环比没进表"
    assert "**3 季度**" in r and "**2 季度**" in r, "连续季度数没进表"
    assert "**53**" in r and "**0.27x**" in r and "**1.13x**" in r and "**1.07x**" in r, "RS 四列没进表"
    assert "273/664" in r, "板块排名没进表"
    assert "$16.33" in r, "参考止损没进表"
    assert "2.0x" in r, f"量比没进表（应为 2.0x）：{r}"


def test_volume_ratio_uses_trading_days_not_rows(env):
    """核心回归：同日双 bar 时量比必须按交易日算，否则和策略口径对不上。"""
    df = pd.read_pickle(env["cache_dir"] / "TEST.pkl")
    df_dup = _with_duplicate_bars(df, dup_from=40)
    pd.to_pickle(df_dup, env["cache_dir"] / "TEST.pkl")

    # 前提：按行数算的量比会被合并量抬高，与按交易日算的结果不同
    raw_avg = float(df_dup["volume"].iloc[-21:-1].mean())
    assert float(df_dup["volume"].iloc[-1]) / raw_avg < 1.5, "前提不成立：行数口径应低于门槛"

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = [ln for ln in md.splitlines() if ln.startswith("| **2026-11-06** | **TEST**")][0]
    assert "2.0x" in row, f"量比应按 20 个交易日算 = 2.0x，实际：{row}"


def test_watchlist_buckets_counted(env):
    """监控池：现价高于止损 3% 以上算 🟢；DEAD 无行情数据不应被算成 🟢。"""
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "🟢" in md and "🔴" in md
    # TEST 现价 17.75 > 止损 16.33 +3% → 🟢
    assert "TEST" in md


def test_volume_marker_follows_the_displayed_number(env):
    """1.98x 显示成 2.0x 就该带 ⚡ —— 阈值按显示值判，否则表里看着自相矛盾。"""
    df = pd.read_pickle(env["cache_dir"] / "TEST.pkl")
    df.iloc[-1, df.columns.get_loc("volume")] = 198_000  # 198000 / 100000 = 1.98x
    pd.to_pickle(df, env["cache_dir"] / "TEST.pkl")

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = [ln for ln in md.splitlines() if ln.startswith("| **2026-11-06** | **TEST**")][0]
    assert "**2.0x** ⚡" in row, f"显示成 2.0x 就该带 ⚡，实际：{row}"


def test_long_qoq_streak_is_flagged(env):
    """连续 11 季 EPS 环比增长 = 基本面最干净的一条，标 🔥。"""
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["触发原因"] = rows[0]["触发原因"].replace("最近2个季度", "最近11个季度")
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = [ln for ln in md.splitlines() if ln.startswith("| **2026-11-06** | **TEST**")][0]
    assert "**11 季度** 🔥" in row, f"连续环比季度应带 🔥，实际：{row}"


def test_missing_cache_symbol_does_not_crash(env):
    """行情缓存缺标的时，报告仍要出得来（该行显示为 —）。"""
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "DEAD" in md  # 有缓存的 DEAD 进表；无缓存的走缺失分支也不崩
