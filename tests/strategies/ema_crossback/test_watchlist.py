"""Tests for strategies/ema_crossback/watchlist.py CRUD operations."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from strategies.ema_crossback.watchlist import (
    load_watchlist,
    save_watchlist,
    add_signals_to_watchlist,
    prune_expired,
)


def test_load_watchlist_missing_file_returns_empty(tmp_path: Path):
    """When watchlist.json doesn't exist, load_watchlist should return {}."""
    watchlist = load_watchlist(path=tmp_path / "watchlist.json")
    assert watchlist == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    """save_watchlist then load_watchlist should return identical data."""
    path = tmp_path / "watchlist.json"
    data = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-18",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
            "added_at": "2026-08-18T09:37:00+00:00",
            "expires_at": "2026-10-17T09:37:00+00:00",
            "crossback_count": 0,
            "last_crossback_date": None,
        }
    }
    save_watchlist(data, path=path)
    loaded = load_watchlist(path=path)
    assert loaded == data


def test_load_watchlist_corrupt_file_returns_empty_and_backup(tmp_path: Path):
    """When watchlist.json is corrupt, backup as .bak and return empty dict."""
    path = tmp_path / "watchlist.json"
    path.write_text("{ this is not valid JSON")
    watchlist = load_watchlist(path=path)
    assert watchlist == {}
    backup = path.with_suffix(".json.bak")
    assert backup.exists()


def test_add_signals_new_symbol_creates_entry(tmp_path: Path):
    """New symbols should get expires_at = today + expire_days."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    new_signals = [
        {
            "symbol": "CRDL",
            "signal_date": "2026-08-24",
            "signal_price": 1.67,
            "stage2_stop": 1.5364,
        }
    ]
    result = add_signals_to_watchlist({}, new_signals, expire_days=60, today=today)
    assert "CRDL" in result
    entry = result["CRDL"]
    assert entry["signal_price"] == 1.67
    assert entry["stage2_stop"] == 1.5364
    assert entry["crossback_count"] == 0
    assert entry["last_crossback_date"] is None
    # expires_at = today + 60 days
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry


def test_add_signals_existing_symbol_refreshes_metadata(tmp_path: Path):
    """Existing symbol: update signal_date/price/stop AND refresh expires_at."""
    today = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"

    initial = {
        "CRDL": {
            "symbol": "CRDL",
            "signal_date": "2026-08-10",
            "signal_price": 1.50,
            "stage2_stop": 1.38,
            "added_at": "2026-08-10T09:00:00+00:00",
            "expires_at": "2026-10-09T09:00:00+00:00",
            "crossback_count": 1,
            "last_crossback_date": "2026-08-25",
        }
    }
    save_watchlist(initial, path=path)

    new_signals = [
        {"symbol": "CRDL", "signal_date": "2026-09-10", "signal_price": 1.67, "stage2_stop": 1.5364}
    ]
    result = add_signals_to_watchlist(
        load_watchlist(path), new_signals, expire_days=60, today=today
    )
    entry = result["CRDL"]
    assert entry["signal_price"] == 1.67
    assert entry["stage2_stop"] == 1.5364
    assert entry["signal_date"] == "2026-09-10"
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry
    assert entry["crossback_count"] == 1
    assert entry["last_crossback_date"] == "2026-08-25"


def test_add_signals_reactivates_expired_symbol(tmp_path: Path):
    """An expired symbol re-triggered today should be refreshed with new expires_at."""
    today = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    initial = {
        "X": {
            "symbol": "X",
            "signal_date": "2026-07-01",
            "signal_price": 10.0,
            "stage2_stop": 9.2,
            "added_at": "2026-07-01T09:00:00+00:00",
            "expires_at": "2026-08-30T09:00:00+00:00",
            "crossback_count": 2,
            "last_crossback_date": "2026-08-15",
        }
    }
    save_watchlist(initial, path=path)

    new_signals = [{"symbol": "X", "signal_date": "2026-10-01", "signal_price": 12.0, "stage2_stop": 11.04}]
    result = add_signals_to_watchlist(
        load_watchlist(path), new_signals, expire_days=60, today=today
    )
    entry = result["X"]
    assert entry["signal_price"] == 12.0
    expected_expiry = (today + timedelta(days=60)).isoformat()
    assert entry["expires_at"] == expected_expiry
    assert entry["crossback_count"] == 2


def test_add_signals_persists_to_disk(tmp_path: Path):
    """add_signals_to_watchlist returns updated dict; caller persists via save_watchlist."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    path = tmp_path / "watchlist.json"
    result = add_signals_to_watchlist(
        {}, [{"symbol": "ABC", "signal_date": "2026-08-24", "signal_price": 50.0, "stage2_stop": 46.0}],
        expire_days=60, today=today,
    )
    save_watchlist(result, path)
    reloaded = load_watchlist(path)
    assert "ABC" in reloaded


def test_prune_expired_removes_expired_entries(tmp_path: Path):
    """Entries with expires_at < today should be removed."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {
        "EXPIRED": {
            "symbol": "EXPIRED",
            "expires_at": "2026-08-20T09:00:00+00:00",
            "signal_date": "2026-06-20",
        },
        "FRESH": {
            "symbol": "FRESH",
            "expires_at": "2026-10-20T09:00:00+00:00",
            "signal_date": "2026-08-20",
        },
    }
    pruned = prune_expired(watchlist, today=today)
    assert "EXPIRED" not in pruned
    assert "FRESH" in pruned


def test_prune_expired_keeps_entries_exactly_at_today(tmp_path: Path):
    """Entries with expires_at == today should be KEPT (boundary)."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {
        "BOUNDARY": {
            "symbol": "BOUNDARY",
            "expires_at": "2026-08-24T09:00:00+00:00",
            "signal_date": "2026-08-20",
        },
    }
    pruned = prune_expired(watchlist, today=today)
    assert "BOUNDARY" in pruned


def test_prune_expired_returns_new_dict(tmp_path: Path):
    """prune_expired should not mutate the input watchlist."""
    today = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    watchlist = {"X": {"symbol": "X", "expires_at": "2026-01-01T00:00:00+00:00"}}
    original_copy = dict(watchlist)
    prune_expired(watchlist, today=today)
    assert watchlist == original_copy
