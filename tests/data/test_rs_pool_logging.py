"""
排名池的「池子大小 + 签名」必须打在 live run 的日志里。

为什么是硬要求
--------------
RS Rating 是**横截面百分位**：分母是排名池。排名池一旦无声地缩水或换血
（重下数据、名单改一版、闸门调一个常量），所有股票的评级会整体位移，
而项目「成功跑完、一个错都没报」。

对策是每次都把池子大小和签名写进日志，人能看见。但这要求日志**真的能看见**：

本项目没有 `logging.basicConfig`，`logger.info()` 落到 `logging.lastResort`
（只处理 WARNING 及以上）——**直接被丢掉**。2026-10-28 实测：live run 日志里
一条 `RS 排名池：9080 只` 都没有。

所以这里锁死的是「用 print 打到 stdout」，不是「用 logger.info 记录」。

只打一次
--------
`_get_universe_wrets` 每个 ticker 都会调一次；不打去重的话，78 个信号
就是 78 行同样的池子信息，把真正的异常淹掉。
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from data import rs as rsmod

AS_OF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _wide(tmp_path, symbols=("AAPL", "MSFT")):
    """一张小宽表：每只票 300 根连续上涨的 bar（够算 252 日加权收益）。"""
    idx = pd.date_range(end="2026-10-21", periods=300, freq="D", tz="UTC")
    path = tmp_path / "closes.pkl"
    pd.DataFrame(
        {c: [10.0 + i * 0.01 for i in range(300)] for c in symbols}, index=idx
    ).to_pickle(path)
    return path


@pytest.fixture(autouse=True)
def _clean_pool_log():
    """去重状态是模块级的，每个用例都要从头开始。"""
    rsmod._POOL_LOG_SEEN.clear()
    yield
    rsmod._POOL_LOG_SEEN.clear()


@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    """受控的宽表 + 白名单，池子一定有内容。"""
    path = _wide(tmp_path)
    monkeypatch.setattr(rsmod, "DEFAULT_RS_CLOSES_PATH", path)
    monkeypatch.setattr(rsmod, "_load_pool_whitelist", lambda *a, **k: {"AAPL", "MSFT"})
    return path


def test_pool_size_and_signature_are_printed(pool_env, tmp_path, capsys):
    """建池之后必须打印：池子大小 + 签名 + 白名单状态。"""
    cache_path = tmp_path / "rs_daily.json"
    assert rsmod._populate_universe_cache(AS_OF, {}, cache_path) is True

    out = capsys.readouterr().out
    assert "RS 排名池" in out, f"日志里没有池子信息：{out!r}"
    assert "2 只" in out, f"日志里没有池子大小：{out!r}"
    assert rsmod._current_pool_signature() in out, f"日志里没有池子签名：{out!r}"
    assert "白名单" in out, f"日志里没说明白名单状态：{out!r}"
    # 必须带上日期：一份 live run 里有几十个日期的池子，不说日期就没法核对某一行
    assert "2026-10-22" in out, f"日志里没有池子对应的日期：{out!r}"


def test_pool_line_is_printed_once_per_pool(pool_env, tmp_path, capsys):
    """
    同一个池子只打一行 —— 先算后命中是**同一个**池子，打两行纯属噪声。

    命中路径每个 ticker 都走一遍；78 个信号打 78 行同样的内容，
    真正的异常（池子缩水）就被淹了。
    """
    cache_path = tmp_path / "rs_daily.json"
    rsmod._populate_universe_cache(AS_OF, {}, cache_path)

    for _ in range(5):
        rsmod._get_universe_wrets(AS_OF, {}, cache_path)

    out = capsys.readouterr().out
    assert out.count("RS 排名池") == 1, f"同样的池子信息打了 {out.count('RS 排名池')} 次：{out!r}"
    assert "2 只" in out, f"池子大小丢了：{out!r}"


def test_stale_cache_is_reported_on_stdout(pool_env, tmp_path, capsys):
    """签名对不上 → 作废重算。这件事必须让人看见。"""
    cache_path = tmp_path / "rs_daily.json"
    rsmod._populate_universe_cache(AS_OF, {}, cache_path)
    capsys.readouterr()

    entry = rsmod._load_rs_daily_cache(cache_path)
    entry[AS_OF.strftime("%Y-%m-%d")][rsmod.UNIVERSE_CACHE_KEY]["pool_sig"] = "stale"
    rsmod._save_rs_daily_cache(entry, cache_path)

    rsmod._get_universe_wrets(AS_OF, {}, cache_path)

    out = capsys.readouterr().out
    assert "失效" in out, f"缓存作废没有告诉任何人：{out!r}"
    assert "stale" in out, f"没打出旧签名：{out!r}"


def test_signature_change_is_reported_once_for_all_dates(pool_env, tmp_path, capsys):
    """
    一次签名变更会让**所有日期**的缓存条目同时作废 —— 只该报一次。

    不去重就是 30 行同样的告警，真正的告警反而被埋掉。
    """
    cache_path = tmp_path / "rs_daily.json"
    rsmod._populate_universe_cache(AS_OF, {}, cache_path)
    capsys.readouterr()

    cache = rsmod._load_rs_daily_cache(cache_path)
    for day in ("2026-10-20", "2026-10-21"):
        cache.setdefault(day, {})[rsmod.UNIVERSE_CACHE_KEY] = dict(
            cache[AS_OF.strftime("%Y-%m-%d")][rsmod.UNIVERSE_CACHE_KEY]
        )
        cache[day][rsmod.UNIVERSE_CACHE_KEY]["pool_sig"] = "stale"
    rsmod._save_rs_daily_cache(cache, cache_path)

    for day in ("2026-10-20", "2026-10-21"):
        rsmod._get_universe_wrets(
            datetime(2026, 10, int(day[-2:]), tzinfo=timezone.utc), {}, cache_path
        )

    out = capsys.readouterr().out
    assert out.count("失效") == 1, f"同一次签名变更报了 {out.count('失效')} 次：{out!r}"
