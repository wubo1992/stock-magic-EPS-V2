"""
tools/download_universe_history.py 的测试 —— 给新增标的补齐历史（Step 9）。

这个脚本会往用户的行情缓存目录**新增**几千个 pkl，所以红线只有一条，
但它比 `fix_dirty_history.py` 那条更隐蔽：

  **绝不覆盖已存在的 pkl。** `fix_dirty_history.py` 走的是「改名移到隔离档」，
  有回滚；这里直接写 `data/cache/<SYM>.pkl`，一旦写错就是原件没了。
  所以 `save_new` 必须在**任何**写盘之前检查目标是否已存在，且测试要断言
  覆盖场景下字节完全没变。

另外两条也必须钉死：
  - **可续跑**：靠 pkl 存在性 + 清单里「已判定」的状态。中途崩了不能重下几千只。
  - **失败的看得见**：拿不到 / 历史太短的票要逐条记进清单，不能只留一个成功计数。
"""
import json

import numpy as np
import pandas as pd
import pytest

from tools.download_universe_history import (
    _write_manifest,
    alpaca_symbol,
    load_manifest,
    load_universe,
    pending_symbols,
    refresh_stale,
    save_new,
    stale_symbols,
)

AS_OF = pd.Timestamp("2026-10-22", tz="UTC")


def _frame(n=400, start=10.0, end=20.0, end_date="2026-10-21"):
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


def _universe(symbols: dict) -> dict:
    return {
        "generated_at": "2026-10-23T00:00:00+00:00",
        "symbols": symbols,
    }


# ════════════════════════════════════════════════════════════════
# 选谁下载
# ════════════════════════════════════════════════════════════════


def test_pending_skips_symbols_already_in_cache(tmp_path):
    """cache 里已有的绝不重下 —— 那是用户的数据，不是本脚本该碰的东西。"""
    (tmp_path / "AAPL.pkl").write_bytes(b"original")
    u = _universe(
        {"AAPL": {"is_etf": False, "is_adr": False}, "MSFT": {"is_etf": False, "is_adr": False}}
    )
    assert pending_symbols(u, tmp_path, {}) == ["MSFT"]


def test_pending_skips_symbols_already_decided_in_manifest(tmp_path):
    """
    清单里已判定「历史太短 / 取不到」的票不该反复重下。

    续跑靠两条线索：pkl 存在（写过的）和清单里有条目（判过的）。
    只看 pkl 的话，那些永远攒不满 253 根 bar 的新 ETF 每跑一次就重下一遍。
    """
    (tmp_path / "AAPL.pkl").write_bytes(b"x")
    manifest = {"AAPL": {"status": "ok"}, "TINY": {"status": "too_short", "bars": 12}}
    u = _universe(
        {
            "AAPL": {"is_etf": False, "is_adr": False},
            "TINY": {"is_etf": True, "is_adr": False},
            "MSFT": {"is_etf": False, "is_adr": False},
        }
    )
    assert pending_symbols(u, tmp_path, manifest) == ["MSFT"]


def test_pending_can_exclude_etfs(tmp_path):
    """ETF 数量是普通股的 5 倍多，能单独关掉才能做对照实验。"""
    u = _universe(
        {
            "AAA": {"is_etf": True, "is_adr": False},
            "MSFT": {"is_etf": False, "is_adr": False},
        }
    )
    assert pending_symbols(u, tmp_path, {}, include_etfs=False) == ["MSFT"]


def test_pending_can_exclude_adrs(tmp_path):
    u = _universe(
        {
            "PORSCH": {"is_etf": False, "is_adr": True},
            "MSFT": {"is_etf": False, "is_adr": False},
        }
    )
    assert pending_symbols(u, tmp_path, {}, include_adrs=False) == ["MSFT"]


def test_load_universe_rejects_a_file_that_is_not_a_universe(tmp_path):
    """清单损坏/格式不对要报错，不要静默返回空 —— 空名单会让人以为「没票要下」。"""
    bad = tmp_path / "u.json"
    bad.write_text(json.dumps({"symbols": "not a dict"}))
    with pytest.raises(ValueError, match="symbols"):
        load_universe(bad)


# ════════════════════════════════════════════════════════════════
# 代码名映射
# ════════════════════════════════════════════════════════════════


def test_alpaca_symbol_keeps_dots():
    """
    Alpaca 的分级股代码用的是**点**，不是连字符（2026-10-23 实测）：

        BRK.A ->  2,528 根 bar        BRK-A -> APIError "invalid symbol"
        BF.B  ->  有数据              BF-B  -> APIError "invalid symbol"
        名单里 25 个带点代码：原样 23 个成功，换连字符 0 个成功

    早先这里写的是「点换连字符」，方向正好反了 —— 结果这 25 只全部
    fetch_failed，而且失败得很安静：Alpaca 对不存在的代码只是报错，
    二分拆批之后单独重试仍是空，看起来就像「这些票没有历史数据」。

    所以这里不做任何改写：名单里是什么就原样发给 Alpaca。
    """
    assert alpaca_symbol("BRK.A") == "BRK.A"
    assert alpaca_symbol("AAPL") == "AAPL"
    assert alpaca_symbol("BF-B") == "BF-B"  # 本来就带连字符的别被改动


# ════════════════════════════════════════════════════════════════
# 写盘红线
# ════════════════════════════════════════════════════════════════


def test_save_new_writes_a_new_pkl(tmp_path):
    assert save_new(tmp_path, "MSFT", _frame()) is True
    assert (tmp_path / "MSFT.pkl").exists()
    assert len(pd.read_pickle(tmp_path / "MSFT.pkl")) == 400


def test_save_new_never_overwrites(tmp_path):
    """
    硬红线：目标已存在时必须一个字节都不动。

    `fix_dirty_history.py` 走隔离档所以能覆盖；这里是直接写 data/cache/，
    覆盖等于销毁用户数据。返回值 False 让调用方记成 skipped 而不是 ok。
    """
    target = tmp_path / "AAPL.pkl"
    target.write_bytes(b"original bytes")
    before = target.read_bytes()
    mtime = target.stat().st_mtime_ns

    assert save_new(tmp_path, "AAPL", _frame()) is False
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == mtime


def test_save_new_does_not_create_parent_dirs_implicitly(tmp_path):
    """目录不存在就报错，不要顺手 mkdir —— 那会把写错路径变成静默的另一个缓存目录。"""
    with pytest.raises(FileNotFoundError):
        save_new(tmp_path / "nope", "MSFT", _frame())

# ════════════════════════════════════════════════════════════════
# 清单不能互相覆盖
#
# 踩过的坑：先跑下载（判定 5,799 只，写下载清单），再跑 --refresh-stale
# 刷新存量 —— 刷新用的是**同一个**清单文件，于是把下载清单整个盖掉了。
# 后果：下载时判成「历史不足 253 根 / 取不到」的那 1,372 只，
# 「已判定」的状态随之消失，下次重跑脚本会把它们全部重新下一遍
# （几千只 × 10 年 = 白烧几小时），正是这个清单本来要防的事。
#
# 两种模式判定的是不同的事，判定结果必须分开存。

def test_refresh_manifest_does_not_clobber_the_download_manifest(tmp_path):
    """刷新存量的清单不许盖掉下载清单 —— 两者判定的是不同的事。"""
    dl = tmp_path / "download.json"
    rf = tmp_path / "refresh.json"

    _write_manifest({"TINY": {"status": "too_short", "bars": 12}}, AS_OF, path=dl)
    _write_manifest({"AAPL": {"status": "ok", "bars": 400}}, AS_OF, path=rf)

    assert load_manifest(dl)["TINY"]["status"] == "too_short"
    assert load_manifest(rf)["AAPL"]["status"] == "ok"


def test_load_manifest_returns_the_decided_symbols(tmp_path):
    """
    清单文件存的是 {元数据..., "entries": {代码: 判定}}。

    `load_manifest` 必须返回**代码 -> 判定**这一层，因为 `pending_symbols`
    拿它去算「已判定」的代码集合。返回整个 payload 的话，`set(payload)`
    得到的是 {"generated_at","as_of","counts","entries"} 四个键 ——
    一只票都跳不过去，续跑形同虚设：1,521 只没缓存的票每跑一次就重下一遍。

    这不是理论风险：单元测试传的是扁平 dict（能跳过），
    而 main() 传的是 `load_manifest()` 的返回值（跳过不了）。
    """
    path = tmp_path / "m.json"
    _write_manifest({"TINY": {"status": "too_short", "bars": 12}}, AS_OF, path=path)

    assert load_manifest(path) == {"TINY": {"status": "too_short", "bars": 12}}


def test_pending_still_skips_symbols_decided_by_an_earlier_download(tmp_path):
    """下载清单判过的票（取不到 / 历史太短），刷新之后也不该被重下。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    dl = tmp_path / "download.json"
    rf = tmp_path / "refresh.json"

    _write_manifest({"TINY": {"status": "too_short", "bars": 12}}, AS_OF, path=dl)
    _write_manifest({"AAPL": {"status": "ok", "bars": 400}}, AS_OF, path=rf)

    u = _universe(
        {
            "TINY": {"is_etf": True, "is_adr": False},
            "AAPL": {"is_etf": False, "is_adr": False},
            "MSFT": {"is_etf": False, "is_adr": False},
        }
    )
    # TINY 被**下载**清单判为历史太短 -> 跳过（刷新不改变这一点）
    # AAPL 只出现在刷新清单里，下载清单没判过它 -> 仍要下
    assert pending_symbols(u, cache, load_manifest(dl)) == ["AAPL", "MSFT"]


# ════════════════════════════════════════════════════════════════
# 补齐落后一天的存量文件
#
# 为什么要做：Step 9 下载的新文件带 2026-10-22 的 bar，而早先那 6,303 个
# 只到 10-21。于是宽表最后一行只覆盖约一半的列，`has_current_bar` 把
# **全部**存量文件判成「落后于最新交易日」——排名池从 5,001 掉到 5,033，
# 辛辛苦苦下回来的新票进了池，用户原有的 AAPL 之类却全被踢出去了。
#
# 这不是判据的问题，是数据新旧不齐。补齐即可，但**必须可回滚**：
# 旧文件先改名移到 data/cache_refresh_stale/，绝不 unlink。

def _wide(**series):
    """造一张日历宽表：None = 那天没有 bar。"""
    idx = pd.date_range("2026-10-19", periods=4, freq="D", tz="UTC")
    return pd.DataFrame(
        {k: pd.Series(v, dtype=float, index=idx) for k, v in series.items()}
    )


def test_stale_symbols_are_the_ones_missing_the_last_bar():
    wide = _wide(
        FRESH=[1.0, 2.0, 3.0, 4.0],       # 有最后一天
        STALE=[1.0, 2.0, 3.0, None],      # 停在倒数第二天
        NEWISH=[None, None, None, 5.0],   # 只有最后一天
    )
    assert stale_symbols(wide) == ["STALE"]


def test_stale_symbols_ignores_completely_empty_columns():
    """一格数据都没有的列是「没下过」，不是「落后」—— 它压根不在 cache 里。"""
    wide = _wide(FRESH=[1.0, 2.0, 3.0, 4.0], EMPTY=[None] * 4)
    assert stale_symbols(wide) == []


def test_refresh_stale_backs_up_the_old_file_before_writing(tmp_path):
    """
    红线：替换一律是「改名移到备份目录」，不是删除。

    回滚就一条 `mv data/cache_refresh_stale/*.pkl data/cache/`。
    断言的是**旧字节仍在** —— 不是「目录里有个文件」，而是内容一模一样。

    新数据必须真的多一天（到 10-22），否则「原文件已经新鲜」就该直接跳过 ——
    那正是下面那条测试守的行为。
    """
    cache = tmp_path / "cache"
    backup = tmp_path / "backup"
    cache.mkdir()
    pd.to_pickle(_frame(n=300, end_date="2026-10-21"), cache / "AAPL.pkl")
    original = (cache / "AAPL.pkl").read_bytes()

    assert refresh_stale(cache, backup, "AAPL", _frame(n=400, end_date="2026-10-22")) is True
    assert len(pd.read_pickle(cache / "AAPL.pkl")) == 400
    assert (backup / "AAPL.pkl").read_bytes() == original


def test_refresh_stale_leaves_a_fresh_file_alone(tmp_path):
    """已经有最后一天 bar 的文件不许被动 —— 它不需要刷新。"""
    cache = tmp_path / "cache"
    backup = tmp_path / "backup"
    cache.mkdir()
    pd.to_pickle(_frame(), cache / "AAPL.pkl")
    original = (cache / "AAPL.pkl").read_bytes()

    assert refresh_stale(cache, backup, "AAPL", _frame(n=300)) is False
    assert (cache / "AAPL.pkl").read_bytes() == original
    assert not backup.exists(), "没动过的文件不该凭空多出一份备份"


def test_refresh_stale_refuses_to_truncate_history(tmp_path):
    """
    新数据起点比旧文件**更晚**时必须拒绝替换。

    这是刷新存量最危险的一个失败模式，而且安静：新文件一样「新鲜」，
    一样能算 RS，但前面几千根 bar 没了 —— 回测覆盖无声缩水几个月。
    实测踩到过：抓取起点写成 as_of - 10 年（2016-10-22），而旧文件到
    2016-04-07，AAPL 被从 4,103 行砍到 2,513 行、起点整整推后 6 个月。

    判据用**起点日期**而不是 bar 数：旧文件可能有重复的日内行，
    行数不可比（AAPL 4,103 行其实只有约 2,632 个交易日）。
    """
    cache = tmp_path / "cache"
    backup = tmp_path / "backup"
    cache.mkdir()
    pd.to_pickle(_frame(n=400, end_date="2016-10-21"), cache / "AAPL.pkl")
    original = (cache / "AAPL.pkl").read_bytes()

    # 新数据只回溯到 2020 年 —— 比旧文件的 2016 年更晚
    truncated = _frame(n=400, end_date="2026-10-22")
    truncated.index = pd.date_range("2020-01-01", periods=400, freq="B", tz="UTC")

    assert refresh_stale(cache, backup, "AAPL", truncated) is False
    assert (cache / "AAPL.pkl").read_bytes() == original
    assert not backup.exists(), "拒绝替换时不该产生备份"
