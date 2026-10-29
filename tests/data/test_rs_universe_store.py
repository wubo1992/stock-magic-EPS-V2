"""
data/rs_universe.py 的测试。

最重要的一组是 `test_vectorized_matches_scalar_*`：宽表是「排名池的唯一真相来源」，
一旦向量化实现和标量 `_compute_weighted_return` 有偏差，所有 RS Rating 都是错的，
而且**不会报任何错**（就是一个安静偏掉的百分位）。所以必须逐值对拍。
"""
import numpy as np
import pandas as pd
import pytest

from data import rs_universe as ru
from data.rs import (
    DIRTY_WINDOW_WEIGHTED,
    MIN_HISTORY_DAYS,
    _compute_weighted_return,
)

AS_OF = pd.Timestamp("2026-10-22", tz="UTC")


def _frame(closes, start_offset=400):
    idx = pd.date_range(end=AS_OF, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"close": [float(c) for c in closes]}, index=idx)


def _flat(n=400, start=100.0, drift=1.0):
    return [start + drift * i for i in range(n)]


def _with_break_at(offset, n=400, ratio=0.2):
    closes = _flat(n)
    closes[-offset] = closes[-offset] * ratio
    return closes


def _with_step_at(offset, pct, n=400):
    """
    在 iloc[-offset] 造一次**永久跳空**（复权缺口 / 未复权拆股痕迹）。

    注意是台阶不是 V：跳空之后所有价格都整体抬高 pct，再平滑延续。
    这是真实脏数据的形状 —— 也是唯一能暴露「收益率取倒数不对称」的形状。
    做成 V（先跌后涨）的话，反向算出来的那个 -36% 也会越线，
    反而把 bug 盖住了。
    """
    closes = _flat(n)
    shift = closes[-offset] * pct
    for i in range(n - offset, n):
        closes[i] += shift
    return closes


def _with_span_at(offset, span, n=400):
    """
    造一条「253 根窗口内 max/min **恰好**等于 span」的序列。

    注意**不能**靠整段缩放来造：等比缩放不改变 max/min 的比值。
    必须真的把某一根 bar 压下去（它成为 min）。

    也**不能**拿那根 bar 自己的原值去除：窗口最大值来自最后几根的漂移段，
    除下来实际比值会带上 ~2 倍误差，阈值两侧的边界测试就会两边都判死。
    改成「窗口最大值 ÷ span」，比值才是精确的。仅用于 span > 1。

    形状取自真实的坏数据：SXTC 是「首价上亿、末价 0.048」，
    即窗口早期的某几根高得离谱，而不是整段均匀地高。
    """
    closes = _flat(n)
    closes[-offset] = max(closes[-253:]) / span
    return closes


def _wide_from_closes(**series) -> pd.DataFrame:
    """从 {symbol: [close...]} 造一张连续日期的宽表（无缺口）。"""
    lengths = {len(v) for v in series.values()}
    assert len(lengths) == 1, "测试辅助函数要求各列等长（避免人为造缺口）"
    n = lengths.pop()
    idx = pd.date_range(end=AS_OF, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({k: [float(x) for x in v] for k, v in series.items()}, index=idx)


def _ragged_wide(**series) -> pd.DataFrame:
    """各列长度可以不同、结束日期也可以不同（模拟休市 / 落后 / 上市晚）。"""
    idx = pd.date_range(end=AS_OF, periods=max(len(v) for v in series.values()), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            k: pd.Series([float(x) for x in v], index=idx[-len(v):])
            for k, v in series.items()
        }
    )


def _with_gap(closes: pd.Series, gap_offset: int) -> pd.Series:
    """在倒数第 gap_offset 根上挖一个日历缺口（模拟休市）。"""
    out = closes.copy()
    out.iloc[-gap_offset] = np.nan
    return out


def _as_price_df(wide: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """把宽表的一列还原成标量路径期望的长表（index=日期, close 列），丢掉 NaN 行。"""
    s = wide[symbol].dropna()
    return pd.DataFrame({"close": s.astype(float)}, index=s.index)


# ── 向量化 vs 标量对拍 ────────────────────────────────────────────

def test_vectorized_matches_scalar_on_clean_data():
    """干净数据下逐值一致 —— 权重加总算错的话这里立刻炸。"""
    wide = _wide_from_closes(
        AAA=_flat(start=100.0, drift=1.0),
        BBB=_flat(start=200.0, drift=-0.4),
        CCC=_flat(start=500.0, drift=2.5),
    )
    got = ru.vectorized_weighted_return(wide)

    for sym in wide.columns:
        _assert_same(got[sym], _compute_weighted_return(_as_price_df(wide, sym), AS_OF), sym)


def _assert_same(vectorized, scalar, sym):
    """向量化给 NaN、标量给 None，两者语义等价（「这只票算不出来」）。"""
    if scalar is None:
        assert pd.isna(vectorized), f"{sym} 标量判死但向量化给了数 {vectorized}"
    else:
        assert vectorized == pytest.approx(scalar, rel=1e-12, abs=1e-15), sym


def test_vectorized_matches_scalar_with_heterogeneous_calendars():
    """
    各票交易日历不一致时，仍必须与标量**逐值**一致。

    这是本模块最核心的一条。日历宽表的行是全体 symbol 日期的并集，
    早期版本直接在日历行上取 iloc[-253]，结果 6,303 只算出 **0 只**——
    因为一只票休市一天、换个交易所日历、或上市晚一点，位置就全错位了。
    """
    wide = _ragged_wide(
        FULL=_flat(400),
        SHORT_TAIL=_flat(380),
        NEWISH=_flat(300),
        WITH_HOLE=[*_flat(300)[:150], *_flat(400)[150:]],
        TOO_SHORT=_flat(200),
    )
    got = ru.vectorized_weighted_return(wide)

    for sym in ("FULL", "SHORT_TAIL", "NEWISH", "WITH_HOLE"):
        _assert_same(got[sym], _compute_weighted_return(_as_price_df(wide, sym), AS_OF), sym)

    # 只有历史不足 253 根的落选（标量也是 None）
    assert pd.isna(got["TOO_SHORT"])
    assert _compute_weighted_return(_as_price_df(wide, "TOO_SHORT"), AS_OF) is None


def test_real_gap_up_is_admitted():
    """
    单日大涨**必须放过** —— 这是旧规则最大的误伤来源。

    排名池里的数据来自 Alpaca `adjustment="all"`，已复权；此时窗口里的
    ±50% 不是「没复权好」，而是真实行情。实测反证（2026-10-23 逐 bar 比对
    raw 与 all：两侧都有这次跳空，且当日量比 43x、振幅 34.7%）：
      RCEL 2026-08-06 4.75 → 08-07 7.77（+63.6%），纳斯达克 100 成分股
      AEMD 2026-09-17 +372%，量比 199x
    旧规则把这些一并排除（603 只落选，其中 RCEL 的 wret=+253% 本该排进前 1%）。

    真正的数据损坏改由「价格量级合理性」把关，不用涨跌幅去猜 ——
    涨跌幅分不出「+63% 的好消息」和「没复权的拆股」。
    """
    wide = _wide_from_closes(
        UP=_with_step_at(200, 0.636),   # RCEL 那种 +63.6% 跳空
        CLEAN=_flat(),
    )
    got = ru.vectorized_weighted_return(wide)
    assert not pd.isna(got["UP"]), "真实跳空被误判成脏数据"
    assert not pd.isna(got["CLEAN"])

    # 单日暴跌同理 —— 只测上涨看不出「双向都放过」这件事
    wide_dn = _wide_from_closes(DOWN=_with_step_at(200, -0.565))  # AARD -56.5%
    assert not pd.isna(ru.vectorized_weighted_return(wide_dn)["DOWN"]), "真实暴跌被误判成脏数据"


def test_absurd_price_span_is_excluded():
    """
    真正该挡的是**价格量级荒谬**的序列，跳空幅度只是它的一个副作用。

    实测样本（Alpaca adjustment="all" 自己的产出，隔离档旧数据同样如此）：
      SXTC  首 101,760,000 → 末 0.048（跨 20 亿倍）
      HUBC  跨 256 万倍 / WHLR 跨 26.8 万倍 / OFAL 跨 139 倍

    这类序列的 252 天价格区间远超任何真实股票 —— 真实行情票里最高的
    AAOI 也才 11.45 倍、AEMD 20.1 倍，全池 99% 分位是 410 倍。
    所以用 max/min 这个**与涨跌幅无关**的量来判，涨得再 wild 也拦不住它。
    """
    wide = _wide_from_closes(
        BROKEN=_with_span_at(200, ru.MAX_PRICE_SPAN * 50),  # 跨 5 万倍
        WILD_JUMP=_with_step_at(200, 3.72),                # AEMD +372%，量级正常
        CLEAN=_flat(),
    )
    got = ru.vectorized_weighted_return(wide)
    assert pd.isna(got["BROKEN"]), "价格量级荒谬的序列进了排名池"
    assert not pd.isna(got["WILD_JUMP"]), "+372% 的真实行情被误杀"
    assert not pd.isna(got["CLEAN"])


def test_price_span_boundary():
    """阈值两侧必须给出相反结果，且阈值本身是可读常量、不藏在函数体里。"""
    wide = _wide_from_closes(
        UNDER=_with_span_at(200, ru.MAX_PRICE_SPAN / 2.0),
        OVER=_with_span_at(200, ru.MAX_PRICE_SPAN * 2.0),
    )
    got = ru.vectorized_weighted_return(wide)

    assert not pd.isna(got["UNDER"]), f"比值 {ru.MAX_PRICE_SPAN / 2:.0f} 倍不该被挡"
    assert pd.isna(got["OVER"]), f"比值 {ru.MAX_PRICE_SPAN * 2:.0f} 倍必须被挡"


def test_vectorized_matches_scalar_on_span_boundaries():
    """量级门槛两侧，逐只标量和整表向量必须给出**同一个**入选集合。"""
    wide = _wide_from_closes(
        CLEAN=_flat(),
        UNDER=_with_span_at(200, ru.MAX_PRICE_SPAN / 4.0),
        OVER=_with_span_at(200, ru.MAX_PRICE_SPAN * 4.0),
    )
    got = ru.vectorized_weighted_return(wide)
    for sym in wide.columns:
        _assert_same(got[sym], _compute_weighted_return(_as_price_df(wide, sym), AS_OF), sym)


def test_vectorized_matches_scalar_on_many_upward_breaks():
    """各种跳空幅度下，向量化与标量必须给出同一个入选集合。"""
    for pct in (0.55, 0.60, 0.75, 1.20, 2.00):
        wide = _wide_from_closes(UP=_with_step_at(200, pct))
        got = ru.vectorized_weighted_return(wide)
        _assert_same(
            got["UP"],
            _compute_weighted_return(_as_price_df(wide, "UP"), AS_OF),
            f"+{pct:.0%}",
        )


def test_trim_sparse_tail_keeps_sparse_middle_rows():
    """
    中间的稀疏行是美股节假日，必须保留。

    只砍尾部：把中间的稀疏行一并删掉，等于悄悄改数据 ——
    那天有 bar 的少数标的会被凭空抹掉一根。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    for i in range(8):  # 中间那行只有 2 列有数据（节假日）
        wide.iloc[200, i] = np.nan

    trimmed = ru.trim_sparse_tail(wide)
    assert trimmed.equals(wide), "中间的稀疏行被误删了"
    assert trimmed.iloc[200].notna().sum() == 2


def test_trim_sparse_tail_only_trims_tail():
    """尾部连续几行稀疏时全部砍掉，稀疏行之前的数据一点不动。"""
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    for i in range(8):
        wide.iloc[-3:, i] = np.nan

    trimmed = ru.trim_sparse_tail(wide)
    assert len(trimmed) == len(wide) - 3
    assert trimmed.equals(wide.iloc[:-3])


def test_vectorized_matches_scalar_dirty_boundaries():
    """脏数据边界上，逐只标量和整表向量必须给出**同一个**入选集合。"""
    wide = _wide_from_closes(
        CLEAN=_flat(),
        BREAK_AT_253=_with_break_at(253),
        BREAK_AT_254=_with_break_at(254),
        BREAK_AT_255=_with_break_at(255),
        BREAK_FAR_PAST=[*_flat()[:10], *([x * 5 for x in _flat()[10:200]]), *_flat()[200:]],
    )
    got = ru.vectorized_weighted_return(wide)

    for sym in wide.columns:
        _assert_same(got[sym], _compute_weighted_return(_as_price_df(wide, sym), AS_OF), sym)


def test_price_span_check_sees_across_calendar_gap():
    """
    价格量级检查必须跨过日历缺口。

    宽表是日历对齐的，缺口行是 NaN。若统计 max/min 时把 NaN 当 0，
    任何有缺口的票都会被误判成「最低价 0」而整只落选 —— 这正是
    `trim_sparse_tail` 和 bars-back 对齐存在的理由，不能被绕过。
    """
    full = _flat(400)
    # 缺口之后紧跟一根价格量级离谱的 bar
    holed = [*full[:150], *[x / (ru.MAX_PRICE_SPAN * 10) for x in full[150:151]], *full[151:]]
    wide = _wide_from_closes(HOLES=holed, CLEAN=_flat())
    wide.loc[wide.index[149], "HOLES"] = np.nan  # 日历缺口

    got = ru.vectorized_weighted_return(wide)
    assert pd.isna(got["HOLES"]), "缺口之后的量级异常没被抓到"
    assert not pd.isna(got["CLEAN"]), "有缺口的干净票不该被牵连落选"


def test_price_span_survives_halts_without_dropping_symbols():
    """
    长期停牌的票价格量级正常，只是 bar 少 —— 不能因为窗口不完整就整只落选。

    与 `_ragged_wide` 的停牌用例一起守住这条：量级检查看的是**已有 bar**
    的极值，不是要求窗口填满。
    """
    wide = _wide_from_closes(FULL=_flat())
    wide["HALTED"] = _with_gap(wide["FULL"], gap_offset=40)

    got = ru.vectorized_weighted_return(wide)
    assert not pd.isna(got["HALTED"]), "停牌票被量级检查误杀"
    assert not pd.isna(got["FULL"])


def _wide_with_stretched_symbol(hole_end: int, n_rows: int = 700) -> pd.DataFrame:
    """
    造一张宽表：19 只普通票在最后 400 行连续有数据；
    STRETCHED 的 bar 分布在第 0..hole_end 行连续，之后中间挖空，
    最后第 n_rows-1 行单独一根。

    它的「倒数第 253 根 bar」落在第 (hole_end - 251) 行，于是 253 根窗口
    横跨的日历天数 = (n_rows - 1) - (hole_end - 251)：
        hole_end = 550 -> 400 天     hole_end = 549 -> 401 天
    """
    idx = pd.date_range(end=AS_OF, periods=n_rows, freq="D", tz="UTC")
    data = {}
    for i in range(19):
        col = np.full(n_rows, np.nan)
        col[-400:] = np.arange(400, dtype=float) + 100.0 + i
        data[f"F{i}"] = col
    col = np.full(n_rows, np.nan)
    col[: hole_end + 1] = np.arange(hole_end + 1, dtype=float) + 100.0
    col[-1] = float(hole_end + 1) + 100.0
    data["STRETCHED"] = col
    return pd.DataFrame(data, index=idx)


def test_stretched_window_is_excluded_by_the_calendar_span_gate():
    """
    窗口横跨 401 个自然日 > 400 —— 这只票必须落选。

    它的 253 根 bar 里有一段半年的空洞，于是「12 个月的相对强度」
    实际算的是「上一次有数据到现在」。实测这类票的 wret 能到 40 倍
    （GNMX 130.7），直接坐在 RS 排行榜的头名上 —— AKTS 曾是全市场第 1。
    """
    wide = _wide_with_stretched_symbol(hole_end=549)
    got = ru.vectorized_weighted_return(wide)

    assert pd.isna(got["STRETCHED"]), "窗口被空洞拉长的票进了排名池"
    assert not pd.isna(got["F0"]), "正常票被牵连落选"


def test_window_of_exactly_400_days_is_admitted():
    """边界：正好 400 天必须放过。"""
    wide = _wide_with_stretched_symbol(hole_end=550)
    got = ru.vectorized_weighted_return(wide)
    assert not pd.isna(got["STRETCHED"]), "400 天的窗口被误杀"


def test_vectorized_matches_scalar_on_the_calendar_span_gate():
    """跨度闸门两侧，标量与向量必须给出同一个入选集合。"""
    wide = _wide_with_stretched_symbol(hole_end=549)
    got = ru.vectorized_weighted_return(wide)
    for sym in ("STRETCHED", "F0"):
        _assert_same(got[sym], _compute_weighted_return(_as_price_df(wide, sym), AS_OF), sym)


def test_as_of_slices_to_that_day():
    """
    宽表是当前快照，as_of 之前必须先切。

    crossback 按「信号当日」算 RS；不切的话回看 3 天的信号会拿到今天的排名池，
    而且返回值看着完全正常，没有任何报错。
    """
    wide = _wide_from_closes(A=_flat(400), B=_flat(400))
    past_day = wide.index[-4]

    got_today = ru.vectorized_weighted_return(wide)
    got_past = ru.vectorized_weighted_return(wide, as_of=past_day)

    assert not got_today["A"] == got_past["A"], "as_of 没有生效，回看日期拿到了今天的池子"
    _assert_same(got_past["A"], _compute_weighted_return(_as_price_df(wide, "A"), past_day), "A")


def test_as_of_naive_timestamp_is_accepted():
    wide = _wide_from_closes(A=_flat(400))
    assert not pd.isna(ru.vectorized_weighted_return(wide, as_of=wide.index[-1]).A)
    assert not pd.isna(ru.vectorized_weighted_return(wide, as_of=wide.index[-1].tz_localize(None)).A)


# ── 参考日：必须是「全市场都有数据」的那一天 ──────────────────────


def test_partial_last_row_does_not_shrink_the_pool():
    """
    末行只覆盖一部分票时，参考日必须回退到**全市场都有数据**的那一天。

    真实场景（2026-10-26 实测）：live run 只刷新 live pool，5,155 只票到了
    10-23，另外 4,377 只全市场票还停在 10-22。宽表末行覆盖率 5,156/10,695
    = 48%。拿末行当参考日 -> 排名池从 9,776 **静默腰斩**到 4,885，
    而且**不报任何错**，只是 RS Rating 的分位悄悄换了一个小得多的池子。

    参考日必须自己去找「全市场都有数据」的那一天（这里是 10-22）。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(20)}
    wide = _wide_from_closes(**syms)
    # 末行只有 5 只票有数据（= live pool 那一小撮被刷新了）
    for i in range(5):
        wide.iloc[-1, i] = np.nan

    ref = ru.market_reference_day(wide)
    assert ref == wide.index[-2], f"参考日应回退到倒数第二个交易日，实际 {ref}"

    got = ru.vectorized_weighted_return(wide)
    assert got.notna().sum() == 20, f"池子被腰斩了：只剩 {got.notna().sum()} 只"


def test_reference_day_is_the_last_row_when_the_market_is_covered():
    """全市场都有数据时，参考日就是最后一行（不打折、不回退）。"""
    wide = _wide_from_closes(A=_flat(400), B=_flat(400), C=_flat(400))
    assert ru.market_reference_day(wide) == wide.index[-1]


def test_reference_day_skips_a_holiday_row():
    """
    末尾是美股节假日（只有零星标的有 bar）时，参考日必须跳过它。

    节假日是**真实信息**不能删，但它不是「交易日」——
    参考日落在节假日上会让那一行成为「最新交易日」，绝大多数票都落选。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(20)}
    wide = _wide_from_closes(**syms)
    for i in range(18):  # 节假日：20 只里只有 2 只成交
        wide.iloc[-1, i] = np.nan

    assert ru.market_reference_day(wide) == wide.index[-2]


def test_reference_day_is_none_when_the_table_is_empty():
    empty = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"), columns=["A"], dtype=float)
    assert ru.market_reference_day(empty) is None


def test_pool_is_measured_at_the_reference_day():
    """
    参考日之前的行情才是「现价」—— 末行那几天的价格不能进计算。

    否则参考日回退到 10-22、算出来的却是 10-23 的收益，
    等于拿未来的一天的涨幅去排名。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i, drift=0.0) for i in range(20)}
    wide = _wide_from_closes(**syms)
    # 末行只有 5 只票有数据（= 只刷新了 live pool 那一小撮），并给它们一个暴涨
    for i in range(5, 20):
        wide.iloc[-1, i] = np.nan
    for i in range(5):
        wide.iloc[-1, i] = wide.iloc[-2, i] * 10

    got = ru.vectorized_weighted_return(wide)
    assert got.notna().all()
    # 平价序列的加权收益必须是 0；用 10 倍末行会把 R63 撑到 +900%
    assert got.abs().max() < 0.01, f"参考日之后的行情混进来了：{got.abs().max()}"


def test_sparse_trailing_row_does_not_collapse_pool():
    """
    末尾只有 1 只票有数据的行必须被砍掉。

    这种行会把「最后一个交易日」对齐到绝大多数票都没有的位置，
    结果整个池子塌成 1 只 —— 不报任何错，只是 RS Rating 全部变成同一个值。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    stray = wide.index[-1] + pd.Timedelta(days=1)
    wide.loc[stray, "S0"] = 999.0  # 只有 S0 有这一行

    got = ru.vectorized_weighted_return(wide)
    assert got.notna().sum() == 10, f"池子塌了：只剩 {got.notna().sum()} 只"


def test_trim_sparse_tail_keeps_dense_rows():
    wide = _wide_from_closes(A=_flat(400), B=_flat(400))
    trimmed = ru.trim_sparse_tail(wide)
    assert trimmed.equals(wide)


def test_trim_sparse_tail_drops_partial_row():
    """10 列里只剩 2 列的尾部行 < 中位数的一半，必须砍掉。"""
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    for i in range(8):
        wide.iloc[-1, i] = np.nan

    trimmed = ru.trim_sparse_tail(wide)
    assert len(trimmed) == len(wide) - 1


def test_trim_sparse_tail_keeps_row_with_minor_halts():
    """10 列里缺席 1 只，仍在中位数一半以上，是正常休市不是坏行。"""
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    wide.iloc[-1, 0] = np.nan

    trimmed = ru.trim_sparse_tail(wide)
    assert len(trimmed) == len(wide)


def test_trim_sparse_tail_tolerates_one_day_halt():
    """
    尾部普遍停牌的交易日不该被砍。

    中位数口径下，少量票缺席（少数票休市）仍在半数以上，照常保留 ——
    被砍的只能是「几乎全体都没有」的那种行。
    """
    syms = {f"S{i}": _flat(400, start=100.0 + i) for i in range(10)}
    wide = _wide_from_closes(**syms)
    for col in ("S0", "S1"):
        wide.loc[wide.index[-1], col] = np.nan

    trimmed = ru.trim_sparse_tail(wide)
    assert len(trimmed) == len(wide)


def test_bars_back_frame_anchors_each_symbol_at_its_own_last_bar():
    """
    第 k 行 = 该 symbol 自己的倒数第 k+1 根 bar。

    NaN 只允许出现在**尾部**。这条是整套对齐的地基：pandas 2.x 的 stack()
    会保留 NaN 行，不 dropna 的话数出来的是日历行数，这张表会悄悄退化成
    日历表本身 —— 而且结果看着完全正常。
    """
    wide = _ragged_wide(FULL=_flat(400), SHORT=_flat(300))
    bars = ru.to_bars_back_frame(wide)

    assert len(bars) == 400
    assert bars["FULL"].notna().all()
    assert bars["SHORT"].notna().sum() == 300
    assert bars["SHORT"].notna().values[:300].all(), "NaN 只该出现在尾部"
    # 两只票都以「自己的最后一根」为第 0 行
    assert bars["FULL"].iloc[0] == pytest.approx(499.0)
    assert bars["SHORT"].iloc[0] == pytest.approx(399.0)
    assert pd.isna(bars["SHORT"].iloc[300])


def test_bars_back_frame_anchors_at_each_symbols_own_tail():
    """
    中间休市一天，第 k 行必须仍然是「**它自己**的倒数第 k+1 根 bar」。

    这正是「按正数第 i 根对齐」的毛病：那条轴的末端由 bar 数最多的票决定，
    比它少一根的票整体偏移一个交易日 —— 数值看着完全正常。
    """
    wide = _wide_from_closes(FULL=_flat())
    wide["GAPPY"] = _with_gap(wide["FULL"], gap_offset=100)
    bars = ru.to_bars_back_frame(wide)
    own = wide["GAPPY"].dropna()

    assert bars["GAPPY"].notna().sum() == len(own) == 399
    for k in (0, 1, 63, 126, 189, 252):
        assert bars["GAPPY"].iloc[k] == pytest.approx(own.iloc[-(k + 1)]), f"第 {k} 根错位"


def test_stale_symbol_excluded_but_scalar_keeps_it():
    """
    把「宽表比标量更严」这条**刻意**的差异写死成契约。

    标量路径会用三天前的收盘价当现价；宽表要求最后一个交易日有 bar。
    这不是 bug，是排名池的定义。哪天有人想「对齐」两者，先看这条测试。

    表格用了 20 只票而不是 2 只：参考日的判定是「这一行有没有覆盖全市场」，
    2 只票的表里少 1 只 = 覆盖 50%，跟「半张表被刷新」在结构上无法区分。
    断言本身没变，标的仍然是「落后一天以上的票落选、标量路径仍保留它」。
    """
    syms = {f"F{i}": _flat(400, start=100.0 + i) for i in range(19)}
    syms["STALE"] = _flat(400)
    wide = _wide_from_closes(**syms)
    for offset in (1, 2, 3):  # 停在三天前
        wide.loc[wide.index[-offset], "STALE"] = np.nan
    got = ru.vectorized_weighted_return(wide)

    assert not pd.isna(got["F0"])
    assert pd.isna(got["STALE"])
    assert _compute_weighted_return(_as_price_df(wide, "STALE"), AS_OF) is not None


def test_price_span_covers_the_whole_253_bar_window():
    """
    量级检查必须覆盖**整个** 253 根窗口 —— 也就是算 R252 用的那段。

    只看最后 63 根（R63 那段）会漏掉更早的量级崩坏：SXTC 就是
    「早期价格极高、近期 0.05 元」这种形状，只看尾部根本看不出来。

    同时守住窗口外沿：iloc[-254]（R252 基准价再往前一根）的崩坏**不该**
    影响入选 —— 那个价格根本不参与任何计算。
    """
    wide = _wide_from_closes(
        AT_253=_with_span_at(253, ru.MAX_PRICE_SPAN * 10),   # 窗口最老一根，R252 基准价
        AT_254=_with_span_at(254, ru.MAX_PRICE_SPAN * 10),   # 窗口外一根
        CLEAN=_flat(),
    )
    got = ru.vectorized_weighted_return(wide)
    assert pd.isna(got["AT_253"]), "窗口最老一根的量级崩坏没被抓到"
    assert not pd.isna(got["AT_254"]), "窗口外的量级崩坏不该牵连这只票落选"
    assert not pd.isna(got["CLEAN"])


def test_dirty_window_constant_matches_rs_module():
    """
    两张表的跳空窗口常量必须一致（10W / 10D 两条回看线仍在用）。

    注意排名池本身已不再用跳空做门槛（改用 MAX_PRICE_SPAN），
    但 rs.py 的 _compute_10w_return / _compute_10d_return 还在用，
    常量必须留着。
    """
    assert ru.DIRTY_WINDOW == DIRTY_WINDOW_WEIGHTED == 254


# ── 入选规则 ─────────────────────────────────────────────────────

def test_gap_before_window_still_matches_scalar():
    """
    中间的日历缺口不该让这只票的结果跑偏。

    缺口只影响「数第几根 bar」：它的 bar 序列比没缺口的少一根，
    所以 R* 锚点跟着少一根是**对的**—— 标量看到的也是同一串 bar。
    """
    wide = _wide_from_closes(FULL=_flat())
    wide["GAPPY"] = _with_gap(wide["FULL"], gap_offset=100)

    got = ru.vectorized_weighted_return(wide)
    _assert_same(got["GAPPY"], _compute_weighted_return(_as_price_df(wide, "GAPPY"), AS_OF), "GAPPY")
    _assert_same(got["FULL"], _compute_weighted_return(_as_price_df(wide, "FULL"), AS_OF), "FULL")


def test_missing_last_bar_is_stale_and_excluded():
    """最后一个交易日没有 bar = 数据落后，只能落选。"""
    syms = {f"F{i}": _flat(400, start=100.0 + i) for i in range(19)}
    wide = _wide_from_closes(**syms)
    wide["STALE"] = _with_gap(wide["F0"], gap_offset=1)

    got = ru.vectorized_weighted_return(wide)
    assert not pd.isna(got["F0"])
    assert pd.isna(got["STALE"])


def test_stale_symbol_excluded():
    """
    落后一天的票必须落选。

    这是「绝不 ffill」的全部意义：若沿用三天前的收盘价当现价，
    它的加权收益会以一个陈旧价格进入百分位排名。

    用 20 只票而不是 2 只：参考日的判定看的是「这一行覆盖了全市场吗」，
    2 只票的表里少 1 只（50%）跟「只刷新了一半」在结构上分不开。
    """
    syms = {f"F{i}": _flat(400, start=100.0 + i) for i in range(19)}
    syms["Stale"] = _flat(400)
    wide = _wide_from_closes(**syms)
    wide.iloc[-1, wide.columns.get_loc("Stale")] = np.nan
    got = ru.vectorized_weighted_return(wide)
    assert not pd.isna(got["F0"])
    assert pd.isna(got["Stale"])


def test_no_forward_fill_is_performed(tmp_path):
    """
    缺口必须原样留成 NaN，**不能**被前值填掉。

    这是整个模块最容易被人「顺手优化」掉的一条：一旦有人给宽表加 ffill，
    落后三周的票就会带着三周前的价格进排名，而且不会有任何报错。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    holed = _frame(_flat())
    holed.iloc[-5, holed.columns.get_loc("close")] = np.nan
    pd.to_pickle(holed, cache / "HOLED.pkl")

    out = tmp_path / "closes.pkl"
    built = ru.build_universe_closes(symbols=["HOLED"], cache_dir=cache, out_path=out)
    loaded = ru.load_universe_closes(out)

    assert built["HOLED"].isna().sum() == 1
    assert loaded["HOLED"].isna().sum() == 1
    # 缺口前一根 = 100 + 394；缺口后一根必须是 NaN，不能被 494 填掉
    assert loaded["HOLED"].iloc[-6] == pytest.approx(100.0 + 394.0)
    assert pd.isna(loaded["HOLED"].iloc[-5])


def test_short_history_returns_all_nan():
    wide = _wide_from_closes(SHORT=_flat(n=100))
    got = ru.vectorized_weighted_return(wide)
    assert got.isna().all()


def test_history_threshold_matches_min_history_days():
    """
    bar 数门槛必须与标量的 MIN_HISTORY_DAYS 一致：252 根不够，253 根刚好够。

    R252 的基准价是倒数第 253 根，所以 253 根是能算出数的最小值 ——
    宽表与标量在这个边界上必须给出同一个答案。
    """
    assert MIN_HISTORY_DAYS == 253
    assert ru.vectorized_weighted_return(_wide_from_closes(A=_flat(n=252))).isna().all()
    assert not ru.vectorized_weighted_return(_wide_from_closes(A=_flat(n=253))).isna().any()


def test_history_threshold_matches_scalar_at_boundary():
    """边界上逐只对拍 —— 252/253 根这个坎两边必须一致。"""
    for n in (250, 252, 253, 254):
        wide = _wide_from_closes(A=_flat(n=n))
        got = ru.vectorized_weighted_return(wide)
        _assert_same(got["A"], _compute_weighted_return(_as_price_df(wide, "A"), AS_OF), f"n={n}")


# ── 指数规整 ─────────────────────────────────────────────────────

def test_index_is_normalized_to_utc_midnight(tmp_path):
    """pkl 索引常带盘中时间，宽表必须归一到 UTC 零点，否则同一天会散成多列行。"""
    day = pd.Timestamp("2026-10-21", tz="UTC")
    intraday = pd.DatetimeIndex(
        [day + pd.Timedelta(hours=4), day + pd.Timedelta(hours=13, minutes=30)]
    )
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=intraday)

    s = ru._read_close_series_from_df(df)
    assert len(s) == 1
    assert s.index[0] == day


def test_naive_index_is_localized_to_utc(tmp_path):
    naive = pd.DatetimeIndex(["2026-10-20", "2026-10-21"])
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=naive)
    s = ru._read_close_series_from_df(df)
    assert s.index.tz is not None
    assert str(s.index.tz) == "UTC"


def test_non_price_pkl_is_skipped(tmp_path):
    """data/cache 里混着 EPS 的 dict 型 pkl，不能当成行情读。"""
    pd.to_pickle({"eps": 1.0}, tmp_path / "eps_AAPL.pkl")
    pd.to_pickle({"x": 1.0}, tmp_path / "polygon_AAPL.pkl")
    assert ru.discover_symbols(tmp_path) == []


def test_price_pkl_without_close_column_skipped(tmp_path):
    pd.to_pickle(pd.DataFrame({"open": [1.0]}), tmp_path / "WEIRD.pkl")
    assert ru.discover_symbols(tmp_path) == ["WEIRD"]
    assert ru._read_close_series(tmp_path / "WEIRD.pkl") is None


# ── 落盘往返 + 增量 ──────────────────────────────────────────────

def test_build_then_load_roundtrip(tmp_path):
    out = tmp_path / "closes.pkl"
    cache = tmp_path / "cache"
    cache.mkdir()
    pd.to_pickle(_frame(_flat()), cache / "AAA.pkl")
    pd.to_pickle(_frame(_flat(start=50.0)), cache / "BBB.pkl")

    built = ru.build_universe_closes(
        symbols=["AAA", "BBB"], cache_dir=cache, out_path=out, tail_rows=400
    )
    loaded = ru.load_universe_closes(out)

    assert set(built.columns) == {"AAA", "BBB"}
    assert loaded.equals(built)
    assert (loaded["AAA"] > loaded["BBB"]).all()


def test_build_never_writes_to_cache_dir(tmp_path):
    """「绝不删改行情缓存」是硬红线：构造宽表后 cache 目录必须一字未动。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    src = cache / "AAA.pkl"
    pd.to_pickle(_frame(_flat()), src)
    before = src.read_bytes()

    ru.build_universe_closes(
        symbols=["AAA"], cache_dir=cache, out_path=tmp_path / "closes.pkl"
    )
    assert src.read_bytes() == before
    assert sorted(p.name for p in cache.iterdir()) == ["AAA.pkl"]


def test_tail_rows_truncates(tmp_path):
    out = tmp_path / "closes.pkl"
    cache = tmp_path / "cache"
    cache.mkdir()
    pd.to_pickle(_frame(_flat(n=1000)), cache / "AAA.pkl")

    wide = ru.build_universe_closes(
        symbols=["AAA"], cache_dir=cache, out_path=out, tail_rows=300
    )
    assert len(wide) == 300


def test_load_missing_file_returns_none(tmp_path):
    assert ru.load_universe_closes(tmp_path / "nope.pkl") is None


def test_load_corrupt_file_returns_none(tmp_path):
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(b"not a pickle")
    assert ru.load_universe_closes(bad) is None


def test_update_store_upserts_without_dropping_columns(tmp_path):
    """增量更新必须保留旧列，只替换本次传入的那些 —— 否则每轮 live 都会缩小池子。"""
    out = tmp_path / "closes.pkl"
    cache = tmp_path / "cache"
    cache.mkdir()
    pd.to_pickle(_frame(_flat()), cache / "AAA.pkl")
    pd.to_pickle(_frame(_flat()), cache / "BBB.pkl")
    ru.build_universe_closes(
        symbols=["AAA", "BBB"], cache_dir=cache, out_path=out, tail_rows=400
    )

    # 只带 AAA 回来（BBB 这轮没抓到）
    ru.update_universe_store({"AAA": _frame(_flat(start=200.0))}, path=out, tail_rows=400)

    loaded = ru.load_universe_closes(out)
    assert set(loaded.columns) == {"AAA", "BBB"}
    assert loaded["AAA"].iloc[-1] == pytest.approx(200.0 + 399.0)


def test_update_store_bootstraps_when_no_store(tmp_path):
    out = tmp_path / "closes.pkl"
    built = ru.update_universe_store({"AAA": _frame(_flat())}, path=out, tail_rows=400)
    assert list(built.columns) == ["AAA"]
    assert out.exists()


def test_update_store_accepts_a_symbol_that_is_not_in_the_table_yet(tmp_path):
    """
    宽表里还没有的**新代码**第一次被抓到时，更新不许炸。

    旧写法 `existing.drop(columns=list(fresh))` 在 fresh 里有宽表没有的代码时
    抛 KeyError。live run 里它被 try/except 吞掉，只打一行
    「宽表更新失败（将回退逐只 pkl 计算）」—— 于是这只票**永远进不了宽表**，
    也就永远进不了排名池。静默且致命。
    """
    out = tmp_path / "closes.pkl"
    cache = tmp_path / "cache"
    cache.mkdir()
    pd.to_pickle(_frame(_flat()), cache / "AAA.pkl")
    ru.build_universe_closes(
        symbols=["AAA"], cache_dir=cache, out_path=out, tail_rows=400
    )

    # NEW 是宽表里没有的新代码
    ru.update_universe_store(
        {"AAA": _frame(_flat(start=200.0)), "NEW": _frame(_flat())},
        path=out,
        tail_rows=400,
    )

    loaded = ru.load_universe_closes(out)
    assert set(loaded.columns) == {"AAA", "NEW"}


def test_update_store_drops_non_price_frames(tmp_path):
    out = tmp_path / "closes.pkl"
    built = ru.update_universe_store(
        {"AAA": _frame(_flat()), "BAD": {"eps": 1.0}}, path=out, tail_rows=400
    )
    assert list(built.columns) == ["AAA"]


def test_build_raises_when_no_readable_pkl(tmp_path):
    cache = tmp_path / "empty"
    cache.mkdir()
    with pytest.raises(RuntimeError, match="没有可读的行情"):
        ru.build_universe_closes(symbols=["A", "B"], cache_dir=cache, out_path=tmp_path / "o.pkl")