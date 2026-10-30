"""EPS 缓存不能永久有效 —— 否则筛选用的是几个季度前的旧 EPS。

2026-10-30 实测故障：LUNG 的信号（2026-10-29 发出）用的 EPS 缓存是
2026-04-13 抓的，**最新季度还停在 2025-Q4** —— 缺了 2026-Q1/Q2 两个季度，
同比/环比全是在旧数据上算的。

模块 docstring 本来就写着「本地 pickle 文件，90 天过期」，
但 `_is_cache_valid()` 只判断文件存不存在 → 缓存永不过期。

修法：缓存超过 TTL（默认 30 天）就重新抓；抓不到时**回退到旧缓存**
（宁可旧数据也不要没有数据）。
"""
import os
import pickle
import time
from pathlib import Path

import pytest


@pytest.fixture
def eps_env(tmp_path, monkeypatch):
    """把 EPS 缓存目录指到 tmp_path，并给上必需的 API key。"""
    from data import fundamentals as f

    monkeypatch.setattr(f, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(f, "ALPHA_VANTAGE_API_KEY", "test-av-key")
    monkeypatch.setattr(f, "FINNHUB_API_KEY", "test-fh-key")
    return f, tmp_path


def _write_cache(path: Path, quarters: list, age_days: float = 0.0) -> None:
    path.write_bytes(pickle.dumps({"eps_history": quarters}))
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))


def _q(n: int, start_year: int = 2020):
    """造 n 个季度的 EPS 历史（最新在前）。"""
    out = []
    y, q = start_year, 1
    for i in range(n):
        out.append((f"{y}-Q{q}", 1.0 + i * 0.1))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


def test_expired_cache_is_refreshed(eps_env):
    """过期缓存必须重新抓 —— 否则新季度永远进不来。"""
    f, tmp_path = eps_env
    cache = tmp_path / "eps_TEST.pkl"
    _write_cache(cache, _q(10, start_year=2020), age_days=200)

    fresh = _q(12, start_year=2020)
    monkey_calls = []

    def fake_finnhub(symbol):
        monkey_calls.append(symbol)
        return list(fresh)

    f._fetch_eps_from_finnhub = fake_finnhub

    result = f.get_eps_growth("TEST", quarters_required=3, yoy_required=True, qoq_required=True)

    assert monkey_calls == ["TEST"], "过期缓存应该触发重新抓取"
    assert result["latest_eps"] == pytest.approx(fresh[0][1])
    assert result["quarters"] == 12


def test_fresh_cache_is_not_refetched(eps_env):
    """没过期就不该白花 API 额度。"""
    f, tmp_path = eps_env
    cache = tmp_path / "eps_TEST.pkl"
    quarters = _q(10, start_year=2020)
    _write_cache(cache, quarters, age_days=1)

    calls = []
    f._fetch_eps_from_finnhub = lambda s: calls.append(s) or []

    result = f.get_eps_growth("TEST", quarters_required=3, yoy_required=True, qoq_required=True)

    assert calls == [], "新鲜缓存不该重新抓"
    assert result["latest_eps"] == pytest.approx(quarters[0][1])


def test_expired_cache_falls_back_to_stale_data_when_refresh_fails(eps_env):
    """抓不到新数据时回退到旧缓存 —— 宁可旧数据也不要没数据。"""
    f, tmp_path = eps_env
    cache = tmp_path / "eps_TEST.pkl"
    quarters = _q(10, start_year=2020)
    _write_cache(cache, quarters, age_days=200)

    f._fetch_eps_from_finnhub = lambda s: []
    f._fetch_eps_from_av = lambda s: []

    result = f.get_eps_growth("TEST", quarters_required=3, yoy_required=True, qoq_required=True)

    assert result["latest_eps"] == pytest.approx(quarters[0][1])
    assert result["quarters"] == 10
