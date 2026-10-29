"""
Yahoo 回退必须有**上限 + 熔断**，不能一股脑全试。

为什么这是硬要求
----------------
2026-10-26 实测：live run 的数据获取 538.7 秒里，**462.8 秒（86%）**
花在 Yahoo 回退上——

```
[数据] Alpaca 成功下载 5921/6028 只
[数据] 增量更新：Alpaca 未覆盖 107 只，fallback 到 Yahoo Finance...
[数据] Yahoo 下载 107 只，预计耗时: 3.6分钟，实际: 327.6秒
```

这 107 只全是 Alpaca 不覆盖的 ADR / OTC 代码，Yahoo 对它们**一只都取不到**
（403 IP 级限流）。每只 2 秒 sleep + 一次请求 = 3 秒，107 只就是 5 分半。

再往前还有一次同样的回退（45 只全量下载后 Alpaca 漏掉的），又花了 135.2 秒。

两边加起来 463 秒纯浪费，而且日志刷几百行「被限流（403），跳过」，
真正有用的告警被淹了。

规则：
  1. 回退**最多试 N 只**（默认 20），超出的记进失败清单，不静默丢；
  2. 连续 403 达到阈值就**熔断**——IP 级限流时继续打只会更糟。
"""
from datetime import datetime, timezone

import pytest

from data import fetcher

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _ok_payload():
    """一个能被 _parse_yahoo_response 吃下的 Yahoo chart 响应。"""
    ts = [1758662400, 1758748800, 1758835200]  # 2026-09-24 ~ 09-26
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": ts,
                    "indicators": {
                        "quote": [
                            {
                                "open": [1.0, 2.0, 3.0],
                                "high": [1.0, 2.0, 3.0],
                                "low": [1.0, 2.0, 3.0],
                                "close": [1.0, 2.0, 3.0],
                                "volume": [100.0, 100.0, 100.0],
                            }
                        ],
                        "adjclose": [{"adjclose": [1.0, 2.0, 3.0]}],
                    },
                }
            ],
        }
    }


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"chart": {"error": {"code": "not found"}}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    """假装自己是 requests.Session：按 URL 决定回 403 还是回正常数据。"""

    def __init__(self, responder=None):
        self.headers = {}
        self.calls: list[str] = []
        self._responder = responder or (lambda url: _FakeResponse(403))

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        return self._responder(url)


@pytest.fixture
def fake_yahoo(monkeypatch):
    """接管 requests.Session 与 time.sleep（sleep 2 秒 × 上百只会把测试拖死）。"""
    session = _FakeSession()
    monkeypatch.setattr(fetcher.requests, "Session", lambda: session)
    monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
    return session


def test_the_fallback_stops_after_the_cap(fake_yahoo, tmp_path):
    """超上限的代码不再去 Yahoo 试 —— 否则 107 只就是 5 分半。"""
    failed = tmp_path / "download_failed.txt"

    fetcher._fetch_via_yahoo(
        [f"SYM{i}" for i in range(50)],
        START,
        END,
        365,
        max_symbols=20,
        failed_path=failed,
    )

    assert len(fake_yahoo.calls) <= 20, f"试了 {len(fake_yahoo.calls)} 只，没到上限"


def test_symbols_beyond_the_cap_are_recorded_not_dropped(fake_yahoo, tmp_path):
    """超上限的代码要记进失败清单 —— 不能「没报错」地消失。"""
    failed = tmp_path / "download_failed.txt"

    fetcher._fetch_via_yahoo(
        [f"SYM{i}" for i in range(50)],
        START,
        END,
        365,
        max_symbols=20,
        failed_path=failed,
    )

    text = failed.read_text(encoding="utf-8")
    assert "SYM49" in text, "超上限的代码没有进失败清单"
    assert "SYM0" in text, "被限流取不到的代码也没进失败清单"


def test_a_403_streak_breaks_the_circuit(fake_yahoo, tmp_path):
    """
    连续 403 就要停手。

    IP 级限流时继续打只会更糟，而且日志会被几百行「被限流」淹掉。
    """
    failed = tmp_path / "download_failed.txt"

    fetcher._fetch_via_yahoo(
        [f"SYM{i}" for i in range(200)],
        START,
        END,
        365,
        max_symbols=None,  # 不设上限，只靠熔断兜底
        failed_path=failed,
    )

    assert len(fake_yahoo.calls) < 200, "403 熔断没生效"
    assert len(fake_yahoo.calls) <= 20, f"熔断阈值太大：{len(fake_yahoo.calls)} 次请求"


def test_the_circuit_resets_after_a_success(monkeypatch, tmp_path):
    """不是 403 的失败（比如这只票真的没数据）不该累计成熔断。"""
    session = _FakeSession(
        responder=lambda url: (
            _FakeResponse(200, _ok_payload())
            if "SYM0" in url
            else _FakeResponse(404)
        )
    )
    monkeypatch.setattr(fetcher.requests, "Session", lambda: session)
    monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
    failed = tmp_path / "download_failed.txt"

    got = fetcher._fetch_via_yahoo(
        [f"SYM{i}" for i in range(30)],
        START,
        END,
        365,
        max_symbols=None,
        failed_path=failed,
    )

    assert "SYM0" in got, "取到的票没返回"
    assert len(session.calls) >= 20, "不该因为非 403 的失败就熔断"


def test_successful_symbols_are_still_returned(monkeypatch, tmp_path):
    """正常路径不能被这次改动影响。"""
    session = _FakeSession(responder=lambda url: _FakeResponse(200, _ok_payload()))
    monkeypatch.setattr(fetcher.requests, "Session", lambda: session)
    monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
    failed = tmp_path / "download_failed.txt"

    got = fetcher._fetch_via_yahoo(
        ["A", "B"], START, END, 365, max_symbols=20, failed_path=failed
    )

    assert sorted(got) == ["A", "B"]
    assert not failed.exists() or failed.read_text(encoding="utf-8").strip() == ""
