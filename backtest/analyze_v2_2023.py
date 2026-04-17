"""
analyze_v2_2023.py — v_eps_v2 在 2023 年的表现
"""
import sys
sys.path.insert(0, '.')

from datetime import datetime, timezone
from backtest.engine import BacktestEngine
from strategies.registry import STRATEGY_REGISTRY
from universe.manager import _read_universe_md
import yaml

with open("config.yaml") as f:
    config = yaml.safe_load(f)

symbols = _read_universe_md()

start = datetime(2023, 1, 1, tzinfo=timezone.utc)
end = datetime(2023, 12, 31, tzinfo=timezone.utc)

cfg = config["strategies"].get("v_eps_v2", {}).copy()

print(f"股票池: {len(symbols)} 只")
print(f"区间: {start.date()} ~ {end.date()}")

engine = BacktestEngine(
    config=config,
    strategy_cls=STRATEGY_REGISTRY["v_eps_v2"],
    strategy_config=cfg,
    symbols=symbols,
    start_date=start,
    end_date=end,
    save_signals_csv=False,
    strategy_id="v_eps_v2",
    position_size=1500,
    initial_capital=50_000.0,
)

result = engine.run(verbose=False)

print(f"\n=== v_eps_v2 2023 年结果 ===")
print(f"  年化收益: {result.annualized_return*100:.1f}%")
print(f"  夏普比率: {result.sharpe_ratio:.2f}")
print(f"  胜率: {result.win_rate*100:.1f}%")
print(f"  盈亏比: {result.profit_loss_ratio:.2f}")
print(f"  最大回撤: {result.max_drawdown*100:.1f}%")
print(f"  总交易数: {result.total_trades}")
print(f"  每月信号数: {result.signals_per_month:.1f}")
