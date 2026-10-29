"""
_compute_weighted_return / _compute_10w_return / _compute_10d_return 的直接测试。

这些函数此前是**零覆盖**的：tests/data/test_rs_rating.py 的 12 个用例全都预置
rs_daily_cache.json['__UNIVERSE__'] 再调 _get_rs_rating，从来没碰过加权收益本身。
docs/superpowers/specs/2026-07-31-rs-strategy-integration-design.md:222 列了
test_compute_weighted_return 但一直没写。

本文件最重要的用例是 test_dirty_at_iloc_minus_253_rejected：它把「脏数据窗口
必须是 254 而不是 253」这个算术决定固化成契约。
"""
import pandas as pd
import pytest

from data.rs import (
    DIRTY_WINDOW_WEIGHTED,
    _compute_10d_return,
    _compute_10w_return,
    _compute_weighted_return,
    _deduplicate_bars,
)
from data.rs_universe import MAX_PRICE_SPAN

AS_OF = pd.Timestamp("2026-10-22", tz="UTC")
N_BARS = 400


def _frame(closes) -> pd.DataFrame:
    """造一个只有 close 列的单日线 DataFrame（索引是逐日递增的 UTC 时间戳）。"""
    idx = pd.date_range(end=AS_OF, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"close": [float(c) for c in closes]}, index=idx)


def _flat(n=N_BARS, start=100.0, drift=1.0):
    """稳定爬升的收盘价序列，没有任何跳空。"""
    return [start + drift * i for i in range(n)]


def _with_break_at(offset: int, n=N_BARS, ratio=0.2):
    """
    在 iloc[-offset] 这一根上制造一次 -80% 跳空（模拟反向拆股）。
    offset 越小 = 越靠近最近一根。
    """
    closes = _flat(n)
    closes[-offset] = closes[-offset] * ratio
    return closes


def _gapped_frame(gap_days: int, tail_bars: int) -> pd.DataFrame:
    """
    造一份「中间断了一大截」的行情。

    最后 tail_bars 根是连续的日线，再往前 gap_days 天处有 1 根 bar。
    于是「最后 tail_bars + 1 根 bar」横跨的日历天数 = (tail_bars - 1) + gap_days。

    这是真实坏数据的形状：Alpaca 返回 `volume == 0` 的占位行，
    我们 pipeline 里的 `df[df["volume"] > 0]` 过滤把它们剔掉，
    于是 253 根 bar 里少了半年 —— 「12 个月的相对强度」算成了
    「上一次有数据到现在」。
    """
    tail = pd.date_range(end=AS_OF, periods=tail_bars, freq="D", tz="UTC")
    head = pd.DatetimeIndex([tail[0] - pd.Timedelta(days=gap_days)])
    idx = head.append(tail)
    return pd.DataFrame({"close": _flat(len(idx))}, index=idx)


def _with_span_at(offset: int, span: float, n=N_BARS):
    """
    在 iloc[-offset] 这一根上把价格压到「窗口最大值 ÷ span」，使 253 根窗口内
    的 max/min **恰好**等于 span。

    两个容易踩的坑：
      - 不能靠整段缩放来造这个比值 —— 等比缩放不改变 max/min。
      - 也不能拿那根 bar 自己的原值去除：窗口最大值来自最后几根的漂移段
        （499 / 247 ≈ 2.02），除下来实际比值会翻倍，边界测试就会两边都判死。
    仅用于 span > 1（把那根压成窗口最小值）。
    """
    closes = _flat(n)
    closes[-offset] = max(closes[-253:]) / span
    return closes


# ── 基本公式 ──────────────────────────────────────────────────────

def test_weighted_return_matches_hand_computed():
    """用可手算的线性序列验证 0.4*R63 + 0.2*R126 + 0.2*R189 + 0.2*R252。"""
    closes = _flat()
    got = _compute_weighted_return(_frame(closes), AS_OF)

    cur = closes[-1]
    expected = (
        0.4 * ((cur / closes[-64]) - 1)
        + 0.2 * ((cur / closes[-127]) - 1)
        + 0.2 * ((cur / closes[-190]) - 1)
        + 0.2 * ((cur / closes[-253]) - 1)
    )
    assert got == pytest.approx(expected, rel=1e-12)


def test_weighted_return_needs_253_bars():
    """只有 252 根不够 —— R252 需要 iloc[-253] 才能拿到基准价。"""
    assert _compute_weighted_return(_frame(_flat(252)), AS_OF) is None
    assert _compute_weighted_return(_frame(_flat(253)), AS_OF) is not None


def test_weighted_return_rejects_nonpositive_close():
    closes = _flat()
    closes[-1] = 0.0
    assert _compute_weighted_return(_frame(closes), AS_OF) is None


def test_weighted_return_rejects_nan_past():
    """R252 基准价是 NaN 时必须返回 None，而不是把 NaN 传进加权收益。"""
    closes = _flat()
    df = _frame(closes)
    df.iloc[-253, df.columns.get_loc("close")] = float("nan")
    assert _compute_weighted_return(df, AS_OF) is None


# ── 脏数据窗口边界（本文件的核心）────────────────────────────────

def test_real_break_at_iloc_minus_252_is_admitted():
    """
    单根 -80% 的跳空**必须放过** —— 排名池排脏看的是价格量级，不是涨跌幅。

    数据来自 Alpaca `adjustment="all"`，已复权；此时窗口里的 ±50% 是真实行情。
    实测 RCEL 2026-08-07 +63.6%（量比 43x、振幅 34.7%，raw 与 all 两侧都有），
    AAOI +66.3%、AEMD +372% 同理。旧规则把它们整只判死，RCEL 的
    wret=+253% 本该排进前 1% 却拿不到评级。
    """
    got = _compute_weighted_return(_frame(_with_break_at(252)), AS_OF)
    assert got is not None, "真实跳空被误判成脏数据"


def test_absurd_span_at_iloc_minus_252_rejected():
    """真正该挡的是价格量级荒谬 —— 与跳空发生在第几根无关。"""
    got = _compute_weighted_return(
        _frame(_with_span_at(252, MAX_PRICE_SPAN * 10)), AS_OF
    )
    assert got is None, "价格量级荒谬的序列进了排名池"


def test_absurd_span_at_base_price_rejected():
    """
    iloc[-253] 就是 R252 的基准价，它必须落在量级检查的窗口内。

    窗口取 253 根（MIN_HISTORY_DAYS），与 R252 的回看完全重合。
    少一根就会漏掉基准价上的量级崩坏 —— 而基准价算错，R252 直接全错。
    """
    got = _compute_weighted_return(
        _frame(_with_span_at(253, MAX_PRICE_SPAN * 10)), AS_OF
    )
    assert got is None, "R252 基准价上的量级崩坏没被抓到"


def test_absurd_span_one_step_outside_window_allowed():
    """
    iloc[-254] 不在 253 根窗口内，它上面的量级崩坏不该牵连这只票落选。

    那根 bar 的价格不参与任何计算（最深的回看就是 R252 的 iloc[-253]）。
    早于它的历史同理：ABAT 2024-12-24 +86%、ABLV 2025-03-07 +137% 都是正常暴涨。
    """
    got = _compute_weighted_return(
        _frame(_with_span_at(254, MAX_PRICE_SPAN * 10)), AS_OF
    )
    assert got is not None, "窗口外的量级崩坏不该影响入选"


def test_price_span_boundary():
    """阈值两侧必须给出相反结果。"""
    assert _compute_weighted_return(
        _frame(_with_span_at(253, MAX_PRICE_SPAN / 2.0)), AS_OF
    ) is not None
    assert _compute_weighted_return(
        _frame(_with_span_at(253, MAX_PRICE_SPAN * 2.0)), AS_OF
    ) is None


def test_dirty_far_in_past_allowed():
    """
    窗口之外的历史跳空不再影响判定 —— 这是本次修复的核心。

    修复前这类票会被永久判死：ABAT 2024-12-24 +86%、ABLV 2025-03-07 +137%，
    这些都是正常的暴涨，不是脏数据。
    """
    closes = _flat()
    closes[10] = closes[10] * 5.0  # 远在窗口之外
    got = _compute_weighted_return(_frame(closes), AS_OF)
    assert got is not None


def test_dirty_window_constant_stays_254():
    """
    254 这个数必须锁死。

    RS 排名池自己已经不用跳空排脏了（改用 MAX_PRICE_SPAN），但
    `tools/fix_dirty_history.py` 仍用它挑「值得重下的文件」——
    那是一次性修复工具，用跳空当启发式没问题。所以常量要留着，
    且不能被改回 253（少一根就看不见 iloc[-254] -> iloc[-253] 这一跳）。
    """
    assert DIRTY_WINDOW_WEIGHTED == 254


# ── 10 周 / 10 日窗口 ───────────────────────────────────────────

def test_10w_matches_hand_computed():
    closes = _flat()
    got = _compute_10w_return(_frame(closes), AS_OF)
    assert got == pytest.approx((closes[-1] / closes[-51]) - 1, rel=1e-12)


def test_10d_matches_hand_computed():
    closes = _flat()
    got = _compute_10d_return(_frame(closes), AS_OF)
    assert got == pytest.approx((closes[-1] / closes[-11]) - 1, rel=1e-12)


def test_10w_real_break_is_admitted():
    """单日 -80% 是真实行情，10w 必须给数 —— 理由同 weighted，见 MAX_PRICE_SPAN。"""
    assert _compute_10w_return(_frame(_with_break_at(5)), AS_OF) is not None


def test_10d_real_break_is_admitted():
    assert _compute_10d_return(_frame(_with_break_at(5)), AS_OF) is not None


def test_10w_rejects_absurd_price_span():
    """10w 的量级窗口是 51 根：iloc[-51] 就是它的基准价，必须落在窗口内。"""
    assert _compute_10w_return(
        _frame(_with_span_at(51, MAX_PRICE_SPAN * 10)), AS_OF
    ) is None


def test_10d_rejects_absurd_price_span():
    """10d 的量级窗口是 11 根，iloc[-11] 是它的基准价。"""
    assert _compute_10d_return(
        _frame(_with_span_at(11, MAX_PRICE_SPAN * 10)), AS_OF
    ) is None


def test_10w_allows_corruption_just_outside_window():
    """iloc[-52] 不在 51 根窗口内，它上面的量级崩坏不该牵连这只票。"""
    assert _compute_10w_return(
        _frame(_with_span_at(52, MAX_PRICE_SPAN * 10)), AS_OF
    ) is not None


def test_10d_allows_corruption_just_outside_window():
    assert _compute_10d_return(
        _frame(_with_span_at(12, MAX_PRICE_SPAN * 10)), AS_OF
    ) is not None


def test_10w_price_span_boundary():
    """阈值两侧必须给出相反结果。"""
    assert _compute_10w_return(
        _frame(_with_span_at(51, MAX_PRICE_SPAN / 2.0)), AS_OF
    ) is not None
    assert _compute_10w_return(
        _frame(_with_span_at(51, MAX_PRICE_SPAN * 2.0)), AS_OF
    ) is None


def test_10d_price_span_boundary():
    assert _compute_10d_return(
        _frame(_with_span_at(11, MAX_PRICE_SPAN / 2.0)), AS_OF
    ) is not None
    assert _compute_10d_return(
        _frame(_with_span_at(11, MAX_PRICE_SPAN * 2.0)), AS_OF
    ) is None


def test_all_three_windows_admit_the_same_real_moves():
    """
    三个回看线对同一段真实行情必须给出**一致**的判断。

    这条曾经是反的：weighted 改用价格量级排脏之后，10w/10d 还留在「单日 ±50%」，
    于是 RCEL 那类票「有 RS 评级但没有 RS(10W)」—— 用户拿到的信号表直接少一列。
    涨跌幅分不出「+63% 的好消息」和「没复权的拆股」，所以三个函数都得看价格量级。

    方向仍然是「宁可多给一列可能偏高的数，也不要把真实强势股整只踢出去」。
    """
    df = _frame(_with_break_at(5))  # 最近 5 根里有一次 -80%，量级比 5 倍，正常
    assert _compute_weighted_return(df, AS_OF) is not None
    assert _compute_10w_return(df, AS_OF) is not None
    assert _compute_10d_return(df, AS_OF) is not None


# ── 日历跨度闸门：窗口被空洞拉长 ──────────────────────────────────
#
# 「253 根 bar」在正常情况下摊在 366 个自然日里。数据中间少一大截时，
# 这 253 根 bar 会摊在两年、三年甚至十年里 —— 那算出来的根本不是
# 「12 个月的相对强度」，而是「上一次有数据到现在」。
#
# 实测（2026-10-26，10,695 只行情 pkl）：窗口日历跨度的分布是
#   p50 = 366 天   p90 = 368 天   p95 = 389 天   p99 = 1,407 天
# 而跨度 > 400 天的 440 只里，wret p99 = 40.7、max = 130.7 —— 垃圾数值
# 全都坐在 RS 排行榜的头名上（AKTS 曾是全市场第 1，wret 41.6）。


def test_weighted_return_rejects_a_window_stretched_by_a_hole():
    """窗口横跨 401 个自然日 > 400 —— 这只票必须落选。"""
    got = _compute_weighted_return(_gapped_frame(gap_days=150, tail_bars=252), AS_OF)
    assert got is None, "窗口被空洞拉长的票进了排名池"


def test_weighted_return_admits_a_window_of_exactly_400_days():
    """边界：正好 400 天必须放过（400 是上限，不是分界点以外全杀）。"""
    got = _compute_weighted_return(_gapped_frame(gap_days=149, tail_bars=252), AS_OF)
    assert got is not None, "400 天的窗口被误杀"


def test_10w_rejects_a_window_stretched_by_a_hole():
    """10w 的窗口是 51 根，跨度上限 100 天。"""
    assert _compute_10w_return(_gapped_frame(gap_days=52, tail_bars=50), AS_OF) is None


def test_10w_admits_a_window_of_exactly_100_days():
    assert _compute_10w_return(_gapped_frame(gap_days=51, tail_bars=50), AS_OF) is not None


def test_10d_rejects_a_window_stretched_by_a_hole():
    """10d 的窗口是 11 根，跨度上限 25 天。"""
    assert _compute_10d_return(_gapped_frame(gap_days=17, tail_bars=10), AS_OF) is None


def test_10d_admits_a_window_of_exactly_25_days():
    assert _compute_10d_return(_gapped_frame(gap_days=16, tail_bars=10), AS_OF) is not None


def test_a_hole_outside_a_lines_own_window_does_not_kill_it():
    """
    跨度闸门按**各自**的窗口量，不能一刀切。

    这里的空洞落在 weighted 的 253 根窗口里，但 10w 的 51 根、10d 的 11 根
    都是连续的近期日线 —— 它们量到的跨度分别是 50 天和 10 天，完全正常。
    10w/10d 必须照常给数：把一个只影响 R252 的数据缺陷扩散到另外两条线，
    等于凭空砍掉用户的 RS (10W) / RS (10D) 两列。
    """
    df = _gapped_frame(gap_days=150, tail_bars=252)
    assert _compute_weighted_return(df, AS_OF) is None
    assert _compute_10w_return(df, AS_OF) is not None
    assert _compute_10d_return(df, AS_OF) is not None


# ── 去重（pkl 里同一天可能有多根 intraday bar）─────────────────────

def test_deduplicate_bars_collapses_intraday_rows():
    """同一交易日的多条 bar 按日取第一条。"""
    day = pd.Timestamp("2026-10-21", tz="UTC")
    idx = pd.DatetimeIndex(
        [day + pd.Timedelta(hours=4), day + pd.Timedelta(hours=13, minutes=30)]
    )
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=idx)
    out = _deduplicate_bars(df)
    assert len(out) == 1
    assert out.iloc[0] == 10.0