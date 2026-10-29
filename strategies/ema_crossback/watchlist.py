"""
strategies/ema_crossback/watchlist.py — Watch List CRUD

持久化 v_eps_v2 买入信号到 output/v_eps_v2/watchlist.json，
供 EMA 10 crossback 策略每天扫描。

设计要点：
- functional API（不修改入参，返回新 dict）
- 缺失文件 → 返回 {}
- 损坏文件 → 备份为 .bak + 返回 {}
- 时区：所有 datetime 用 UTC ISO 字符串
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def load_watchlist(path: Path) -> dict[str, dict[str, Any]]:
    """
    加载 watchlist.json。

    Args:
        path: watchlist.json 的完整路径

    Returns:
        dict[symbol, entry]: 若文件不存在或损坏，返回 {}
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data)}")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        # 损坏：备份 + 返回空
        backup = path.with_suffix(".json.bak")
        try:
            path.rename(backup)
            print(f"[watchlist] WARNING: corrupt file backed up to {backup}: {e}")
        except Exception as e2:
            print(f"[watchlist] WARNING: corrupt file, backup failed: {e2}")
        return {}


def save_watchlist(watchlist: dict[str, dict[str, Any]], path: Path) -> None:
    """
    把 watchlist 写回磁盘。

    Args:
        watchlist: {symbol: entry}
        path: watchlist.json 的完整路径
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as f:
        json.dump(watchlist, f, indent=2, ensure_ascii=False)


def add_signals_to_watchlist(
    watchlist: dict[str, dict[str, Any]],
    new_signals: list[dict[str, Any]],
    expire_days: int,
    today: datetime,
) -> dict[str, dict[str, Any]]:
    """
    把新的 v_eps_v2 信号加入 watchlist。

    行为规则：
    - 新 symbol: expires_at = today + expire_days
    - 已存在的 symbol（无论是否过期）: 刷新 metadata (signal_date/price/stop)
      + 刷新 expires_at = today + expire_days
      + 保留 crossback_count 和 last_crossback_date（累计不重置）

    Args:
        watchlist: 当前 watchlist dict（不修改）
        new_signals: list of {symbol, signal_date, signal_price, stage2_stop}
        expire_days: watch list 过期天数（默认 60）
        today: 当前时间（用于计算 expires_at）

    Returns:
        更新后的 watchlist（functional，返回新 dict）
    """
    result = dict(watchlist)  # 复制，不修改入参
    new_expiry = (today + timedelta(days=expire_days)).isoformat()

    for sig in new_signals:
        symbol = sig["symbol"]
        if symbol in result:
            existing = result[symbol]
            crossback_count = existing.get("crossback_count", 0)
            last_crossback_date = existing.get("last_crossback_date")
        else:
            crossback_count = 0
            last_crossback_date = None

        result[symbol] = {
            "symbol": symbol,
            "signal_date": sig["signal_date"],
            "signal_price": round(sig["signal_price"], 4),
            "stage2_stop": round(sig["stage2_stop"], 4),
            "added_at": today.isoformat(),
            "expires_at": new_expiry,
            "crossback_count": crossback_count,
            "last_crossback_date": last_crossback_date,
        }

    return result


def prune_expired(
    watchlist: dict[str, dict[str, Any]],
    today: datetime,
) -> dict[str, dict[str, Any]]:
    """
    移除 expires_at < today 的条目（boundary case: expires_at == today 保留）。

    Args:
        watchlist: 当前 watchlist
        today: 当前时间

    Returns:
        修剪后的 watchlist（新 dict，不修改入参）
    """
    today_iso = today.isoformat()
    return {
        symbol: dict(entry)
        for symbol, entry in watchlist.items()
        if entry.get("expires_at", "") >= today_iso
    }
