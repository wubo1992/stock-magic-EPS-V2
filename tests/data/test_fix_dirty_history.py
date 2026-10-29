"""Tests for tools/fix_dirty_history.py —— 脏数据重下 + 隔离提升。

这个脚本会动用户的行情缓存，所以三条红线各自单独测：

1. **绝不删除** —— 替换一律是 `data/cache/` → `data/cache_quarantine/` 的**改名移动**，
   回滚就一条 `mv`。测试用 mtime / 目录归属断言，没有任何一条路径会 unlink。
2. **影子目录隔离** —— 抓取阶段只写 `data/cache_rsfix/`，对 `data/cache/` 零写入。
3. **失败要可见** —— 清单里必须写清楚每只票是「通过 / 失败+原因 / 没动」，
   不能只留一个「成功」计数。

批量重试也有独立测试：`_alpaca_fetch_batch` 累计剔除 10 个无效代码后就会
**整批丢弃**（返回 {}），上万个代码里混着 warrant/unit，不做二分拆批
就会静悄悄丢掉整批 200 只。
"""
import json
import os
import time

import numpy as np
import pandas as pd
import pytest

from tools.fix_dirty_history import (
    DIRTY_WINDOW,
    MIN_BARS,
    dual_source_conflicts,
    fetch_resilient,
    find_dirty_symbols,
    find_stretched_symbols,
    tail_jump_count,
    validate_frame,
    window_calendar_span,
)

AS_OF = pd.Timestamp("2026-10-22", tz="UTC")


def _frame(n=400, start=100.0, end=200.0, end_date="2026-10-21"):
    idx = pd.date_range(end=end_date, periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": np.linspace(start, end, n),
            "high": np.linspace(start, end, n) * 1.01,
            "low": np.linspace(start, end, n) * 0.99,
            "close": np.linspace(start, end, n),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _gapped_frame(gap_days=400, tail_bars=252, end_date="2026-10-21"):
    """
    造一份「中间断了一大截」的行情。

    一共 253 根 bar：第 1 根在 gap_days 天之前，后 252 根是连续的日线 ——
    于是「253 根 bar」横跨了远超一年的日历时间。这正是 2026-10-26 实测到的
    440 只票的形状：数据中间少了一大截，RS 的 252 日基准价取到了半年前的
    旧价，R252 直接算飞（AKTS 的 wret=41.61，全市场第一）。
    """
    tail = pd.date_range(end=end_date, periods=tail_bars, freq="D", tz="UTC")
    head = pd.DatetimeIndex([tail[0] - pd.Timedelta(days=gap_days)])
    idx = head.append(tail)
    n = len(idx)
    return pd.DataFrame(
        {
            "open": np.linspace(100.0, 200.0, n),
            "high": np.linspace(100.0, 200.0, n) * 1.01,
            "low": np.linspace(100.0, 200.0, n) * 0.99,
            "close": np.linspace(100.0, 200.0, n),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


# ════════════════════════════════════════════════════════════════
# 校验：新数据必须比旧数据干净，否则提升等于白干
# ════════════════════════════════════════════════════════════════


def test_clean_frame_passes_validation():
    ok, reason = validate_frame(_frame(), AS_OF)
    assert ok is True, reason


def test_frame_with_too_few_bars_is_rejected():
    """
    少于 253 根 bar 的数据无法算 R252（基准价在 iloc[-253]）。

    注意是 253 不是 252 —— 取不到基准价就算不出加权收益。
    """
    ok, reason = validate_frame(_frame(n=MIN_BARS - 1), AS_OF)
    assert ok is False
    assert "bar" in reason


def test_frame_exactly_at_bar_threshold_passes():
    ok, reason = validate_frame(_frame(n=MIN_BARS), AS_OF)
    assert ok is True, reason


def test_real_jump_in_tail_is_accepted():
    """
    尾部有 ±50% 跳空**必须放过** —— 这正是上一版校验犯的错。

    重下走 Alpaca `adjustment="all"`，数据已复权。此时窗口里的 ±50%
    不是没复权好，而是**真实行情**。实测反证：AAOI / AARD / AGIO 的
    raw 与 all 逐 bar 比对零差异（历史上根本没拆过股），它们的跳空
    当日成交量比 8.5x~1091x、振幅 9%~68% —— 100% 是消息面推动的真涨跌幅。

    旧校验在这里拒绝它们，894 只重下候选里 548 只因此被毙，其中 539 只是
    普通股（多为生物科技微盘）。拿干净数据当脏数据扔掉，比留着旧数据更糟。

    跳空仍然要**看得见**（见 tail_jump_count 的测试与清单里的 jumps 字段），
    但看得见不等于拒绝。
    """
    df = _frame()
    df.iloc[-30, df.columns.get_loc("close")] = df.iloc[-30]["close"] * 0.4
    ok, reason = validate_frame(df, AS_OF)
    assert ok is True, f"已复权数据里的真实跳空被误判为脏数据: {reason}"


def test_dirty_window_is_254():
    """窗口常量本身也锁死，双保险。"""
    assert DIRTY_WINDOW == 254


# ════════════════════════════════════════════════════════════════
# 时间戳必须归一到 UTC 零点
# ════════════════════════════════════════════════════════════════


def test_alpaca_symbol_keeps_dots():
    """
    Alpaca 的分级股代码用的是**点**，不是连字符（2026-10-23 实测）：

        BRK.A -> 2,528 根 bar        BRK-A -> APIError "invalid symbol"

    早先这里写的是「点换连字符」，方向正好反了。后果不是报错而是**静默**：
    Alpaca 对不存在的代码只是让整批请求失败，二分拆批之后单独重试仍是空，
    看起来就像「这些票没有历史数据」。

    2026-10-23 那轮重下的 894 只脏票里恰好没有带点代码，所以没造成实际损失 ——
    但函数还在，下次用到就会再踩一遍。锁死它。
    """
    from tools.fix_dirty_history import _alpaca_symbol

    assert _alpaca_symbol("BRK.A") == "BRK.A"
    assert _alpaca_symbol("AAPL") == "AAPL"


class _FakeBars:
    def __init__(self, df):
        self.df = df


class _FakeClient:
    """Alpaca 会按交易所时区给时间戳：IEX 是 04:00 UTC、SIP 是 05:00 UTC。"""

    def __init__(self, df):
        self._df = df

    def get_stock_bars(self, req):
        return _FakeBars(self._df)


def test_fetch_normalizes_timestamps_to_utc_midnight():
    """
    Alpaca 的日线时间戳带交易所时区（IEX 04:00 UTC / SIP 05:00 UTC），
    而本项目 pkl 的约定是 **UTC 零点**。不归一的后果是静默的：

      - `data/rs.py::_slice_to_date` 用 `df.index <= as_of`，as_of 是 00:00，
        于是当天那根 bar 被整根丢掉 —— 表现为「刚提升的数据又滞后一天」
      - 宽表索引变成 05:00，与其它 pkl 的 00:00 对不上，join 全断
      - 拿新文件与旧文件求 index 交集恒为空，根本看不出差异

    所以归一必须在落盘前做，不是下游的事。
    """
    from tools.fix_dirty_history import _make_alpaca_fetch

    idx = pd.DatetimeIndex(["2026-10-20", "2026-10-21", "2026-10-22"])
    raw = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": [9.0, 10.0, 11.0], "volume": 1e6},
        index=idx.tz_localize("UTC") + pd.Timedelta(hours=5),  # SIP 的真实形态
    )
    raw.index = pd.MultiIndex.from_product([["AAPL"], raw.index], names=["symbol", "timestamp"])

    out = _make_alpaca_fetch(_FakeClient(raw), pd.Timestamp("2020-01-01").to_pydatetime())(["AAPL"])

    got = out["AAPL"].index
    assert all(h == 0 for h in got.hour), f"时间戳未归一到 UTC 零点: {list(got)}"
    assert list(got.strftime("%Y-%m-%d")) == ["2026-10-20", "2026-10-21", "2026-10-22"]


def test_fetch_keeps_naive_index_working():
    """时间戳没有时区时也要能处理（归一到 UTC 零点，不丢数据）。"""
    from tools.fix_dirty_history import _make_alpaca_fetch

    idx = pd.DatetimeIndex(["2026-10-21", "2026-10-22"])
    raw = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": [9.0, 10.0], "volume": 1e6},
        index=idx,
    )
    raw.index = pd.MultiIndex.from_product([["AAPL"], raw.index], names=["symbol", "timestamp"])

    out = _make_alpaca_fetch(_FakeClient(raw), pd.Timestamp("2020-01-01").to_pydatetime())(["AAPL"])

    assert list(out["AAPL"].index) == [
        pd.Timestamp("2026-10-21", tz="UTC"),
        pd.Timestamp("2026-10-22", tz="UTC"),
    ]


# ════════════════════════════════════════════════════════════════
# 跳空可见性：清单必须能回答「这批提升上去的文件里有几处跳空」
# ════════════════════════════════════════════════════════════════


def test_tail_jump_count_sees_the_jump_into_iloc_minus_253():
    """
    「跳进 iloc[-253] 的那一次」必须数得到 —— 那是 R252 基准价所在的位置。

    造法要点：**改 iloc[-254] 的值**，不要改 iloc[-253]。
    改 -253 会在它两侧各造一个断点，下一跳 +150% 照样能抓到，
    于是 253 窗口也能「通过」—— 测不出差别。
    改 -254 只影响「-254 → -253」这一跳，它是否可见就完全由窗口长度决定：

        窗口 254：slice 从 -254 开始，-254 落在 pct_change 的 NaN 槽，
                  -253 那一跳被算出来 → 数得到
        窗口 253：slice 从 -253 开始，-253 自己落在 NaN 槽 → 看不见

    所以这个断言正是把窗口钉死在 254 的那根钉子。
    """
    df = _frame()
    df.iloc[-254, df.columns.get_loc("close")] = df.iloc[-254]["close"] * 0.4
    assert tail_jump_count(df) == 1


def test_tail_jump_count_ignores_jump_one_step_outside_the_window():
    """
    窗口**外**的跳空不该算数。

    边界在 iloc[-255]：改那里的值只影响 -256 → -255 那一跳，
    不触及 R252 的基准价 iloc[-253]。

    （iloc[-254] 不行 —— 在那里改值会把 iloc[-253] 抬高 150%，
    基准价一样被污染。所以窗口外沿是 -255，不是 -254。）

    更早的跳空同理：3 年前的一次大涨不该让今天这只票落选，
    这正是 Step 2 修掉的 bug。
    """
    df = _frame()
    df.iloc[-255, df.columns.get_loc("close")] = df.iloc[-255]["close"] * 0.4
    assert tail_jump_count(df) == 0


def test_tail_jump_count_is_zero_for_clean_data():
    assert tail_jump_count(_frame()) == 0


def test_tail_jump_count_tolerates_short_history():
    """不足一窗的数据不该崩，数得出来的才算。"""
    assert tail_jump_count(_frame(n=50)) == 0


def test_frame_stale_last_bar_is_rejected():
    """
    最后一根 bar 落后于最新交易日太多就不能用。

    否则一只停牌票会带着三周前的收盘价进排名池。
    """
    ok, reason = validate_frame(_frame(end_date="2026-09-01"), AS_OF)
    assert ok is False
    assert "滞后" in reason


def test_frame_one_day_behind_is_rejected():
    """as_of 前一个交易日 = 最新可用交易日，应该放过；早于它就不行。"""
    ok, _ = validate_frame(_frame(end_date="2026-10-20"), AS_OF)
    assert ok is False, "落后一个交易日说明停牌/退市，不能拿旧价当现价"


def test_a_frame_with_an_absurd_price_span_is_rejected():
    """
    提升前的复检必须跟 RS 排名池用**同一套**排脏判据。

    `find_dirty_symbols` 找脏票时用的是「窗口内 max/min 价格量级 > 1000 倍」
    （`data/rs.py::_price_span_absurd`）。但 `validate_frame` 只查列齐全、
    bar 数够、末根不滞后 —— **没有查价格量级**。后果是：
    一份量级荒谬的新数据照样被标成 ok、照样被提升上去，
    而 RS 排名池转头又把它拒掉。清单写着「ok」，池子里却没有它。

    实测（2026-10-26）：BDAI / CDT / RUBI 等 35 只就是这样上岗位的 ——
    Alpaca 的 `adjustment="all"` 对它们给出 521250 → 1.99 这种 195,508 倍
    的价格范围（拆股复权因子本身是坏的），RS 管道拒收，但清单说 ok。

    修法不是「把这些票救回来」（数据源本身坏），而是**别再假装合格**。
    """
    df = _frame(n=400)
    tail = 1000.0 * (0.4 / 1000.0) ** (np.arange(253) / 252.0)
    for col in ("open", "high", "low", "close"):
        df.iloc[:, df.columns.get_loc(col)] = 1000.0
        df.iloc[-253:, df.columns.get_loc(col)] = tail

    ok, reason = validate_frame(df, AS_OF)
    assert ok is False, "量级荒谬的数据被当成合格数据提升了"
    assert "量级" in reason


def test_frame_missing_required_column_is_rejected():
    df = _frame().drop(columns=["volume"])
    ok, reason = validate_frame(df, AS_OF)
    assert ok is False
    assert "volume" in reason


def test_empty_frame_is_rejected():
    ok, reason = validate_frame(_frame(n=0), AS_OF)
    assert ok is False


# ════════════════════════════════════════════════════════════════
# 同日双源污染
# ════════════════════════════════════════════════════════════════
#
# 同一个交易日出现两条 bar，只可能是两个数据源各写了一遍
# （Yahoo 的 00:00 UTC + Alpaca 的 04:00 UTC）。
#
# 收盘价差了 >2% = 两套价格基准混在一起：
#   - Yahoo 的 adjclose 是**分红复权**后的
#   - Alpaca 的 adjustment="all" 是**拆股复权**、不含分红
# 差 0.5% 是分红级别，差 100 倍是反向拆股级别。
#
# 这种文件算出来的加权收益是拿两套基准做比值 —— 名义上「没报错」，
# 实际上 R63/R126/R189/R252 全部带偏。而 ±50% 跳空那道闸门**看不见**
# 它：TWG 的价格量级只有 157 倍，稳稳地通过了闸门。


def _dual_source_frame(n=400, conflict_at=(-1,), gap=0.05, end_date="2026-10-21"):
    """
    造一个「同一天两条 bar」的行情帧。

    conflict_at —— 冲突发生在倒数第几根 bar（0 = 最后一根）。一根 bar
    被拆成两条：00:00 UTC 的 Yahoo 那条 + 04:00 UTC 的 Alpaca 那条，
    后者按 gap 比例偏离（模拟拆股/分红复权基准不同）。
    """
    df = _frame(n=n, end_date=end_date)
    extra = []
    for offset in conflict_at:
        pos = len(df) + offset
        day = df.index[pos]
        row = df.iloc[pos].copy()
        for col in ("open", "high", "low", "close"):
            row[col] = row[col] * (1.0 + gap)
        extra.append(row)
    extra = pd.DataFrame(extra, index=df.index[[len(df) + o for o in conflict_at]])
    # Yahoo 写的是 00:00 UTC，Alpaca 写的是 04:00 UTC
    extra.index = extra.index + pd.Timedelta(hours=4)
    return pd.concat([df, extra]).sort_index()


def test_dual_source_conflict_is_detected():
    """同一交易日两条 bar、收盘价差 5% = 两套价格基准混用。"""
    conflicts = dual_source_conflicts(_dual_source_frame(), AS_OF)
    assert conflicts, "同日双源冲突没被认出来"


def test_a_small_same_day_difference_is_not_a_conflict():
    """两条 bar 只差 0.3%（分红调整级别）不算污染。"""
    assert dual_source_conflicts(_dual_source_frame(gap=0.003), AS_OF) == []


def test_dual_source_conflicts_outside_the_window_are_ignored():
    """窗口外的旧冲突不影响今天这只票 —— 和 ±50% 跳空那道闸门同一口径。"""
    df = _dual_source_frame(n=900, conflict_at=(-880,))
    conflicts = dual_source_conflicts(df, AS_OF)
    assert conflicts == [], f"窗口外的冲突被算进来了：{conflicts}"


def test_find_dirty_symbols_flags_dual_source_conflicts(tmp_path):
    """双源污染的票必须进重下清单 —— 它算出来的 RS 是错的。"""
    (tmp_path / "GOOD.pkl").write_bytes(pickle_dumps(_frame()))
    (tmp_path / "MIXED.pkl").write_bytes(pickle_dumps(_dual_source_frame()))

    assert find_dirty_symbols(tmp_path, AS_OF) == ["MIXED"]


# ════════════════════════════════════════════════════════════════
# 找脏票
# ════════════════════════════════════════════════════════════════


def test_find_dirty_symbols_flags_only_window_dirty(tmp_path):
    """
    只有**窗口内**的脏数据才算数。

    窗口外（3 年前）的一次价格量级异常不该让今天这只票落选 ——
    这正是 Step 2 修掉的 bug：改之前 927 只票因此被误杀。

    窗口 = 最后 253 根 bar（RS 算 R252 用到的那一段），与
    `data/rs.py::_price_span_absurd` 的窗口一致。
    """
    good = _frame(n=600)  # 窗口外有一段量级异常
    good.iloc[100, good.columns.get_loc("close")] = good.iloc[100]["close"] * 0.001
    bad = _frame()
    bad.iloc[-10, bad.columns.get_loc("close")] = bad.iloc[-10]["close"] * 0.0001

    (tmp_path / "GOOD.pkl").write_bytes(pickle_dumps(good))
    (tmp_path / "BAD.pkl").write_bytes(pickle_dumps(bad))

    assert find_dirty_symbols(tmp_path, AS_OF) == ["BAD"]


def test_find_dirty_symbols_does_not_flag_a_real_market_jump(tmp_path):
    """
    真实跳空**不是**脏数据 —— 重下清单必须和 RS 排名池用同一套判据。

    RS 排名池的排脏门槛是「窗口内 max/min 价格量级 > 1000 倍」
    （`data/rs.py::_price_span_absurd`），不是「单日 ±50% 涨跌幅」。
    两套口径不一致的后果是**每次扫描都重下同一批票**：
    重下不能消除真实跳空（AAOI / AARD / AGIO 的 raw 与 all 逐 bar 比对
    零差异 —— 历史上根本没拆过股），所以这批票会被反复列进清单、
    反复下载、反复替换，而排名池从头到尾没变。

    实测（2026-10-23）：旧口径列 1,839 只，其中 409 只只是真实跳空；
    换成 RS 管道的口径后是 1,439 只（= 1,430 只双源 + 43 只量级荒谬）。
    """
    df = _frame()
    df.iloc[-10, df.columns.get_loc("close")] = df.iloc[-10]["close"] * 0.4  # -60% 跳空

    (tmp_path / "JUMP.pkl").write_bytes(pickle_dumps(df))

    assert find_dirty_symbols(tmp_path, AS_OF) == [], "真实跳空被当成脏数据了"


def test_find_dirty_symbols_flags_an_absurd_price_span(tmp_path):
    """
    价格量级荒谬（max/min > 1000 倍）必须进重下清单。

    造的形状是「从 1000 元一路阴跌到 0.4 元」—— 每根 bar 只跌 3%，
    旧的「单日 ±50% 跳空」判据**完全看不见**这种票，
    但 RS 排名池的「窗口内 max/min > 1000 倍」会拒它。
    判据不同步，重下清单就会漏掉 RS 管道真正算不出 RS 的那批票。
    """
    n = 400
    df = _frame(n=n)
    tail = 1000.0 * (0.4 / 1000.0) ** (np.arange(253) / 252.0)
    # 窗口外的 147 根也设成 1000 —— 否则 bar147(≈150) → bar148(1000) 那一下
    # 就是个 +568% 的假跳空，旧的 ±50% 判据会「因为错的原因」通过这个测试。
    for col in ("open", "high", "low", "close"):
        df.iloc[:, df.columns.get_loc(col)] = 1000.0
        df.iloc[-253:, df.columns.get_loc(col)] = tail

    (tmp_path / "SPAN.pkl").write_bytes(pickle_dumps(df))

    assert find_dirty_symbols(tmp_path, AS_OF) == ["SPAN"], "价格量级荒谬的票没进重下清单"


def test_find_dirty_symbols_skips_eps_cache(tmp_path):
    """data/cache 里混着 EPS/Polygon 的 dict 型 pkl，不能当行情读。"""
    (tmp_path / "eps_AAPL.pkl").write_bytes(pickle_dumps({"a": 1}))
    (tmp_path / "polygon_x.pkl").write_bytes(pickle_dumps({"a": 1}))
    (tmp_path / "AAPL.pkl").write_bytes(pickle_dumps(_frame()))
    assert find_dirty_symbols(tmp_path, AS_OF) == []


def test_find_dirty_symbols_survives_corrupt_file(tmp_path):
    """一只票的 pkl 损坏不能让整轮扫描崩掉。"""
    (tmp_path / "BROKEN.pkl").write_bytes(b"not a pickle")
    (tmp_path / "AAPL.pkl").write_bytes(pickle_dumps(_frame()))
    assert find_dirty_symbols(tmp_path, AS_OF) == []


# ════════════════════════════════════════════════════════════════
# 批量重试：二分拆批
# ════════════════════════════════════════════════════════════════


def test_fetch_resilient_splits_batch_when_it_fails():
    """
    一整批返回空时必须拆成两半重试，不能直接放弃。

    `_alpaca_fetch_batch` 累计剔除 10 个无效代码后就整批返回 {}，
    上万个代码里混着 warrant/unit，不拆批就会静悄悄丢掉整批。
    """
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        if len(symbols) > 2:
            return {}  # 模拟「超限整批丢弃」
        return {s: _frame() for s in symbols}

    result = fetch_resilient(
        fetch, ["A", "B", "C", "D"], max_batch=4, min_split=1
    )
    assert sorted(result) == ["A", "B", "C", "D"]
    # 第一次必然是整批（不然不知道它失败），关键是失败之后确实拆了
    assert calls[0] == ["A", "B", "C", "D"]
    assert sorted(len(c) for c in calls[1:]) == [2, 2], f"没有真正拆批: {calls}"


def test_fetch_resilient_isolates_the_bad_symbol():
    """
    一批里混着一只永远取不到的票，好票必须全拿到。

    关键：坏的只有 1 只时不能把另外 199 只一起丢掉。
    """
    def fetch(symbols):
        if "BAD" in symbols:
            return {}
        return {s: _frame() for s in symbols}

    result = fetch_resilient(fetch, ["A", "BAD", "B"], max_batch=3, min_split=1)
    assert sorted(result) == ["A", "B"]
    assert "BAD" not in result


def test_fetch_resilient_recovers_the_lost_half():
    """左半边坏掉时右半边仍要拿到数据 —— 不能因为一半失败就整批报废。"""
    def fetch(symbols):
        if any(s.startswith("X") for s in symbols):
            return {}
        return {s: _frame() for s in symbols}

    result = fetch_resilient(
        fetch, ["X1", "X2", "A", "B"], max_batch=4, min_split=1
    )
    assert sorted(result) == ["A", "B"]


def test_fetch_resilient_does_not_recurse_forever_on_one_symbol():
    """单只票反复失败要收敛，不能无限递归。"""
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {}

    assert fetch_resilient(fetch, ["BAD"], max_batch=1, min_split=1) == {}
    assert len(calls) == 1, "单只票不该被反复重试"


def pickle_dumps(obj) -> bytes:
    import pickle

    return pickle.dumps(obj)


# ════════════════════════════════════════════════════════════════
# 提升：旧数据留档，任何一代历史都不能被覆盖掉
# ════════════════════════════════════════════════════════════════


def test_promoting_never_overwrites_an_existing_backup(tmp_path, monkeypatch):
    """
    留档目录里已有同名备份时，提升必须把它**挪走**而不是覆盖。

    用户红线是「全程不删除任何 data/cache/*.pkl」—— 留档目录里那一份
    也是旧数据，覆盖它等于毁掉一代历史。

    这不是假想场景：第二轮重下时，第一轮留档的 781 份里有 746 份会撞名
    （实测 2026-10-23）。
    """
    import tools.fix_dirty_history as fx

    cache = tmp_path / "cache"
    fix = tmp_path / "fix"
    quarantine = tmp_path / "quarantine"
    for d in (cache, fix, quarantine):
        d.mkdir()

    monkeypatch.setattr(fx, "CACHE_DIR", cache)
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "QUARANTINE_DIR", quarantine)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")

    (cache / "AAA.pkl").write_bytes(pickle_dumps(_frame()))
    (quarantine / "AAA.pkl").write_bytes(pickle_dumps(_frame()))  # 上一轮的备份
    (fix / "AAA.pkl").write_bytes(pickle_dumps(_frame()))

    fx.MANIFEST.write_text(
        json.dumps({"entries": {"AAA": {"status": "ok"}}}), encoding="utf-8"
    )

    fx.promote(AS_OF)

    assert (cache / "AAA.pkl").exists(), "新数据没上岗"
    backups = list(quarantine.rglob("AAA.pkl"))
    assert len(backups) == 2, f"备份只剩 {len(backups)} 份 —— 有一代被覆盖了"
    # 上一轮的备份不能被就地覆盖
    assert (quarantine / "AAA.pkl").exists()
    assert any(p.parent != quarantine for p in backups), "旧备份没有被挪走"


def test_promote_moves_the_old_file_into_quarantine(tmp_path, monkeypatch):
    """旧文件是**改名移动**到留档目录，不是被删掉。"""
    import tools.fix_dirty_history as fx

    cache = tmp_path / "cache"
    fix = tmp_path / "fix"
    quarantine = tmp_path / "quarantine"
    for d in (cache, fix, quarantine):
        d.mkdir()

    monkeypatch.setattr(fx, "CACHE_DIR", cache)
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "QUARANTINE_DIR", quarantine)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")

    old_bytes = pickle_dumps(_frame())
    (cache / "AAA.pkl").write_bytes(old_bytes)
    (fix / "AAA.pkl").write_bytes(pickle_dumps(_frame()))

    fx.MANIFEST.write_text(
        json.dumps({"entries": {"AAA": {"status": "ok"}}}), encoding="utf-8"
    )

    fx.promote(AS_OF)

    assert (quarantine / "AAA.pkl").read_bytes() == old_bytes, "旧数据没原样留档"


def test_an_already_downloaded_symbol_still_lands_in_the_manifest(tmp_path, monkeypatch):
    """
    续跑时**已经在影子目录里**的票也必须进清单。

    原来 `todo` 把它们直接过滤掉，于是 `entries` 里没有它们 ——
    而 `promote()` 只认清单，所以这些票**永远不会被提升**，
    脏数据原封不动留在 cache 里，清单看上去却��完整的。

    实测（2026-10-23 第二轮）：1,439 只里有 4 只（ABTS/ACXP/ADGM/ADVB）
    正好撞上上一轮跑剩下的影子文件，被静默漏掉。
    """
    import tools.fix_dirty_history as fx
    import alpaca.data.historical as ah

    fix = tmp_path / "fix"
    fix.mkdir()
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(ah, "StockHistoricalDataClient", lambda *a, **k: object())

    fetched: list[list[str]] = []

    def fake_fetch(client, start, feed_name="sip"):
        def _f(symbols):
            fetched.append(list(symbols))
            return {}
        return _f

    monkeypatch.setattr(fx, "_make_alpaca_fetch", fake_fetch)

    # 上一轮已经下载好的影子文件
    (fix / "AAA.pkl").write_bytes(pickle_dumps(_frame()))

    entries = fx.collect(["AAA", "BBB"], AS_OF, batch=10, sleep=0)

    assert "AAA" in entries, "已经下载好的票没进清单 —— 提升时会漏掉它"
    assert entries["AAA"]["status"] == "ok"
    assert "AAA" not in fetched[0], "已经下载好的票又被下载了一遍"


# ════════════════════════════════════════════════════════════════
# 窗口被空洞拉长：RS 算的是「12 个月相对强度」，不是「两根 bar 之间」
# ════════════════════════════════════════════════════════════════
#
# 排脏判据（价格量级 > 1000 倍）看不见这一类坏数据。实测（2026-10-26）：
# 排名池里 440 只票的「253 根 bar 窗口」横跨 > 400 个日历日（正常 p50=366），
# 窗口中间少了一大截数据。最典型的 AKTS：
#
#     2024-12-17  收盘 0.0372
#     ……中间空了 13 个月……
#     2026-01-09  收盘 22.40
#
# 收盘价在「数据空洞」处被当成**一天**的涨跌读出来（+60,115%），
# R252 的基准价取到的是半年前的旧价，wret = 41.61 —— 全市场第一，RS 99。
#
# 价格量级这一关拦不住它：AKTS 的窗口 max/min = 866 < 1000，稳稳通过。
# 真正的问题是**日历跨度**：253 根 bar 摊在 675 个自然日里，
# 那不是 12 个月的相对强度，是 22 个月的。


def test_window_calendar_span_counts_the_days_the_window_covers():
    """连续日线的 253 根 bar 横跨 252 个自然日。"""
    assert window_calendar_span(_frame(n=400), AS_OF) == 252


def test_window_calendar_span_is_zero_when_the_window_is_shorter_than_253():
    """历史不足 253 根的票算不出 RS，不该被判成「被拉长」。"""
    assert window_calendar_span(_frame(n=50), AS_OF) == 0


def test_a_window_with_a_big_hole_is_stretched():
    """中间少一大截 → 253 根 bar 横跨远超一年的日历时间。"""
    span = window_calendar_span(_gapped_frame(gap_days=400), AS_OF)
    assert span > 600, f"空洞没被量出来：{span} 天"


def test_find_stretched_symbols_flags_only_the_gapped_file(tmp_path):
    """
    探测器只认「窗口被拉长」的票。

    正常连续数据的票不能被卷进来 —— 那会让每轮扫描都把几千只正常票
    重新下载一遍，而排名池一点都不会变。
    """
    (tmp_path / "GOOD.pkl").write_bytes(pickle_dumps(_frame(n=600)))
    (tmp_path / "GAP.pkl").write_bytes(pickle_dumps(_gapped_frame(gap_days=400)))

    assert find_stretched_symbols(tmp_path, AS_OF, max_span_days=400) == ["GAP"]


def test_find_stretched_symbols_skips_eps_cache(tmp_path):
    (tmp_path / "eps_AAPL.pkl").write_bytes(pickle_dumps({"a": 1}))
    (tmp_path / "AAPL.pkl").write_bytes(pickle_dumps(_frame(n=600)))
    assert find_stretched_symbols(tmp_path, AS_OF, max_span_days=400) == []


def test_find_stretched_symbols_survives_corrupt_file(tmp_path):
    (tmp_path / "BROKEN.pkl").write_bytes(b"not a pickle")
    (tmp_path / "AAPL.pkl").write_bytes(pickle_dumps(_frame(n=600)))
    assert find_stretched_symbols(tmp_path, AS_OF, max_span_days=400) == []


# ════════════════════════════════════════════════════════════════
# 重下的数据不许比现有数据差
# ════════════════════════════════════════════════════════════════
#
# Alpaca 的 `adjustment="all"` 对个别代码给出**更短**的历史（35 只实测
# 195,508 倍的荒谬价格范围，一并更短）。拿它替换现有数据 = 用差数据
# 覆盖好数据 —— 虽然旧文件会留档，但排名池会白白少掉一只。


def test_collect_rejects_a_download_with_fewer_recent_bars(tmp_path, monkeypatch):
    """
    重下结果最近两年比现有数据还少 → 不许上岗。

    判据用「最近 504 个自然日里的 bar 数」，不是总 bar 数：
    Alpaca 的历史深度经常不如 Yahoo，但只要**最近两年**是全的，
    RS 的 253 根窗口就是完整的 —— 总 bar 数少不代表数据差。
    """
    import tools.fix_dirty_history as fx
    import alpaca.data.historical as ah

    cache = tmp_path / "cache"
    fix = tmp_path / "fix"
    cache.mkdir()
    fix.mkdir()
    monkeypatch.setattr(fx, "CACHE_DIR", cache)
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(ah, "StockHistoricalDataClient", lambda *a, **k: object())

    # 现有数据：最近两年 bar 很全（600 根日线 → 最近 504 天里 504 根）
    (cache / "EXIST.pkl").write_bytes(pickle_dumps(_frame(n=600)))

    # 重下结果：300 根 bar（过了 253 根的门槛，但最近两年比现有数据少 204 根）
    def fake_fetch(client, start, feed_name="sip"):
        def _f(symbols):
            return {s: _frame(n=300, end_date="2026-10-21") for s in symbols}
        return _f

    monkeypatch.setattr(fx, "_make_alpaca_fetch", fake_fetch)

    entries = fx.collect(["EXIST"], AS_OF, batch=10, sleep=0)

    assert entries["EXIST"]["status"] == "rejected"
    assert "bar" in entries["EXIST"]["reason"]
    assert not (fix / "EXIST.pkl").exists(), "不合格的数据不该写进影子目录"


def test_collect_rejects_a_download_with_fewer_total_bars(tmp_path, monkeypatch):
    """
    总 bar 数比现有数据少 → 不许上岗。

    「最近两年不更差」那道闸门拦不住这一类：现有文件可以「早年很全、
    近两年稀疏」，而重下结果「近两年很全、总共更短」。

    实测 FUSE（2026-10-27）：现有 717 根（2022-03 起，Yahoo 合并），
    Alpaca 只有 253 根（2025-10 起）—— 拿它替换 = 回测历史凭空少掉三年半。
    """
    import tools.fix_dirty_history as fx
    import alpaca.data.historical as ah

    cache = tmp_path / "cache"
    fix = tmp_path / "fix"
    cache.mkdir()
    fix.mkdir()
    monkeypatch.setattr(fx, "CACHE_DIR", cache)
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(ah, "StockHistoricalDataClient", lambda *a, **k: object())

    # 现有数据：600 根 bar，但其中只有 300 根落在最近 504 天里（早年很全、近两年稀疏）
    old = _frame(n=300, end_date="2026-10-21")
    early = _frame(n=300, end_date="2024-01-01")
    (cache / "EXIST.pkl").write_bytes(pickle_dumps(pd.concat([early, old])))

    # 重下结果：总共 350 根，最近 504 天里 350 根（比现有的 300 根还多）
    def fake_fetch(client, start, feed_name="sip"):
        def _f(symbols):
            return {s: _frame(n=350, end_date="2026-10-21") for s in symbols}
        return _f

    monkeypatch.setattr(fx, "_make_alpaca_fetch", fake_fetch)

    entries = fx.collect(["EXIST"], AS_OF, batch=10, sleep=0)

    assert entries["EXIST"]["status"] == "rejected"
    assert "bar" in entries["EXIST"]["reason"]
    assert not (fix / "EXIST.pkl").exists(), "总历史更短的数据不该写进影子目录"


def test_fetch_start_reaches_before_the_alpaca_sip_floor():
    """
    重下的起点必须早于 Alpaca SIP 的数据底线（2016-01-04）。

    原来的起点是 `as_of - 3652 天`（正好 10 年）。实测（2026-10-27）：
    84 只票的新数据比现有数据少 204 根 bar —— 因为现有文件（Yahoo 合并）
    能追到 2016-01-04，而请求起点 2016-10-23 把前面一年整段截掉了。

    拿它替换现有数据 = 白白丢掉一年历史，回测覆盖无声缩水，而且**不报错**。
    """
    from tools.fix_dirty_history import ALPACA_SIP_FLOOR, _fetch_start

    start = _fetch_start(pd.Timestamp("2026-10-23", tz="UTC"))
    assert start < ALPACA_SIP_FLOOR, (
        f"请求起点 {start} 晚于 Alpaca 的数据底线 {ALPACA_SIP_FLOOR} —— 历史会被截短"
    )


def test_collect_accepts_a_download_that_fills_the_holes(tmp_path, monkeypatch):
    """
    重下结果把空洞补上了 → 允许上岗。

    这正是「补洞」那一轮要的：窗口被拉长的票重下之后 bar 变多，
    门槛要放行，不能一律拦掉。
    """
    import tools.fix_dirty_history as fx
    import alpaca.data.historical as ah

    cache = tmp_path / "cache"
    fix = tmp_path / "fix"
    cache.mkdir()
    fix.mkdir()
    monkeypatch.setattr(fx, "CACHE_DIR", cache)
    monkeypatch.setattr(fx, "FIX_DIR", fix)
    monkeypatch.setattr(fx, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(ah, "StockHistoricalDataClient", lambda *a, **k: object())

    (cache / "GAP.pkl").write_bytes(pickle_dumps(_gapped_frame(gap_days=400)))

    def fake_fetch(client, start, feed_name="sip"):
        def _f(symbols):
            return {s: _frame(n=600, end_date="2026-10-21") for s in symbols}
        return _f

    monkeypatch.setattr(fx, "_make_alpaca_fetch", fake_fetch)

    entries = fx.collect(["GAP"], AS_OF, batch=10, sleep=0)

    assert entries["GAP"]["status"] == "ok"
    assert (fix / "GAP.pkl").exists()