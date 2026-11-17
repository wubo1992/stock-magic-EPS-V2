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


def test_tiny_positive_eps_keeps_precision_in_reason(eps_env):
    """EPS $0.0025 不能被写成 "$0.00" —— 报告的「EPS 为正」门槛读的就是这个串。

    2026-11-17 实测：KVYO 最新季度 EPS = $0.0025（从 −$0.0298 转正），
    触发原因写成「最新季度EPS$0.00」→ 仓位档位规则 `(eps or 0) > 0` 判成
    「不是正数」→ 即使 RS ≥85 也拿不到标准仓；报告显示的 EPS 也是 $0.00，
    读者以为它没赚钱。
    """
    f, tmp_path = eps_env
    quarters = [
        ("2026-Q3", 0.0025),    # 最新季度：微利
        ("2026-Q2", -0.0298),
        ("2026-Q1", 0.0296),
        ("2025-Q4", 0.0232),
        ("2025-Q3", -0.0014),   # 去年同期（基数接近 0 → 同比百分比虚高）
        ("2025-Q2", -0.0852),
        ("2025-Q1", -0.0514),
        ("2024-Q4", -0.0996),
    ]
    _write_cache(tmp_path / "eps_TEST.pkl", quarters)

    _passed, reason = f.check_eps_filter("TEST", quarters=2)

    assert "最新季度EPS$0.00，" not in reason, f"小额正 EPS 被抹成 0：{reason}"

    import re
    m = re.search(r"最新季度EPS\$(-?[\d.]+)", reason)
    assert m, f"触发原因里找不到 EPS：{reason}"
    assert float(m.group(1)) == pytest.approx(0.0025)


def test_normal_eps_string_is_unchanged(eps_env):
    """正常金额仍按 2 位小数写 —— 不能为了 KVYO 一只票把所有行的 EPS 都改格式。"""
    f, tmp_path = eps_env
    quarters = [
        ("2026-Q3", 1.00),
        ("2026-Q2", 0.53),
        ("2026-Q1", 0.31),
        ("2025-Q4", 0.29),
        ("2025-Q3", 0.28),
        ("2025-Q2", 0.20),
        ("2025-Q1", 0.10),
        ("2024-Q4", 0.05),
    ]
    _write_cache(tmp_path / "eps_TEST.pkl", quarters)

    _passed, reason = f.check_eps_filter("TEST", quarters=2)

    assert "最新季度EPS$1.00，" in reason, reason


def test_zero_eps_is_not_written_as_n_a(eps_env):
    """EPS 恰好为 0 不是「没有数据」—— 写成 N/A 会让人以为是取数失败。"""
    f, tmp_path = eps_env
    quarters = [
        ("2026-Q3", 0.0),
        ("2026-Q2", -0.0298),
        ("2026-Q1", 0.0296),
        ("2025-Q4", 0.0232),
        ("2025-Q3", -0.0014),
        ("2025-Q2", -0.0852),
        ("2025-Q1", -0.0514),
        ("2024-Q4", -0.0996),
    ]
    _write_cache(tmp_path / "eps_TEST.pkl", quarters)

    _passed, reason = f.check_eps_filter("TEST", quarters=2)

    assert "N/A" not in reason, reason
    assert "最新季度EPS$0.00，" in reason, reason
