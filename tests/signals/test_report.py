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


def test_generate_crossback_report(tmp_path):
    """generate_crossback_report should create a markdown file at the expected path."""
    from pathlib import Path
    from datetime import datetime, timezone
    from signals.report import generate_crossback_report

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
        assert "CRDL" in content
    finally:
        if original is not None:
            report_mod.OUTPUT_DIR = original


def test_build_report_today_section_excludes_older_crossback_records():
    """「今日新增」区段只能放当天的信号。

    main.py 把 v_eps_v2 当天信号 + crossback pipeline 吐出的**全部历史 crossback**
    一起塞进 records（crossback 是「自信号日以来」累积的）。以前这里不按日期过滤，
    于是 2026-09-29 的 crossback 也被标成「🆕 今日新增 / 明日开盘买入」。
    """
    from signals.report import _build_report

    today_records = [
        {
            "日期": "2026-10-30", "股票": "DFIN", "信号": "买入",
            "信号类型": "v_eps_v2_buy", "强度(1-5)": 3, "参考止损": "$55.95",
            "触发原因": "[EPS] 最新季度EPS$1.44 | [量能] 4.4x均量",
        },
        {
            "日期": "2026-09-29", "股票": "MGNI", "信号": "买入",
            "信号类型": "crossback_buy", "强度(1-5)": 5, "参考止损": "$23.58",
            "触发原因": "[EMA10回踩] 上穿EMA10($24.29, +5.7%)",
        },
    ]

    content = _build_report(
        "2026-10-30", today_records, [], strategy_name="Weinstein + EPS"
    )

    today_section = content.split("## 过去 7 天信号汇总")[0]
    assert "DFIN" in today_section
    assert "MGNI" not in today_section, "历史 crossback 不该出现在「今日新增」区段"
    assert today_section.count("🆕 **今日新增**") == 1, "「今日新增」只能有一条"


def test_build_report_today_section_says_no_signal_when_only_old_records():
    """当天没信号但有历史 crossback 时，不该假装有今日新增。"""
    from signals.report import _build_report

    old_crossback = {
        "日期": "2026-09-29", "股票": "MGNI", "信号": "买入",
        "信号类型": "crossback_buy", "强度(1-5)": 5, "参考止损": "$23.58",
        "触发原因": "[EMA10回踩] 上穿EMA10($24.29, +5.7%)",
    }

    content = _build_report(
        "2026-10-30", [old_crossback], [], strategy_name="Weinstein + EPS"
    )

    today_section = content.split("## 过去 7 天信号汇总")[0]
    assert "今日无新信号" in today_section
    assert "MGNI" not in today_section