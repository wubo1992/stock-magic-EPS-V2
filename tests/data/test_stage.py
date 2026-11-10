"""Weinstein 阶段判定（Stage 1/2/3/4 + A/B 子阶段）的测试。

判据**完全移植**自参考项目的生产引擎：

    /Users/wubo/Desktop/codes/Stan Weinstein stage analysis/weinstein/classical.py
    规则版本：WEINSTEIN-STRUCTURE-1.3

参考实现跑在**周线**上，所以本项目的日线要先重采样成周线：

    MA30（30 周均线）+ 近 4 周斜率（±0.30% 走平）
    前 26 周区间支撑/阻力（不含本周）
    最近 4 周 vs 此前 4 周的更高/更低高点、更低低点
    前序阶段上下文（Stage 1 只有突破自身底部阻力才进 Stage 2）

本文件的每条 fixture 都用参考实现 `weinstein.classical.analyze_classical`
跑过一遍，期望值来自参考实现的真实输出，不是手写猜的。

判定用的是**当前**状态（最新一根 bar），所以能回答「这条信号现在还在不在 Stage 2」。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.stage import _developing_note, compute_stage  # noqa: E402


def _daily_from_weekly(
    closes: list[float],
    start: str = "2020-01-06",
    high_mult: float = 1.01,
    low_mult: float = 0.99,
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """把周收盘价展开成日线 bar（每周 5 个交易日：周一~周五）。

    这样 `W-FRI` 重采样能精确还原出原本的周线序列，
    周 bar 的 open=周内首个收盘、high=收盘*high_mult、low=收盘*low_mult、close=周末收盘。
    与参考实现的 fixture 构造方式一致。
    """
    start_ts = pd.Timestamp(start, tz="UTC")
    index: list[pd.Timestamp] = []
    rows: list[tuple[float, float, float, float]] = []
    for week_index, close in enumerate(closes):
        for day in range(5):
            index.append(start_ts + pd.Timedelta(days=week_index * 7 + day))
            rows.append((close, close * high_mult, close * low_mult, close))
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": volume,
        },
        index=pd.DatetimeIndex(index),
    )


# ---------------------------------------------------------------- fixtures
# 期望值来自参考实现 classical.analyze_classical（as_of=2030-01-01）：
#   base40+adv20   → stage=2 sub=2   dir=up
#   dec60          → stage=4 sub=4B  dir=down
#   dec30+drift30  → stage=1 sub=1   dir=flat
#   adv30+dip8+flat22 → stage=3 sub=3B dir=down


def _base_then_advance() -> list[float]:
    """40 周筑底 + 20 周主升 → Stage 2。"""
    base = [50.0] * 40
    advance = [50.0 * (1.02 ** (i + 1)) for i in range(20)]
    return base + advance


def _pure_decline() -> list[float]:
    """连续下跌 → Stage 4。"""
    return [100.0 * (0.97 ** (i + 1)) for i in range(60)]


def _decline_then_base() -> list[float]:
    """下跌 30 周后横向磨底 → Stage 1。"""
    decline = [100.0 * (0.97 ** (i + 1)) for i in range(30)]
    base = [decline[-1] * (1.001 ** (i + 1)) for i in range(30)]
    return decline + base


def _advance_then_top() -> list[float]:
    """主升 30 周后破位下跌再走平 → Stage 3（顶部区）。"""
    advance = [50.0 * (1.02 ** (i + 1)) for i in range(30)]
    dip = [advance[-1] * (1 - 0.008 * (i + 1)) for i in range(8)]
    flat = [dip[-1] * (1 + 0.005 * ((i % 4) - 1.5)) for i in range(22)]
    return advance + dip + flat


# ------------------------------------------------------------------ tests


def test_advance_after_base_is_stage_2():
    """筑底后放量主升 → Stage 2（策略的买点所在阶段）。"""
    res = compute_stage(_daily_from_weekly(_base_then_advance()))
    assert res.stage == 2, f"应该是 Stage 2，实际 {res.stage}：{res.reason}"
    assert "上升" in res.label
    assert res.px_vs_ma > 0
    assert res.slope_pct > 0


def test_pure_decline_is_stage_4():
    """持续下跌 → Stage 4（趋势已坏，仓位该走）。"""
    res = compute_stage(_daily_from_weekly(_pure_decline()))
    assert res.stage == 4, f"应该是 Stage 4，实际 {res.stage}：{res.reason}"
    assert "下跌" in res.label
    assert res.px_vs_ma < 0


def test_decline_then_base_is_stage_1():
    """下跌后横盘磨底 → Stage 1（筑底，不买）。"""
    res = compute_stage(_daily_from_weekly(_decline_then_base()))
    assert res.stage == 1, f"应该是 Stage 1，实际 {res.stage}：{res.reason}"
    assert "筑底" in res.label


def test_advance_then_breakdown_is_stage_3():
    """主升后破位下跌再走平 → Stage 3（顶部区，减仓）。"""
    res = compute_stage(_daily_from_weekly(_advance_then_top()))
    assert res.stage == 3, f"应该是 Stage 3，实际 {res.stage}：{res.reason}"
    assert "顶部" in res.label


def test_substage_is_reported():
    """主阶段之外还要给出 A/B 子阶段（参考实现的 GTA 映射）。"""
    res = compute_stage(_daily_from_weekly(_base_then_advance()))
    assert res.substage == "2", f"子阶段应该是 2，实际 {res.substage}"
    assert res.substage_name
    assert res.substage_reason


def test_insufficient_weekly_history_cannot_judge():
    """周线不足 60 根 → 判断不了（参考实现硬性要求），不能瞎猜。"""
    res = compute_stage(_daily_from_weekly(_base_then_advance()[:40]))
    assert res.stage is None, f"历史不足不该给结论，实际 {res.stage}"
    assert res.label == "—"
    assert "周线" in res.reason


def test_same_day_double_bars_do_not_change_the_verdict():
    """同一天两根 bar（合并量 + IEX）不能改变阶段判定。"""
    base = _daily_from_weekly(_base_then_advance())
    dup = pd.concat([
        base,
        base.assign(volume=base["volume"] * 30, close=base["close"] + 0.01)
             .set_axis(base.index + pd.Timedelta(hours=4)),
    ]).sort_index(kind="stable")

    assert compute_stage(base).stage == compute_stage(dup).stage


def test_volume_regime_switch_does_not_change_the_verdict():
    """行情源从「合并量」切到「IEX」时量能差 20~50 倍，不能把量比打穿。

    本项目日线 pkl 里同一天有两根 bar，去重后留下的是 IEX 那一根，
    它的量只有合并量的 1/20~1/50。若量比跨口径比较，最近几周会假性暴跌，
    触发 Stage 4B 的「缩量」误判。阶段判定必须不受影响。
    """
    closes = _base_then_advance()
    df = _daily_from_weekly(closes)
    # 把后 10 周的量砍到 1/30（模拟切到 IEX 口径）
    cutoff = df.index.max() - pd.Timedelta(days=7 * 9)
    df.loc[df.index > cutoff, "volume"] = df.loc[df.index > cutoff, "volume"] / 30.0

    res = compute_stage(df)
    assert res.stage == 2, f"量能口径切换不该改判，实际 {res.stage}：{res.reason}"


def test_reason_explains_the_verdict():
    """每个结论都必须带一句人能看懂的理由（报告里要印出来供审计）。"""
    for closes in (_base_then_advance(), _pure_decline(), _decline_then_base(), _advance_then_top()):
        res = compute_stage(_daily_from_weekly(closes))
        assert res.reason, "没有理由 = 没法审计"
        assert "30周均线" in res.reason or "30 周均线" in res.reason


def test_label_carries_stage_number_and_name():
    """标签必须同时带阶段号和中文名，报告里要一眼看懂。"""
    res = compute_stage(_daily_from_weekly(_base_then_advance()))
    assert res.label.startswith("Stage 2")
    assert "上升期" in res.label


def test_developing_note_only_fires_when_the_partial_week_flipped_the_stage():
    """本周没走完 → 读数是暂定的；但只有真把阶段翻掉了才值得说。

    参考实现的口径：发展中结论可变化。若与上一完整周结论一致，就不是「可能翻」，
    只是「还没收完」，没必要在每一行都刷一句发展中。
    """
    assert _developing_note(False, 3, 3, "3") == ""
    same = _developing_note(True, 3, 3, "3")
    assert "一致" in same, f"阶段没变就该说一致：{same}"
    flip = _developing_note(True, 1, 4, "4B-")
    assert "Stage 4" in flip and "Stage 1" in flip, f"翻阶段要说清从哪到哪：{flip}"


def test_compute_stage_accepts_as_of_and_marks_the_partial_week():
    """as_of 落在最后一根周线那一周内 → 该周是「发展中」的暂定读数。"""
    df = _daily_from_weekly(_base_then_advance())
    last_bar = df.index.max()
    developing = compute_stage(df, as_of=last_bar.date())
    assert developing.developing is True, "as_of 在本周内却没标发展中"
    assert developing.confirmed_stage is not None
    closed = compute_stage(df, as_of=(last_bar + pd.Timedelta(days=10)).date())
    assert closed.developing is False, "上一周就完成了，不该是发展中"
