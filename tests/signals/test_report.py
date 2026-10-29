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