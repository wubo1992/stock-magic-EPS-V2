"""
extended_backtest.py — v_eps_v2 冠军参数 2010-2026 全段回测
"""
import sys
sys.path.insert(0, '.')

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

from backtest.engine import BacktestEngine
from strategies.registry import STRATEGY_REGISTRY
from backtest.optimizer import load_universe
import yaml

with open("config.yaml") as f:
    config = yaml.safe_load(f)

symbols = load_universe()
start = datetime(2010, 1, 1, tzinfo=timezone.utc)
end = datetime(2026, 3, 27, tzinfo=timezone.utc)

champion_params = {
    'stop_loss_pct': 0.08,
    'trailing_stop_pct': 0.15,
    'time_stop_days': 60,
    'eps_quarters_required': 2,
}

print(f"股票池: {len(symbols)} 只")
print(f"区间: {start.date()} ~ {end.date()}")
print(f"参数: {champion_params}")

cfg = config["strategies"].get("v_eps_v2", {}).copy()
cfg.update(champion_params)

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

print()
print("=== v_eps_v2 冠军参数 2010-2026 回测结果 ===")
print(f"  年化收益: {result.annualized_return*100:.1f}%")
print(f"  夏普比率: {result.sharpe_ratio:.2f}")
print(f"  胜率: {result.win_rate*100:.1f}%")
print(f"  盈亏比: {result.profit_loss_ratio:.2f}")
print(f"  最大回撤: {result.max_drawdown*100:.1f}%")
print(f"  信号/月: {result.signals_per_month:.1f}")
print(f"  总交易数: {result.total_trades}")
print(f"  峰值持仓: {result.max_positions}")
print(f"  平均持仓: {result.avg_positions:.1f}")

# 保存 equity curve
eq = result.equity_curve
eq_df = eq.to_frame(name="equity")
drawdown = (eq - eq.cummax()) / eq.cummax() * 100
eq_df["drawdown_pct"] = drawdown.values
eq_path = Path("output/v_eps_v2/equity_curve_2010_2026.csv")
eq_df.to_csv(eq_path)
print(f"\n净值数据已保存: {eq_path}")
print(f"净值范围: ${eq.iloc[0]:,.0f} → ${eq.iloc[-1]:,.0f}")

# ── 画图 ──────────────────────────────────────────────────────
output_dir = Path("output/v_eps_v2")
output_dir.mkdir(parents=True, exist_ok=True)

fig, axes = plt.subplots(3, 1, figsize=(16, 12),
                         gridspec_kw={"height_ratios": [3, 1, 1]})
fig.suptitle(
    "v_eps_v2 冠军参数 · 2010-2026 全段回测\n"
    f"夏普 {result.sharpe_ratio:.2f} | 年化 {result.annualized_return*100:.1f}% | "
    f"胜率 {result.win_rate*100:.1f}% | 盈亏比 {result.profit_loss_ratio:.2f} | "
    f"最大回撤 {result.max_drawdown*100:.1f}% | 总交易 {result.total_trades}",
    fontsize=13, y=0.98
)

dates = eq.index.to_pydatetime()
equity_vals = eq.values
drawdown_vals = drawdown.values

# 图1：净值曲线
ax1 = axes[0]
ax1.plot(dates, equity_vals, color="steelblue", linewidth=1.2, label="策略净值")
ax1.axhline(50_000, color="gray", linestyle="--", linewidth=1, label="本金 $50,000")
ax1.fill_between(dates, 50_000, equity_vals,
                  where=(equity_vals >= 50_000), alpha=0.2, color="green")
ax1.fill_between(dates, 50_000, equity_vals,
                  where=(equity_vals < 50_000), alpha=0.2, color="red")
ax1.scatter([dates[0]], [equity_vals[0]], color="steelblue", s=40, zorder=5)
ax1.annotate(f"${equity_vals[0]:,.0f}", (dates[0], equity_vals[0]),
              textcoords="offset points", xytext=(5, 5), fontsize=9)
ax1.scatter([dates[-1]], [equity_vals[-1]], color="steelblue", s=40, zorder=5)
ax1.annotate(f"${equity_vals[-1]:,.0f}", (dates[-1], equity_vals[-1]),
              textcoords="offset points", xytext=(5, 5), fontsize=9)
ax1.set_ylabel("组合净值 ($)", fontsize=11)
ax1.set_title("净值曲线", fontsize=11, pad=4)
ax1.grid(True, alpha=0.3)
ax1.legend(loc="upper left", fontsize=9)
ax1.set_xlim(dates[0], dates[-1])

# 图2：回撤
ax2 = axes[1]
ax2.fill_between(dates, 0, drawdown_vals, alpha=0.4, color="red")
ax2.plot(dates, drawdown_vals, color="darkred", linewidth=0.7)
ax2.set_ylabel("回撤 (%)", fontsize=11)
ax2.set_title("回撤曲线", fontsize=11, pad=4)
ax2.grid(True, alpha=0.3)
ax2.set_xlim(dates[0], dates[-1])
max_dd_idx = np.argmin(drawdown_vals)
ax2.scatter([dates[max_dd_idx]], [drawdown_vals[max_dd_idx]], color="darkred", s=40, zorder=5)
ax2.annotate(f"最大 {drawdown_vals[max_dd_idx]:.1f}%",
             (dates[max_dd_idx], drawdown_vals[max_dd_idx]),
             textcoords="offset points", xytext=(5, -10), fontsize=9, color="darkred")

# 图3：资金利用率
daily_return = pd.Series(equity_vals).pct_change().fillna(0).values
peak_equity = equity_vals.max()
avg_usage = result.avg_positions * 1500 / peak_equity if peak_equity > 0 else 0.3
k = 0.5
utilization = avg_usage + k * daily_return
utilization = np.clip(utilization, 0.05, 0.95)

ax3 = axes[2]
ax3.fill_between(dates, 0, utilization * 100, alpha=0.3, color="orange")
ax3.plot(dates, utilization * 100, color="orange", linewidth=0.8)
ax3.axhline(avg_usage * 100, color="darkorange", linestyle="--", linewidth=1,
             label=f"均值 {avg_usage*100:.0f}%")
ax3.set_ylabel("利用率 (%)", fontsize=11)
ax3.set_xlabel("日期", fontsize=11)
ax3.set_title("资金利用率（每日近似）", fontsize=11, pad=4)
ax3.grid(True, alpha=0.3)
ax3.legend(loc="upper right", fontsize=9)
ax3.set_xlim(dates[0], dates[-1])
ax3.set_ylim(0, 100)

for ax in axes:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(2))

plt.tight_layout()
fig_path = output_dir / "full_backtest_2010_2026.png"
plt.savefig(fig_path, dpi=150, bbox_inches="tight")
plt.close()
print(f"图表已保存: {fig_path}")

# 也按年切分，看各年表现
print("\n=== 年度表现 ===")
eq_df["year"] = pd.to_datetime(eq_df.index).year
yearly = {}
for year, grp in eq_df.groupby("year"):
    start_val = grp["equity"].iloc[0]
    end_val = grp["equity"].iloc[-1]
    ret = (end_val - start_val) / start_val * 100
    dd = grp["drawdown_pct"].min()
    yearly[year] = {"return": ret, "max_drawdown": dd, "start": start_val, "end": end_val}
    print(f"  {year}: 收益 {ret:+.1f}% | 最大回撤 {dd:.1f}%")
