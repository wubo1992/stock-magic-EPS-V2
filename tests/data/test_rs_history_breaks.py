"""
已知「历史断点」：证券身份变了，断点前后的价格**不可比**，窗口跨过它就不给数。

背景（2026-10-28 亲自实测，不是估算）
------------------------------------
1. Alpaca 的 corporate-actions 接口（`CorporateActionsRequest`）给出了权威口径：

       WOLF  2025-09-29  unit_split  old_rate=1.0  new_rate=0.008352
       NEUP  2024-12-24  stock_merger  1 股 BNOX → 0.08333333 股 NEUP

   WOLF 那次**不是普通反向拆股**：按 8-K（2025-09-29，Item 8.01），
   旧股全部注销（约 1.5648 亿股），按 0.008352 新股/旧股换发新股
   （初始 1,306,903 股），旧 CUSIP 977852102 → 新 CUSIP 97785W106。
   断点前后是**两个不同的证券**，价格没有任何可比性。

2. 所以「把断点前的价格乘一个比例」这条路走不通：

       按 1/0.008352 = 119.73 倍还原 → 2025-09-26 的 1.21 变成 144.87，
                                 而断点后是 22.10 → 凭空造出 -85% 的「跌幅」
       按 20 倍还原（曾经的猜测）  → 24.20 vs 22.10 → -8.7%
                                  只是把错误缩小，没有变对

   唯一的诚实做法：**窗口跨过断点就不给数**。
   等断点后攒满 253 根 bar（干净的 12 个月），这只票自然回归排名池。

3. 危害是实测出来的：NEUP 在 as_of=2025-11-15 的加权收益是 **+265.7%**
   （R252 的基准价落在合并前的 0.2531），在历史排名池里直接排到第 1 ——
   那不是收益，是两个不同的证券拼在一起算出来的假数字。
"""
import pandas as pd
import pytest

from data import rs_universe as ru
from data.rs import _compute_weighted_return, _current_pool_signature

BREAK_DATE = pd.Timestamp("2025-09-29", tz="UTC")

N_BARS = 400
BREAK_AT = 200  # 第 200 根 bar 就是断点日那根
IDX = pd.date_range(
    start=BREAK_DATE - pd.Timedelta(days=BREAK_AT),
    periods=N_BARS,
    freq="D",
    tz="UTC",
)
AS_OF = IDX[-1]

# R252 的基准价落在 index[N_BARS - 253] = 147，**早于**断点（200）
BASE_INDEX = N_BARS - 253
assert BASE_INDEX < BREAK_AT

# 与标量路径的加权公式一致（data/rs.py: WEIGHT_63/126/189/252）
PARTS = ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252))


def _closes() -> list[float]:
    """一条连续上涨的价格序列（断点前后**不**制造跳空，好单独检验断点规则）。"""
    return [10.0 + i * 0.01 for i in range(N_BARS)]


def _price_df(closes: list[float] | None = None) -> pd.DataFrame:
    c = closes if closes is not None else _closes()
    return pd.DataFrame({"close": [float(x) for x in c]}, index=IDX)


def _hand_weighted(closes: list[float]) -> float:
    cur = closes[-1]
    return sum(w * (cur / closes[-(lb + 1)] - 1.0) for w, lb in PARTS)


# ── 断点规则本身 ──────────────────────────────────────────────────

def test_break_inside_window_yields_no_number(monkeypatch):
    """
    窗口跨过断点 → 不给数（None），而不是拿断点前的价格算出一个假收益。

    这是本文件的核心契约：断点前的价格与断点后的价格不可比，
    硬算出来的收益率没有任何意义。
    """
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2025-09-29"})

    got = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="TESTX")

    assert got is None


def test_break_before_window_still_yields_a_number(monkeypatch):
    """断点在窗口**之前** → 窗口是干净的，照常给数（且不做任何价格缩放）。"""
    # 窗口起点 = IDX[BASE_INDEX] = 2025-08-07，断点必须早于它
    assert IDX[BASE_INDEX] > pd.Timestamp("2025-04-01", tz="UTC")
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2025-04-01"})

    got = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="TESTX")

    assert got is not None
    assert got == pytest.approx(_hand_weighted(_closes()), rel=1e-12)


def test_break_after_current_date_still_yields_a_number(monkeypatch):
    """断点还没发生（在未来）→ 整段窗口都在断点之前，照常给数。"""
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2027-01-01"})

    got = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="TESTX")

    assert got is not None
    assert got == pytest.approx(_hand_weighted(_closes()), rel=1e-12)


def test_unknown_ticker_is_never_excluded(monkeypatch):
    """没登记断点的票不受影响。"""
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2025-09-29"})

    got = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="OTHER")

    assert got is not None
    assert got == pytest.approx(_hand_weighted(_closes()), rel=1e-12)


def test_break_exactly_at_window_start_is_allowed(monkeypatch):
    """
    断点正好落在窗口起点（= R252 的基准价那根 bar）→ 基准价已经是断点后的价格，
    与现价同尺度，可以算。
    """
    base_ts = IDX[BASE_INDEX]
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": base_ts.strftime("%Y-%m-%d")})

    got = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="TESTX")

    assert got is not None
    assert got == pytest.approx(_hand_weighted(_closes()), rel=1e-12)


# ── 两条路径必须一致 ──────────────────────────────────────────────

def test_wide_and_scalar_agree_across_a_break(monkeypatch):
    """宽表路径也必须把跨断点的票踢出排名池（NaN），与标量路径的 None 对齐。"""
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2025-09-29"})
    wide = pd.DataFrame({"TESTX": _closes()}, index=IDX)

    got_wide = ru.vectorized_weighted_return(wide)

    assert pd.isna(got_wide["TESTX"])


def test_wide_and_scalar_agree_when_break_is_outside_the_window(monkeypatch):
    """断点在窗口之前时，宽表与标量给出同一个数。"""
    monkeypatch.setattr(ru, "KNOWN_PRICE_BREAKS", {"TESTX": "2025-04-01"})
    wide = pd.DataFrame({"TESTX": _closes()}, index=IDX)

    got_wide = ru.vectorized_weighted_return(wide)
    got_scalar = _compute_weighted_return(_price_df(), AS_OF.to_pydatetime(), ticker="TESTX")

    assert got_scalar is not None
    assert got_wide["TESTX"] == pytest.approx(got_scalar, rel=1e-12)


# ── 签名必须覆盖这张表 ────────────────────────────────────────────

def test_pool_signature_covers_the_known_breaks_table(monkeypatch):
    """
    改了断点表 → 排名池的成分就变了 → 签名必须跟着变。

    不签的话旧缓存照样命中，历史排名池里继续躺着 NEUP 的 +265% 假值，
    而且什么异常都看不出来（这是本项目最常见的静默空跑）。
    """
    before = _current_pool_signature()
    monkeypatch.setattr(
        "data.rs_universe.KNOWN_PRICE_BREAKS",
        {"TESTX": ("2025-09-29",)},
    )
    assert _current_pool_signature() != before, "改了断点表，签名却没变"
