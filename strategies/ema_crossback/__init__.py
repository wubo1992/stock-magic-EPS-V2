"""EMA 10 crossback strategy + watch list."""
from strategies.ema_crossback.watchlist import (
    load_watchlist,
    save_watchlist,
    add_signals_to_watchlist,
    prune_expired,
)
from strategies.ema_crossback.ema_crossback_strategy import (
    detect_crossback,
    run_ema_crossback_strategy,
)

__all__ = [
    "load_watchlist",
    "save_watchlist",
    "add_signals_to_watchlist",
    "prune_expired",
    "detect_crossback",
    "run_ema_crossback_strategy",
]
