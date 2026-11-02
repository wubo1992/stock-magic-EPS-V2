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


def test_missing_rs_rating_message_matches_the_strategy():
    """RS 缺失时的说明必须区分「老记录」和「该策略不接入 RS」。

    现场（2026-10-30）：cigar_butt_v1a / v_weinstein 的**当天**信号被写成
    「—（该记录早于 RS Rating 上线）」—— 那条策略根本没接 RS Rating，
    跟上线早晚无关，写错了就是在对用户说假话。
    """
    from signals.report import _build_report

    weinstein_today = {
        "日期": "2026-10-30", "股票": "AEYE", "信号": "买入",
        "信号类型": "v_weinstein_buy", "强度(1-5)": 3, "参考止损": "$12.72",
        "触发原因": "[Stage2] SMA150上升30天 | [量能] 5.4x均量",
    }

    content = _build_report(
        "2026-10-30", [weinstein_today], [], strategy_name="Weinstein Stage 2"
    )

    assert "该记录早于 RS Rating 上线" not in content
    assert "未接入 RS" in content


def test_missing_stop_loss_does_not_claim_a_ten_percent_loss():
    """没有参考止损时，风险提示不该凭空说「亏损约 10%」。"""
    from signals.report import _build_report

    cigar_today = {
        "日期": "2026-10-30", "股票": "TCRX", "信号": "买入",
        "信号类型": "cigar_butt_v1a_buy", "强度(1-5)": 2, "参考止损": "—",
        "触发原因": "NCAV选股: P/NCAV=0.38",
    }

    content = _build_report(
        "2026-10-30", [cigar_today], [], strategy_name="捡烟蒂V1A"
    )

    assert "参考止损 — 对应从买入价亏损约 10%" not in content


def test_crossback_report_always_shows_recent_pnl_section(tmp_path):
    """10 天内没有 crossback 触发时，收益追踪区段不能整段消失。

    现场（2026-11-02）：最后一次 crossback 是 10-22，落在 10 天窗口之外，
    `if recent_pnl:` 直接跳过整个区段 —— 用户打开报告看到区段凭空不见，
    分不清是「没信号」还是「坏了」。
    """
    from pathlib import Path
    from datetime import datetime, timezone
    from signals.report import generate_crossback_report

    watchlist = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-18",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
            "crossback_count": 1,
            "last_crossback_date": "2026-08-24",
        }
    }

    today = datetime(2026, 11, 2, tzinfo=timezone.utc)
    import signals.report as report_mod
    original = report_mod.OUTPUT_DIR
    report_mod.OUTPUT_DIR = tmp_path
    try:
        path = generate_crossback_report(
            records=[],
            watchlist=watchlist,
            strategy_id="v_eps_v2",
            today=today,
            recent_pnl=[],
        )
        content = Path(path).read_text(encoding="utf-8")
        assert "过去 10 天信号收益追踪" in content
        assert "过去 10 天无触发信号" in content
    finally:
        report_mod.OUTPUT_DIR = original


def _dfin_record():
    return {
        "日期": "2026-11-02", "股票": "DFIN", "信号": "买入",
        "信号类型": "v_eps_v2_buy", "强度(1-5)": 3, "参考止损": "$61.17",
        "触发原因": "[EPS] 最新季度EPS$1.44 | [Stage2] SMA150上升30天 | [量能] 3.0x均量",
    }


def test_stop_loss_percentage_is_computed_not_hardcoded():
    """止损百分比必须算出来，不能写死 10%。

    现场（2026-11-02）：v_eps_v2 的 stop_loss_pct 是 8%（$66.49 → $61.17），
    报告却写「对应从买入价亏损约 10%」—— 那个 10% 是写死的，
    v_weinstein 才是 10%。写死等于对用户说假话。
    """
    from signals.report import _build_report

    watchlist = {
        "DFIN": {
            "symbol": "DFIN", "signal_date": "2026-11-02",
            "signal_price": 66.49, "stage2_stop": 61.17,
        }
    }

    content = _build_report(
        "2026-11-02", [_dfin_record()], [],
        strategy_name="Weinstein + EPS", watchlist=watchlist,
    )

    assert "参考止损 $61.17 对应从买入价亏损约 8%" in content
    assert "亏损约 10%" not in content


def test_stop_loss_percentage_is_omitted_when_entry_price_is_unknown():
    """拿不到买入价时就不写百分比 —— 不能编一个数出来。"""
    from signals.report import _build_report

    content = _build_report(
        "2026-11-02", [_dfin_record()], [], strategy_name="Weinstein + EPS"
    )

    assert "对应从买入价亏损约" not in content
    assert "参考止损 $61.17" in content