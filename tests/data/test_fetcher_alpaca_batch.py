"""
`_alpaca_fetch_batch` 失败时必须**二分拆批**，不能整批丢掉。

为什么这是硬要求
----------------
live run 的股票池有 6,000+ 只，混着 ADR、unit、warrant、以及早该退市的票。
原实现遇到非「invalid symbol」的异常就 `return {}` —— **整批 200 只静默消失**，
日志只有一行「批次失败」。项目「成功跑完、什么错都没报」，但这 200 只票
根本没更新数据，RS 拿的是上个月的旧价。

无效代码的剔除也有上限：累计剔满 10 个之后，第 11 个无效代码就会让
**剩下整批**直接返回 {}。上万个代码里混着几十个 warrant 是常态。

所以：失败就拆成两半分别重试；只有单只票反复失败才放弃它一只。
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from data.fetcher import _alpaca_fetch_batch

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _bars_df(symbols):
    """造一个和 Alpaca `bars.df` 同构的 MultiIndex 帧。"""
    rows = []
    for sym in symbols:
        for i in range(3):
            rows.append(
                (
                    sym,
                    pd.Timestamp("2026-10-21") + pd.Timedelta(days=i),
                    100.0 + i,
                    101.0 + i,
                    99.0 + i,
                    100.0 + i,
                    1_000_000.0,
                )
            )
    idx = pd.MultiIndex.from_tuples(
        [(r[0], r[1]) for r in rows], names=["symbol", "timestamp"]
    )
    return pd.DataFrame(
        {
            "open": [r[2] for r in rows],
            "high": [r[3] for r in rows],
            "low": [r[4] for r in rows],
            "close": [r[5] for r in rows],
            "volume": [r[6] for r in rows],
        },
        index=idx,
    )


class _FakeBars:
    def __init__(self, df):
        self.df = df


class _FakeClient:
    """按批次内容决定成功、空响应还是抛异常的假 Alpaca 客户端。"""

    def __init__(self, fail_when=None, error="boom", name_the_bad_one=False, empty_when=None):
        self.fail_when = fail_when or (lambda syms: False)
        self.empty_when = empty_when or (lambda syms: False)
        self.error = error
        self.name_the_bad_one = name_the_bad_one
        self.calls: list[list[str]] = []

    def get_stock_bars(self, req):
        syms = list(req.symbol_or_symbols)
        self.calls.append(syms)
        if self.fail_when(syms):
            if self.name_the_bad_one:
                # 真实 Alpaca 的报错会点名那个无效代码
                bad = next(s for s in syms if s.startswith("BAD"))
                raise RuntimeError(f'{{"message":"invalid symbol: {bad}"}}')
            raise RuntimeError(self.error)
        if self.empty_when(syms):
            return _FakeBars(_bars_df([]))
        return _FakeBars(_bars_df(syms))


def test_a_failing_batch_is_split_instead_of_dropped():
    """
    整批失败必须拆成两半重试，好票必须全拿到。

    原来是 `return {}` —— 一批 200 只全丢，日志只留一行。
    """
    client = _FakeClient(fail_when=lambda syms: len(syms) > 2)

    got = _alpaca_fetch_batch(
        client, ["A", "B", "C", "D"], START, END, set()
    )

    assert sorted(got) == ["A", "B", "C", "D"], "整批失败被当成「没数据」丢掉了"
    assert len(client.calls) > 1, "根本没重试过"
    assert max(len(c) for c in client.calls[1:]) < 4, "没有真正拆批"


def test_an_invalid_symbol_does_not_take_the_batch_down():
    """一只无效代码不该让同批的其他票陪葬。"""
    client = _FakeClient(
        fail_when=lambda syms: "BAD" in syms,
        name_the_bad_one=True,
    )

    got = _alpaca_fetch_batch(
        client, ["A", "BAD", "B"], START, END, set()
    )

    assert sorted(got) == ["A", "B"]
    assert "BAD" not in got


def test_more_than_ten_invalid_symbols_still_keeps_the_good_ones():
    """
    无效代码超过 10 个之后，原实现会把**剩下整批**丢掉。

    剔满 10 个之后应该继续靠拆批定位坏代码，而不是放弃整批。
    """
    bad = [f"BAD{i}" for i in range(15)]
    good = [f"G{i}" for i in range(5)]

    def fail_when(syms):
        return any(s in bad for s in syms)

    client = _FakeClient(fail_when=fail_when, name_the_bad_one=True)

    got = _alpaca_fetch_batch(client, bad + good, START, END, set())

    assert sorted(got) == sorted(good), f"好票被无效代码连累了：{sorted(got)}"


def test_a_persistent_failure_drops_only_the_offending_symbol():
    """一只票反复失败只能丢它一只，不能整批陪葬，也不能无限递归。"""
    client = _FakeClient(
        fail_when=lambda syms: "BAD" in syms,
        error="always fails for BAD",
    )

    got = _alpaca_fetch_batch(
        client, ["A", "BAD", "B"], START, END, set()
    )

    assert sorted(got) == ["A", "B"]
    # 收敛：BAD 被单独试过，但不会无限重试
    assert len(client.calls) < 20, f"重试次数失控：{len(client.calls)}"


def test_a_successful_batch_is_fetched_in_one_call():
    """正常路径不该多发请求（拆批只在失败时启用）。"""
    client = _FakeClient()

    got = _alpaca_fetch_batch(client, ["A", "B", "C"], START, END, set())

    assert sorted(got) == ["A", "B", "C"]
    assert len(client.calls) == 1


def test_an_empty_response_also_splits_the_batch():
    """
    Alpaca 返回**空数据**时也要拆批重试。

    空响应不是异常，所以走不到 except 里的二分拆批 —— 原来直接
    `return {}`，一批 200 只静默消失。

    后果不只是「这批票没更新」：这 200 只随后全部 fallback 到 Yahoo，
    而 Yahoo 的 bar 时间戳是 00:00 UTC、Alpaca 的是 04:00 UTC。合并进
    已有文件后，同一个交易日会留下两根 bar —— **双源污染就是这么来的**
    （实测 5,366 只行情文件同时含 00:00 和 04:00 的 bar）。
    """
    client = _FakeClient(empty_when=lambda syms: len(syms) > 2)

    got = _alpaca_fetch_batch(client, ["A", "B", "C", "D"], START, END, set())

    assert sorted(got) == ["A", "B", "C", "D"], "空响应被当成「这批都没数据」丢掉了"
    assert len(client.calls) > 1, "根本没重试过"
    assert max(len(c) for c in client.calls[1:]) < 4, "没有真正拆批"


def test_an_empty_response_for_one_symbol_does_not_retry_forever():
    """单只票返回空就该放弃它一只，不能无限递归。"""
    client = _FakeClient(empty_when=lambda syms: True)

    got = _alpaca_fetch_batch(client, ["BAD"], START, END, set())

    assert got == {}
    assert len(client.calls) == 1, f"重试次数失控：{len(client.calls)}"
