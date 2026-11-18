"""用户版日报生成器（tools/gen_user_report.py）的测试。

用户版日报是「手工核对每个数字」的重活 —— 19 只票 × 17 个字段。
本测试锁定三件事：

1. **量比/均线/突破必须按交易日算**（不是行数）—— 与策略口径一致，否则报告和信号对不上
2. **报告结构完整**：段1 信号表 / 段1.5 回踩 / 段2 字段说明 / 段3 看盘建议 / caveat / 风险提示
3. **数字必须是真的**：表里的量比、RS、板块排名、止损都要能从输入推出来
"""
import csv
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.stage import LABELS, StageResult  # noqa: E402
from tools.gen_user_report import _format_stage_cell, build_user_report, main  # noqa: E402

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


def _write_weekly_pkl(path: Path, weekly_closes: list[float], end: str = "2026-11-05") -> None:
    """把周收盘价展开成日线写盘 —— 阶段判定跑在周线上，fixture 必须是周线形状。

    参考实现（classical.py）要求至少 60 根周线，所以这里至少 60 周。
    """
    daily: list[float] = []
    for close in weekly_closes:
        daily.extend([close] * 5)
    dates = pd.date_range(end=end, periods=len(daily), freq="B", tz="UTC")
    pd.to_pickle(
        pd.DataFrame(
            {
                "open": daily,
                "high": [c * 1.01 for c in daily],
                "low": [c * 0.99 for c in daily],
                "close": daily,
                "volume": 100_000,
            },
            index=dates,
        ),
        path,
    )


def _stage2_weekly() -> list[float]:
    """45 周筑底 + 25 周主升 → Stage 2。"""
    return [50.0] * 45 + [50.0 * (1.02 ** (i + 1)) for i in range(25)]


def _stage4_weekly() -> list[float]:
    """70 周连续下跌 → Stage 4。"""
    return [300.0 * (0.97 ** (i + 1)) for i in range(70)]


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
        "今日概览", "段 1（**第一层**）", "段 1.5（**第二层**）", "段 2：", "段 3：",
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


def test_caveat_explains_layer2_history_rows_use_old_protocol(env):
    """段 1.5 的历史回踩行必须在 caveat 里说明它跟当前协议口径不同。

    第二层协议加了「当前仍站回 EMA10 上方」这一条 —— 一只票回踩当天成立、
    但之后又跌回 EMA10 下方，它就**不再是当前信号**了。
    历史行仍留在 signals.csv 里（那是当时的事实），若不说明口径，
    用户会把「已经跌回去的票」误当成今天的进场依据。
    """
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.append({
        "日期": "2026-11-03", "股票": "FALLEN", "信号": "买入", "信号类型": "crossback_buy",
        "强度(1-5)": "2", "参考止损": "$9.00",
        "触发原因": "[EMA10回踩] 上穿EMA10($10.00, +2.0%) + 量能1.2x均量",
        "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
        "板块排名": "273/664",
    })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    caveat_section = md.split("数据 caveat")[1]
    assert "EMA10" in caveat_section, "caveat 没提 EMA10 口径差异"
    assert "站回" in caveat_section, "caveat 没说明「当前仍站回 EMA10 上方」这条条件"


def test_crossback_on_session_day_is_todays_new_crossback(env):
    """回踩标的是**它实际发生的那根 K 线日期**，不是运行日。

    所以「今日新增回踩」不能按运行日过滤 —— 那样永远为空，
    今天才第一次检出的新回踩会被埋进「过去 7 天」段里看不见。
    判据应该是「发生在最近一个已完成交易日（数据日）」。
    """
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for date, symbol in (("2026-11-05", "TEST"), ("2026-11-03", "DEAD")):
        rows.append({
            "日期": date, "股票": symbol, "信号": "买入", "信号类型": "crossback_buy",
            "强度(1-5)": "4", "参考止损": "$16.33",
            "触发原因": "[EMA10回踩] 上穿EMA10($17.20, +1.5%) + 量能2.0x均量",
            "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
            "板块排名": "273/664",
        })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    # 段 1.5 的「今日新增」表里应该有数据日发生的回踩（数据日 = 2026-11-05）
    today_section = md.split("### 今日新增")[1].split("###")[0]
    assert "**TEST**" in today_section, f"数据日发生的回踩没进「今日新增」：\n{today_section}"
    assert "2026-11-05" in today_section, f"回踩日期没显示：\n{today_section}"
    assert "**DEAD**" not in today_section, f"更早的回踩被误当成今日新增：\n{today_section}"
    # 更早的回踩留在「过去 7 天」段里，且数据日那条不能重复出现
    history_section = md.split("### 过去 7 天 watchlist 回踩")[1].split("\n## ")[0]
    assert "DEAD" in history_section, f"历史回踩丢了：\n{history_section}"
    assert "TEST" not in history_section, "回踩同时出现在两段里，重复了"


def test_cli_prints_paste_block(env, capsys):
    """CLI 必须把「信号表 / 三条建议 / 大盘一行」直接打到 stdout。

    这是「每天只贴三样」承诺的落点 —— 只写 md 文件等于还要人工再抄一遍。
    """
    rc = main([
        "--date", env["date"],
        "--signals", str(env["signals_csv"]),
        "--watchlist", str(env["watchlist"]),
        "--cache-dir", str(env["cache_dir"]),
        "--universe", str(env["universe"]),
        "--out-dir", str(env["cache_dir"].parent / "out"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "信号表（今日" in out, f"没打印信号表：\n{out}"
    assert "最强组合" in out, f"没打印最强组合：\n{out}"
    assert "注意" in out, f"没打印注意事项：\n{out}"
    assert "大盘" in out, f"没打印大盘：\n{out}"
    assert "TEST" in out, f"信号表里没有当日信号：\n{out}"
    # 量比 ≥2x 必须带 ⚡ —— 与 md 段 1 的标记口径一致
    assert "2.0x ⚡" in out, f"放量没标 ⚡：\n{out}"


def _main_row(md: str) -> str:
    rows = [ln for ln in md.splitlines() if ln.startswith("| **2026-11-06** | **TEST**")]
    assert rows, "段 1 的表里没有今日信号行"
    return rows[0]


def test_stage_column_is_rendered_for_every_signal(env):
    """每条信号都要有一列 Weinstein 阶段 —— 回答「这条信号现在还在不在 Stage 2」。"""
    _write_weekly_pkl(env["cache_dir"] / "TEST.pkl", _stage2_weekly())
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "| 股票 | 阶段 |" in md, "段 1 表头缺「阶段」列"
    row = _main_row(md)
    assert re.search(r"Stage [1-4]", row), f"行里没有阶段：{row}"


def test_stage_column_shows_dash_when_history_is_too_short(env):
    """周线不足 60 根 → 阶段判不了，显示「—」而不是编一个阶段出来。"""
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = _main_row(md)
    assert "—" in row, f"历史不足应显示「—」：{row}"


def test_stage_is_computed_not_hardcoded(env):
    """筑底后主升的票 = Stage 2；阶段必须是算出来的，不是写死的。"""
    _write_weekly_pkl(env["cache_dir"] / "TEST.pkl", _stage2_weekly())

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = _main_row(md)
    assert "Stage 2" in row, f"上涨的票应是 Stage 2，实际：{row}"


def test_stage_column_is_in_paste_block(env, capsys):
    """可贴片段（每天只贴三样）也必须带阶段列 —— 那一列就是用户最想看的。"""
    rc = main([
        "--date", env["date"],
        "--signals", str(env["signals_csv"]),
        "--watchlist", str(env["watchlist"]),
        "--cache-dir", str(env["cache_dir"]),
        "--universe", str(env["universe"]),
        "--out-dir", str(env["cache_dir"].parent / "out"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "| 股票 | 阶段 |" in out, f"可贴片段缺阶段列：\n{out}"


def test_stage_field_is_documented(env):
    """段 2 字段说明必须解释「阶段」是什么（不然读者看不懂这一列）。"""
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "**阶段**" in md, "字段说明里没有「阶段」条目"


def test_stage_4_stock_is_called_out_in_attention(env):
    """已经跌成 Stage 4 的信号必须在「注意」里被点名 —— 阶段列不能只是装饰。"""
    _write_weekly_pkl(env["cache_dir"] / "TEST.pkl", _stage4_weekly())

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    attention = md.split("### ⚠️ **注意**")[1].split("\n### ")[0]
    assert "Stage 4" in attention, f"Stage 4 没被点名：\n{attention}"
    assert "TEST" in attention


def test_stage_warning_merges_into_existing_row(env):
    """同一只票既被别的规则点名、又已经 Stage 4 → 合并成一行，不能占两行。"""
    _write_weekly_pkl(env["cache_dir"] / "TEST.pkl", _stage4_weekly())

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    attention = md.split("### ⚠️ **注意**")[1].split("\n### ")[0]
    rows = [ln for ln in attention.splitlines() if ln.startswith("| **TEST**")]
    assert len(rows) == 1, f"TEST 应只有一行，实际 {len(rows)} 行：\n{attention}"
    assert "Stage 4" in rows[0]


def _sr(stage, substage=None, developing=False, confirmed_stage=None, confirmed_substage=None):
    """造一个阶段判定结果（不跑真实行情 —— 这一层只管怎么写进表格）。"""
    return StageResult(
        stage=stage, label=LABELS.get(stage, "—"), reason="测试用", substage=substage,
        developing=developing, confirmed_stage=confirmed_stage,
        confirmed_substage=confirmed_substage,
    )


def test_stage_cell_plain_when_week_is_complete():
    """周线已收完 → 就写阶段，不加任何修饰。"""
    assert _format_stage_cell(_sr(2, "2")) == "**Stage 2 上升期 · 2**"


def test_stage_cell_silent_when_developing_but_stage_unchanged():
    """发展中但阶段跟上一完整周一样 → 不刷「发展中」噪声（参考实现口径：确认读数才作数）。"""
    text = _format_stage_cell(_sr(2, "2", developing=True, confirmed_stage=2, confirmed_substage="2A"))
    assert "发展中" not in text
    assert "待定" not in text
    assert "Stage 2" in text


def test_stage_cell_flags_stage_flip_from_partial_week():
    """这一周把阶段翻掉了 → 必须标「待定」并写出上一完整周读数，否则就是误报。"""
    text = _format_stage_cell(_sr(1, "1A", developing=True, confirmed_stage=4, confirmed_substage="4B-"))
    assert "待定" in text
    assert "上周" in text
    assert "Stage 4" in text
    assert "下跌期" in text


def test_stage_cell_keeps_stage_3_and_4_marks():
    """Stage 3/4 是「该走了」，标记不能因为发展中判定而丢掉。"""
    assert "⚠" in _format_stage_cell(_sr(3, "3"))
    assert "🔴" in _format_stage_cell(_sr(4, "4"))
    assert _format_stage_cell(_sr(None)) == "—"


# --------------------------------------------------------------------------
# 回踩表：触发收盘 / 上穿EMA10 必须分清
# --------------------------------------------------------------------------
def _md_table(section: str) -> tuple[list[str], list[dict[str, str]]]:
    """把一段 Markdown 里的第一张表拆成 (表头, 行字典列表)。"""
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    if len(lines) < 2:
        return [], []
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for ln in lines[2:]:
        cells = [c.strip().replace("**", "") for c in ln.strip().strip("|").split("|")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return header, rows


def _append_crossback(env: dict, date: str, symbol: str = "TEST") -> None:
    """往 signals.csv 里塞一条 crossback 记录。"""
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.append({
        "日期": date, "股票": symbol, "信号": "买入", "信号类型": "crossback_buy",
        "强度(1-5)": "4", "参考止损": "$16.33",
        "触发原因": "[EMA10回踩] 上穿EMA10($17.20, +1.5%) + 量能2.0x均量",
        "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
        "板块排名": "273/664",
    })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)


def test_crossback_close_column_is_true_close_not_ema10(env):
    """「触发收盘」必须是回踩**当天**的真实收盘价。

    `上穿EMA10($17.20, +1.5%)` 里的 $17.20 是 **EMA10**，不是收盘价。
    fixture 里 11-04 收盘 17.45、11-05 收盘 17.75、EMA10 17.20 —— 三个数都不一样，
    报告必须把它们分到三个不同的列里（触发收盘 / 现价 / 上穿EMA10）。
    """
    _append_crossback(env, "2026-11-04")

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    section = md.split("### 过去 7 天 watchlist 回踩")[1].split("\n## ")[0]
    header, rows = _md_table(section)
    row = [r for r in rows if r.get("股票") == "TEST"]
    assert row, f"历史回踩表里没有 TEST：\n{section}"
    r = row[0]

    assert "触发收盘" in header, f"表头缺「触发收盘」：{header}"
    assert "上穿EMA10" in header, f"表头缺「上穿EMA10」：{header}"
    assert r["触发收盘"] == "$17.45", f"触发收盘应是当天真实收盘 17.45（17.20 是 EMA10）：{r}"
    assert r["上穿EMA10"] == "$17.20", f"上穿EMA10 列应是 EMA10 本身：{r}"
    assert r["现价"] == "$17.75", f"现价应是最新收盘 17.75：{r}"


def test_crossback_today_row_close_column(env):
    """今日新增回踩那张表同样要分清「触发收盘 / 上穿EMA10 / 现价」。"""
    _append_crossback(env, "2026-11-05")

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    section = md.split("### 今日新增")[1].split("###")[0]
    header, rows = _md_table(section)
    row = [r for r in rows if r.get("股票") == "TEST"]
    assert row, f"今日新增表里没有 TEST：\n{section}"
    r = row[0]
    assert r["触发收盘"] == "$17.75", f"触发收盘应是当天真实收盘 17.75：{r}"
    assert r["上穿EMA10"] == "$17.20", f"上穿EMA10 列应是 EMA10 本身：{r}"


def test_spy_volume_label_follows_volume_ratio(env):
    """量能标签要同时看**量比和方向** —— 缩量上涨写成「放量上涨」是报假数字。"""
    df = pd.read_pickle(env["cache_dir"] / "SPY.pkl")
    df.iloc[-1, df.columns.get_loc("close")] = 710.0     # 收涨
    df.iloc[-1, df.columns.get_loc("volume")] = 50_000   # 量比 0.05x → 缩量
    pd.to_pickle(df, env["cache_dir"] / "SPY.pkl")

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "缩量上涨" in md, f"缩量上涨没标出来：\n{md}"
    assert "放量上涨" not in md, f"缩量被标成放量上涨：\n{md}"



def test_crossback_close_falls_back_to_derived_value(env):
    """行情缓存里没有回踩那天的 bar → 用 EMA10×(1+涨幅) 反推，并标「≈」表示是估算值。

    「—」比估算值更没用：上穿涨幅本来就是用 (close−EMA10)/EMA10 算出来的，
    反推出来的收盘价精度只受涨幅四舍五入影响。
    """
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.append({
        "日期": "2026-11-05", "股票": "NOCACHE", "信号": "买入", "信号类型": "crossback_buy",
        "强度(1-5)": "4", "参考止损": "$16.33",
        "触发原因": "[EMA10回踩] 上穿EMA10($17.20, +1.5%) + 量能2.0x均量",
        "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
        "板块排名": "273/664",
    })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    section = md.split("### 今日新增")[1].split("###")[0]
    header, rows = _md_table(section)
    row = [r for r in rows if r.get("股票") == "NOCACHE"]
    assert row, f"今日新增表里没有 NOCACHE：\n{section}"
    r = row[0]
    # 17.20 × (1 + 1.5%) = 17.458
    assert r["触发收盘"] == "≈$17.46", f"取不到缓存时应给反推值并标 ≈：{r}"
    assert r["上穿EMA10"] == "$17.20", f"上穿EMA10 列应是 EMA10 本身：{r}"


def test_crossback_columns_are_documented(env):
    """段 1.5 的列必须有说明 —— 尤其「触发收盘」和「上穿EMA10」必须写清是两个不同的数。"""
    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    assert "**触发收盘**" in md, "字段说明里没有「触发收盘」条目"
    assert "**上穿EMA10**" in md, "字段说明里没有「上穿EMA10」条目"
    assert "不是收盘价" in md, "说明里没点明 EMA10 不是收盘价"


def test_layer2_tables_carry_full_rs_columns(env):
    """段 1.5 的两张回踩表必须带齐 RS 四列 + 板块 + 板块排名。

    第二层信号同样是「可执行的进场依据」，缺 RS 就没法判断这只票
    是不是市场里的强者；缺板块排名就看不出它在板块里排第几。
    signals.csv 里这些字段本来就有，是表格没渲染出来。
    """
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # 数据日（2026-11-05）那条进「今日新增」，更早那条进「过去 7 天」
    for date in ("2026-11-05", "2026-11-03"):
        rows.append({
            "日期": date, "股票": "TEST", "信号": "买入", "信号类型": "crossback_buy",
            "强度(1-5)": "4", "参考止损": "$16.33",
            "触发原因": "[EMA10回踩] 上穿EMA10($17.20, +1.5%) + 量能2.0x均量",
            "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
            "板块排名": "273/664",
        })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    for heading in ("### 今日新增（回踩日", "### 过去 7 天 watchlist 回踩"):
        assert heading in md, f"报告缺区段：{heading}"
        section = md.split(heading)[1].split("\n## ")[0]
        for col in ("RS", "RS/SPY", "10W", "10D", "板块", "板块排名"):
            assert f"| {col}" in section or f"| **{col}**" in section, \
                f"{heading} 缺「{col}」列：\n{section}"
        # RS 数值必须真的出现在表里，不能是空占位
        assert "53" in section, f"{heading} 里 RS Rating 没渲染：\n{section}"
        assert "273/664" in section, f"{heading} 里板块排名没渲染：\n{section}"


def test_layer2_glossary_documents_the_new_columns(env):
    """段 1.5 的字段说明必须覆盖新加的 RS 列。"""
    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.append({
        "日期": "2026-11-05", "股票": "TEST", "信号": "买入", "信号类型": "crossback_buy",
        "强度(1-5)": "4", "参考止损": "$16.33",
        "触发原因": "[EMA10回踩] 上穿EMA10($17.20, +1.5%) + 量能2.0x均量",
        "RS相对强度": "53", "RS/SPY": "0.27", "RS-10W": "1.13", "RS-10D": "1.07",
        "板块排名": "273/664",
    })
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    glossary = md.split("段 1.5 字段说明")[1].split("\n## ")[0]
    for name in ("RS", "RS/SPY", "10W", "10D", "板块排名"):
        assert f"**{name}**" in glossary, f"段 1.5 字段说明缺「{name}」：\n{glossary}"


# --------------------------------------------------------------------------
# EPS 小额精度：$0.0025 不能被抹成 $0.00
# --------------------------------------------------------------------------
def test_eps_money_keeps_sub_cent_precision():
    """小于 1 分的 EPS 要保留有效位 —— 抹成 $0.00 会让「EPS 为正」判不出来。"""
    from tools.gen_user_report import _eps_money
    assert _eps_money(0.0025) == "$0.0025"
    assert _eps_money(1.0) == "$1.00"
    assert _eps_money(-0.13) == "$-0.13"
    assert _eps_money(0.0) == "$0.00"
    assert _eps_money(None) == "—"


def test_tiny_eps_is_recovered_from_cache_and_gets_standard_position(env):
    """触发原因里的 EPS 被抹成 $0.00 时，报告要能自己修回来。

    2026-11-17 实测：KVYO 最新季度 EPS = $0.0025（从 −$0.0298 转正），
    触发原因写成「最新季度EPS$0.00」→ 仓位档位规则 `(eps or 0) > 0` 判成
    「不是正数」→ 即使 RS ≥85 也拿不到标准仓。
    """
    import pickle
    quarters = [
        ("2026-Q3", 0.0025), ("2026-Q2", -0.0298), ("2026-Q1", 0.0296),
        ("2025-Q4", 0.0232), ("2025-Q3", -0.0014), ("2025-Q2", -0.0852),
        ("2025-Q1", -0.0514), ("2024-Q4", -0.0996),
    ]
    (env["cache_dir"] / "eps_TEST.pkl").write_bytes(pickle.dumps({"eps_history": quarters}))

    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["触发原因"] = rows[0]["触发原因"].replace("最新季度EPS$2.10", "最新季度EPS$0.00")
    rows[0]["RS相对强度"] = "90"
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    row = _main_row(md)
    assert "$0.0025" in row, f"小额正 EPS 被抹成 0：{row}"
    assert not re.search(r"\*\*\$0\.00\*\*", row), f"小额正 EPS 被抹成 0：{row}"

    grade_rows = [ln for ln in md.splitlines() if ln.startswith("| ⭐⭐⭐ |")]
    assert any("TEST" in ln for ln in grade_rows), \
        f"RS 90 + EPS 为正 → 应给标准仓（⭐⭐⭐）：\n{md}"


def test_historical_rows_keep_their_own_eps_text(env):
    """历史行不拿今天的 EPS 去覆盖 —— 那是拿今天的数据伪造归档。

    只有报告当天（date_str）的行才允许回退到本地 EPS 缓存。
    """
    import pickle
    quarters = [("2026-Q3", 0.0025)] + [(f"2026-Q{i}", 0.1) for i in (2, 1)]
    (env["cache_dir"] / "eps_TEST.pkl").write_bytes(pickle.dumps({"eps_history": quarters}))

    with open(env["signals_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["日期"] = "2026-11-02"      # 历史行
    rows[0]["触发原因"] = rows[0]["触发原因"].replace("最新季度EPS$2.10", "最新季度EPS$0.00")
    with open(env["signals_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)

    md = build_user_report(
        date_str=env["date"], signals_csv=env["signals_csv"], watchlist_path=env["watchlist"],
        cache_dir=env["cache_dir"], universe_path=env["universe"],
    )
    history = md.split("过去 7 天")[1] if "过去 7 天" in md else md
    assert "$0.0025" not in history, f"历史行被今天的 EPS 覆盖了：\n{history}"


# ---------------------------------------------------------------------------
# 第二层「今天 0 个回踩」时，必须说清监控票卡在哪个协议条件上
# ---------------------------------------------------------------------------
@pytest.fixture
def blocked_env(tmp_path: Path) -> dict:
    """一只「上穿过 EMA10 但被协议挡住」的监控票（量能不足 → 条件③）。

    前 20 天 10 → 20（EMA10 暖起来），之后趴在 10（跌破 EMA10），
    最后一根跳到 22（> EMA10）但量能只有 0.5×20 日均量 → 上穿事件成立、
    量能门槛不成立 → 条件③ 把这只票挡住。
    """
    cache = tmp_path / "cache"
    cache.mkdir()

    n = 200
    closes = [10 + i * (10 / 19) for i in range(20)] + [10.0] * (n - 21) + [22.0]
    vols = [1_000_000] * n
    vols[-1] = 500_000
    dates = pd.date_range(end="2026-11-05", periods=n, freq="B", tz="UTC")
    pd.to_pickle(
        pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": vols},
            index=dates,
        ),
        cache / "BLOCKED.pkl",
    )
    pd.to_pickle(_make_bars(200, close=700.0, volume=1_000_000), cache / "SPY.pkl")

    csv_path = tmp_path / "signals.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADER).writeheader()

    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "BLOCKED": {
            "symbol": "BLOCKED", "signal_date": "2026-08-01", "signal_price": 10.0,
            "stage2_stop": 9.2, "added_at": "2026-08-01T01:25:25+00:00",
            "expires_at": "2026-12-01T01:25:25+00:00",
            "crossback_count": 0, "last_crossback_date": None,
        },
    }), encoding="utf-8")

    uni_path = tmp_path / "rs_universe.json"
    uni_path.write_text(json.dumps({
        "symbols": {"BLOCKED": {"name": "Blocked Inc", "gics_sector": "Information Technology"}}
    }), encoding="utf-8")

    return {
        "signals_csv": csv_path, "watchlist": wl_path, "cache_dir": cache,
        "universe": uni_path, "date": "2026-11-06",
    }


def test_report_lists_crossback_candidates_blocked_by_protocol(blocked_env):
    """第二层报 0 个时，必须逐条说出监控票卡在哪个条件上。

    只写「0 个回踩」等于什么都没说 —— 用户分不清「市场没机会」和
    「协议把机会挡掉了」，这两种情况的行动完全不同。
    """
    md = build_user_report(
        date_str=blocked_env["date"], signals_csv=blocked_env["signals_csv"],
        watchlist_path=blocked_env["watchlist"], cache_dir=blocked_env["cache_dir"],
        universe_path=blocked_env["universe"],
    )
    assert "被协议挡掉的票" in md, "报告里没有「被协议挡掉的候选」这一段"
    section = md.split("被协议挡掉的票", 1)[1]
    row = [ln for ln in section.splitlines() if ln.startswith("| **BLOCKED**")]
    assert row, f"被挡掉的候选没进表：\n{section}"
    assert "③" in row[0], f"没写清卡在哪个条件：{row[0]}"
    assert "0.50x" in row[0], f"没写清上穿当天的量比：{row[0]}"
    assert "2026-11-05" in row[0], f"没写清上穿日：{row[0]}"


def test_report_does_not_list_passing_candidates_as_blocked(blocked_env):
    """四条件全过的票是真信号，不该出现在「被挡掉」表里。"""
    df = pd.read_pickle(blocked_env["cache_dir"] / "BLOCKED.pkl")
    df.iloc[-1, df.columns.get_loc("volume")] = 3_000_000   # 量比 3.0x → 全过
    pd.to_pickle(df, blocked_env["cache_dir"] / "BLOCKED.pkl")

    md = build_user_report(
        date_str=blocked_env["date"], signals_csv=blocked_env["signals_csv"],
        watchlist_path=blocked_env["watchlist"], cache_dir=blocked_env["cache_dir"],
        universe_path=blocked_env["universe"],
    )
    section = md.split("被协议挡掉的票", 1)[1]
    row = [ln for ln in section.splitlines() if ln.startswith("| **BLOCKED**")]
    assert not row, f"四条件全过的票不该被列为「被挡掉」：{row}"


def test_report_flags_volume_ratio_broken_by_source_switch(blocked_env):
    """数据源换挡会把 20 日均量抬高，量比被压成 0.0x —— 报告必须标出来。

    行情 pkl 在 2026-10-23 前后换过源：合并量 bar（00:00 UTC，量是全市场）→
    IEX bar（05:00 UTC，量小两个数量级）。同一天两根 bar 的情况 dedup 能处理，
    跨日期换源处理不了 → 20 日均量混了两个量纲 → 量比失真。
    """
    n = 200
    closes = [10 + i * (10 / 19) for i in range(20)] + [10.0] * (n - 21) + [22.0]
    vols = [100_000.0] * n
    for i in range(180):                       # 前 180 根是「合并量」口径
        vols[i] = 5_000_000.0
    vols[-1] = 150_000.0                       # 上穿当天（IEX 口径）
    idx = pd.date_range(end="2026-11-05", periods=n, freq="B", tz="UTC")
    idx = pd.DatetimeIndex(
        [t.replace(hour=0) for t in idx[:180]] + [t.replace(hour=5) for t in idx[180:]]
    )
    pd.to_pickle(
        pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": vols},
            index=idx,
        ),
        blocked_env["cache_dir"] / "BLOCKED.pkl",
    )

    md = build_user_report(
        date_str=blocked_env["date"], signals_csv=blocked_env["signals_csv"],
        watchlist_path=blocked_env["watchlist"], cache_dir=blocked_env["cache_dir"],
        universe_path=blocked_env["universe"],
    )
    section = md.split("被协议挡掉的票", 1)[1]
    row = [ln for ln in section.splitlines() if ln.startswith("| **BLOCKED**")]
    assert row, f"被挡掉的候选没进表：\n{section}"
    assert "量纲" in row[0], f"量比失真没标出来：{row[0]}"
    assert "1.50x" in row[0], f"没给出同源口径的真实量比：{row[0]}"


def test_paste_block_explains_why_no_crossback(blocked_env):
    """日报贴片段落里也要有「今天为什么没有回踩」的答案。

    贴片段落是每天直接贴给用户看的东西 —— 只报「0 个回踩」等于让人自己去猜。
    """
    from datetime import date as _date

    from tools.gen_user_report import _collect, build_paste_block

    data = _collect(
        blocked_env["date"], blocked_env["signals_csv"], blocked_env["watchlist"],
        blocked_env["cache_dir"], blocked_env["universe"], 7,
        _date.fromisoformat(blocked_env["date"]),
    )
    block = build_paste_block(data)
    assert "BLOCKED" in block, f"贴片段落里没有回踩候选的说明：\n{block}"
    assert "③" in block, f"贴片段落里没写清卡在哪个条件：\n{block}"
