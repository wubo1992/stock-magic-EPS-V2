"""
历史信号的 RS 必须按**当前口径重算**，否则同一份报告里同一条信号有两个数。

背景（2026-10-29 实测）
------------------------
`signals.csv` 里的 RS 列是**信号发出那天**算好写死的字符串。排名池从 3,844 只
扩到 9,082 只之后，10-22 那批信号还带着旧池子的口径：

    CSV   2026-10-22 SIG ... RS-Rating=90 (Consumer Discretionary 排名151/930)
    报告  2026-10-29 SIG ... RS-Rating=91 (Consumer Discretionary 排名160/990)

同一条信号、同一份报告，两个 RS Rating、两个板块分母 —— 用户没法判断该信哪个。
GCT/RCEL 更极端：CSV 里 RS 是空的（信号发出当天算不出来），
现在扩池之后能算出来（97/99），报告却还写着「—」。

→ 报告展示历史信号时按**当前口径重算**。
→ CSV 保持冻结：它是「当时报了什么」的日志，不该被改写。
"""
from data.rs import RSResult
from signals.report import refresh_history_rs


def _sig_record() -> dict:
    """一条 10-22 的 crossback 信号，RS 是旧池子口径写死的。"""
    return {
        "日期": "2026-10-22",
        "股票": "SIG",
        "信号": "买入",
        "信号类型": "crossback_buy",
        "强度(1-5)": 4,
        "参考止损": "$108.60",
        "触发原因": (
            "[EMA10回踩] 上穿EMA10($114.82, +1.5%) + 量能2.0x均量"
            " | [RS] RS/SPY=3.02x / RS-10W=1.27x / RS-10D=1.12x"
            " / RS-Rating=90 (Consumer Discretionary 排名151/930)"
        ),
        "RS相对强度": "90",
        "RS/SPY": "3.02",
        "RS-10W": "1.27",
        "RS-10D": "1.12",
        "板块排名": "151/930",
    }


def _new_rs() -> RSResult:
    """扩池之后重算出来的 SIG：评级和板块分母都变了。"""
    return RSResult(
        rs_spy=3.02,
        sector="Consumer Discretionary",
        rs_10w_spy=1.27,
        rs_10d_spy=1.12,
        sector_rank=160,
        sector_total=990,
        rs_rating=91,
    )


def test_refresh_replaces_rs_columns_and_reason_segment():
    """重算之后，RS 列和 [RS] 段都必须换成新的值。"""
    out = refresh_history_rs([_sig_record()], lambda sym, d: _new_rs())

    r = out[0]
    assert r["RS相对强度"] == "91"
    assert r["板块排名"] == "160/990"
    assert r["RS/SPY"] == "3.02"
    assert "RS-Rating=91" in r["触发原因"]
    assert "RS-Rating=90" not in r["触发原因"]
    assert "排名160/990" in r["触发原因"]


def test_refresh_keeps_the_other_reason_sections_intact():
    """只准动 [RS] 段 —— [EMA10回踩] 里的价格不能被改掉。"""
    out = refresh_history_rs([_sig_record()], lambda sym, d: _new_rs())

    assert "[EMA10回踩] 上穿EMA10($114.82, +1.5%)" in out[0]["触发原因"]


def test_refresh_adds_rs_segment_when_the_record_had_none():
    """
    当时算不出 RS 的记录（GCT/RCEL），现在能算出来了 → 必须把 [RS] 段补上。

    补不上就等于报告还在说「这只票没有 RS」，而它明明有。
    """
    record = {
        "日期": "2026-10-22",
        "股票": "GCT",
        "信号": "买入",
        "信号类型": "v_eps_v2_buy",
        "强度(1-5)": 3,
        "参考止损": "$55.57",
        "触发原因": "[EPS] 最新季度EPS$1.16 | [Stage2] SMA150上升30天 | [量能] 2.0x均量",
        "RS相对强度": "",
        "RS/SPY": "",
        "RS-10W": "",
        "RS-10D": "",
        "板块排名": "",
    }
    new = RSResult(
        rs_spy=7.39,
        sector="Consumer Discretionary",
        rs_10w_spy=1.10,
        rs_10d_spy=1.00,
        sector_rank=44,
        sector_total=969,
        rs_rating=97,
    )

    out = refresh_history_rs([record], lambda sym, d: new)

    assert out[0]["RS相对强度"] == "97"
    assert out[0]["板块排名"] == "44/969"
    assert "[RS] RS/SPY=7.39x" in out[0]["触发原因"]
    assert "[EPS] 最新季度EPS$1.16" in out[0]["触发原因"]


def test_refresh_keeps_frozen_values_when_recompute_fails():
    """重算不出来（历史不足 / 数据脏）→ 保留原值，不能把已有信息抹成空。"""
    record = _sig_record()
    out = refresh_history_rs([record], lambda sym, d: None)

    assert out[0]["RS相对强度"] == "90"
    assert out[0]["板块排名"] == "151/930"
    assert out[0]["触发原因"] == record["触发原因"]


def test_refresh_keeps_frozen_values_when_the_computation_raises():
    """重算抛异常也不能让整份报告挂掉 —— 退回原值继续。"""
    def _boom(sym, d):
        raise RuntimeError("炸了")

    out = refresh_history_rs([_sig_record()], _boom)

    assert out[0]["RS相对强度"] == "90"


def test_refresh_does_not_mutate_the_input_records():
    """输入的记录是 CSV 行，不能被就地改掉（调用方还要拿它做别的事）。"""
    record = _sig_record()
    refresh_history_rs([record], lambda sym, d: _new_rs())

    assert record["RS相对强度"] == "90"
    assert record["板块排名"] == "151/930"


def test_refresh_without_a_compute_fn_is_a_noop():
    """没传重算函数 → 原样返回（调用方可以不开启这个行为）。"""
    history = [_sig_record()]
    assert refresh_history_rs(history, None) == history


def test_report_shows_the_refreshed_rs_for_history_rows(tmp_path, monkeypatch):
    """
    端到端：报告里「过去 7 天信号汇总」和「详细解读」都得用重算后的值。

    这是整件事的验收点 —— 用户读的是报告，不是 CSV。
    """
    import signals.report as report_mod

    monkeypatch.setattr(report_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(
        report_mod, "_read_recent_signals", lambda **kw: [_sig_record()]
    )

    path = report_mod.generate_daily_report(
        [],
        strategy_id="v_eps_v2",
        strategy_name="测试策略",
        rs_compute=lambda sym, d: _new_rs(),
    )

    content = path.read_text(encoding="utf-8")
    # 汇总表用新评级
    assert "| 2026-10-22 | **SIG** | 91 |" in content
    assert "| 2026-10-22 | **SIG** | 90 |" not in content
    # 详细解读用新区间
    assert "Consumer Discretionary 板块排名 160/990" in content
    assert "151/930" not in content
