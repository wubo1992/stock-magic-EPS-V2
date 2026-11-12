"""RS 日级缓存的滚动窗口会把**刚写进去的日期**自己挤掉（2026-11-12 真实故障）。

现场：UMC 的 2026-09-29 crossback 信号，RS Rating 和板块排名都是空的。

查下来不是数据脏（UMC 的加权收益 1.2386 算得出来），是 `_save_rs_daily_cache`
的滚动窗口：

    all_dates = sorted(cache.keys())
    if len(all_dates) > RS_DAILY_CACHE_MAX_DAYS:
        for d in all_dates[:-RS_DAILY_CACHE_MAX_DAYS]:
            cache.pop(d, None)

缓存满了 30 天时，再写一个**比最老那天还老**的日期（回踩信号会一直往前追），
31 个日期 → `all_dates[:-30]` 恰好就是 ["2026-09-29"] → 刚写的条目被自己挤掉。

调用方 `_get_universe_wrets` 写完再从磁盘读回来，读到的是空的 →
RS Rating = None、板块排名 = None，**全程不报任何错**。
"""
import json
from datetime import datetime, timezone

from data import rs as rsmod


def _fill_cache(path, n=rsmod.RS_DAILY_CACHE_MAX_DAYS):
    """铺满 n 天（2026-10-01 起），返回写好的 cache dict。"""
    cache = {f"2026-10-{d:02d}": {} for d in range(1, n + 1)}
    rsmod._save_rs_daily_cache(cache, path)
    return cache


def test_saving_an_older_date_does_not_evict_that_same_date(tmp_path):
    """刚写进去的日期不能被滚动窗口自己挤掉。"""
    path = tmp_path / "rs_daily_cache.json"
    _fill_cache(path)

    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["2026-09-29"] = {"__UNIVERSE__": {"wrets": [0.1, 0.2]}}
    rsmod._save_rs_daily_cache(cache, path, keep_date="2026-09-29")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "2026-09-29" in saved, "刚写的日期被滚动窗口挤掉了"
    assert saved["2026-09-29"]["__UNIVERSE__"]["wrets"] == [0.1, 0.2]


def test_rolling_window_still_evicts_the_oldest_date(tmp_path):
    """滚动本身不能坏 —— 写新日期时最老的那天照样要出去。"""
    path = tmp_path / "rs_daily_cache.json"
    _fill_cache(path)

    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["2026-11-13"] = {"__UNIVERSE__": {"wrets": [0.3]}}
    rsmod._save_rs_daily_cache(cache, path, keep_date="2026-11-13")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "2026-11-13" in saved
    assert "2026-10-01" not in saved, "最老的那天应该被滚动掉"
    assert len(saved) <= rsmod.RS_DAILY_CACHE_MAX_DAYS + 1


def test_universe_pool_survives_a_full_cache(tmp_path, monkeypatch):
    """端到端：缓存满时算出来的池子必须能读回来（不能写完就丢）。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    _fill_cache(cache_path)

    monkeypatch.setattr(
        rsmod, "_universe_wrets_from_wide",
        lambda as_of=None: ([0.1, 0.2, 0.3], "wide"),
    )
    monkeypatch.setattr(rsmod, "_current_pool_signature", lambda: "sig-test")

    wrets = rsmod._get_universe_wrets(
        datetime(2026, 9, 29, tzinfo=timezone.utc), {}, cache_path,
    )

    assert wrets == [0.1, 0.2, 0.3], "池子算出来了却没能存住"


def test_rs_rating_is_computed_for_a_date_outside_the_window(tmp_path, monkeypatch):
    """窗口外的日期也必须能算出 RS Rating —— 不能静默返回 None。"""
    cache_path = tmp_path / "rs_daily_cache.json"
    _fill_cache(cache_path)

    monkeypatch.setattr(
        rsmod, "_universe_wrets_from_wide",
        lambda as_of=None: ([0.1, 0.2, 0.3], "wide"),
    )
    monkeypatch.setattr(rsmod, "_current_pool_signature", lambda: "sig-test")

    rating = rsmod._get_rs_rating(
        ticker_wret=0.25,
        as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
        market_data={},
        cache_path=cache_path,
    )

    assert rating is not None, "窗口外的日期算不出 RS Rating —— 静默失真"
