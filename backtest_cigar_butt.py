"""
backtest_cigar_butt.py — 捡烟蒂V1A/V1B专用回测脚本

用法：
    # 回测V1A
    uv run python backtest_cigar_butt.py --strategy v1a --start 2026-01-01 --end 2026-04-08

    # 回测V1B
    uv run python backtest_cigar_butt.py --strategy v1b --start 2026-01-01 --end 2026-04-08

    # 同时回测两个
    uv run python backtest_cigar_butt.py --strategy both --start 2026-01-01 --end 2026-04-08
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

# 加载 .env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=str(env_path), override=True)

from data.fetcher import fetch
from data.polygon_financials import get_full_financials
from strategies.cigar_butt_v1a import CigarButtV1AStrategy
from strategies.cigar_butt_v1b import CigarButtV1BStrategy


def get_universe_us_stocks() -> list[str]:
    """从UNIVERSE.md获取美股代码"""
    universe_path = Path("UNIVERSE.md")
    if not universe_path.exists():
        # Fallback to cache scan
        return get_cached_us_stocks_fallback()

    stocks = []
    current_section = ""
    in_us_section = False

    with open(universe_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Track current section
            if line.startswith("## "):
                current_section = line
                # Look for US stock sections (S: 标普500, or other US sections)
                if "美股" in line or "S&P" in line or "标普" in line:
                    in_us_section = True
                else:
                    in_us_section = False
            # Skip header lines and table headers
            if line.startswith("#") or line.startswith("|---") or not line:
                continue
            # Parse table rows: | CODE | Company | Description |
            if line.startswith("|") and in_us_section:
                parts = line.split("|")
                if len(parts) >= 3:
                    code = parts[1].strip()
                    # Valid stock code: uppercase letters, 2-5 chars (skip single letter headers like "代码")
                    if code and code.isupper() and 2 <= len(code) <= 5 and code.isalpha():
                        stocks.append(code)

    print(f"[股票池] 从UNIVERSE.md加载 {len(stocks)} 只美股")
    return stocks


def get_cached_us_stocks_fallback() -> list[str]:
    """从缓存目录获取所有美股代码（备用）"""
    cache_dir = Path("data/cache")
    us_stocks = []
    for f in cache_dir.glob("*.pkl"):
        stem = f.stem
        # 过滤：美股无后缀，非指数，非eps缓存
        if "." not in stem and not stem.startswith("eps_") and not stem.startswith("^"):
            us_stocks.append(stem)
    return sorted(us_stocks)


def get_cached_polygon_stocks() -> list[str]:
    """获取已有Polygon.io缓存的股票（用于快速回测）"""
    cache_dir = Path("data/cache")
    stocks = set()
    for f in cache_dir.glob("polygon_balance_sheet_*.pkl"):
        stem = f.stem
        # polygon_balance_sheet_XXXX.pkl
        symbol = stem.replace("polygon_balance_sheet_", "")
        stocks.add(symbol)
    for f in cache_dir.glob("polygon_financials_*_quarterly.pkl"):
        stem = f.stem
        # polygon_financials_XXXX_quarterly.pkl
        parts = stem.replace("polygon_financials_", "").split("_")
        if parts:
            stocks.add(parts[0])
    print(f"[股票池] 从Polygon缓存加载 {len(stocks)} 只股票")
    return sorted(stocks)


def pre_cache_financials(symbols: list[str]) -> dict:
    """
    预缓存财务数据，避免回测过程中频繁调用API

    返回：
        {symbol: {'ncav': float, 'p_ncav': float, 'market_cap': float, ...}}
    """
    print(f"[数据] 预缓存 {len(symbols)} 只股票的财务数据...")
    results = {}

    for i, symbol in enumerate(symbols):
        if i > 0 and i % 50 == 0:
            print(f"[数据] 已处理 {i}/{len(symbols)} 只...")

        try:
            financials = get_full_financials(symbol)
            if financials and not financials.get('error'):
                ncav = financials.get('ncav', 0)
                bs = financials.get('balance_sheet', {})
                market_cap = financials.get('market_cap', 0)
                if market_cap <= 0:
                    market_cap = ncav * 2  # 保守估算

                results[symbol] = {
                    'ncav': ncav,
                    'market_cap': market_cap,
                    'cash': bs.get('cash', 0),
                    'accounts_receivable': bs.get('accounts_receivable', 0),
                    'inventory': bs.get('inventory', 0),
                    'current_liabilities': bs.get('current_liabilities', 0),
                    'total_liabilities': bs.get('total_liabilities', 0),
                    'total_assets': bs.get('total_assets', 0),
                    'balance_sheet': bs,
                }
        except Exception as e:
            print(f"[数据] {symbol} 失败: {e}")

        # 防限流
        time.sleep(0.1)

    print(f"[数据] 预缓存完成: {len(results)}/{len(symbols)} 只成功")
    return results


@dataclass
class CigarTrade:
    """简化版交易记录"""
    symbol: str
    entry_date: datetime
    entry_price: float
    shares: float
    exit_date: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl_pct: float = 0.0


def run_backtest(
    strategy_name: str,
    strategy_config: dict,
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
    financials_cache: dict,
    initial_capital: float = 100000.0,
) -> dict:
    """
    运行简化版回测（用于捡烟蒂策略）

    逻辑：
    - 每月第一个交易日进行rebalance
    - 按P/NCAV排序选择最低的N只
    - 等权配置
    - P/NCAV > 1.0 或持有超限则卖出
    """

    print(f"\n{'='*60}")
    print(f"回测: {strategy_name}")
    print(f"区间: {start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
    print(f"股票池: {len(symbols)} 只")
    print(f"{'='*60}")

    # 创建策略实例
    if strategy_name.startswith("V1A"):
        strategy_cls = CigarButtV1AStrategy
    else:
        strategy_cls = CigarButtV1BStrategy

    # 准备市场数据
    print(f"\n[数据] 获取市场数据...")
    market_data = fetch(symbols, history_days=365, live_mode=False, end_date=end_date)
    print(f"[数据] 获取到 {len(market_data)} 只股票的数据")

    # 创建策略
    strategy = strategy_cls(strategy_config, market_data)

    # 回测变量
    cash = initial_capital
    positions = {}  # symbol -> {shares, entry_price, entry_date, ncav}
    trades = []
    equity_curve = []

    # 生成回测日期列表（每月初）
    current_date = start_date
    rebalance_dates = []
    while current_date <= end_date:
        if current_date.day <= 5 or current_date == start_date:
            rebalance_dates.append(current_date)
        # 下个月
        if current_date.month == 12:
            current_date = current_date.replace(year=current_date.year + 1, month=1)
        else:
            current_date = current_date.replace(month=current_date.month + 1)

    print(f"[回测] 计划rebalance日期数: {len(rebalance_dates)}")

    # 简化回测：按月模拟
    current_date = start_date
    while current_date <= end_date:
        # 检查是否需要rebalance
        should_rebalance = current_date.day <= 5

        # 更新持仓状态
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            if symbol in market_data:
                df = market_data[symbol]
                # 找到current_date对应的价格
                df_up_to = df[df.index <= current_date]
                if len(df_up_to) > 0:
                    current_price = float(df_up_to["close"].iloc[-1])

                    # 检查退出条件
                    ncav = pos['ncav']
                    market_cap = ncav * (current_price / pos['entry_price']) * (pos['shares'] / pos['shares'])  # 简化

                    # P/NCAV > 1.0 卖出
                    if ncav > 0:
                        p_ncav = (pos['shares'] * current_price) / ncav
                        if p_ncav > strategy.sell_p_ncav_threshold:
                            # 卖出
                            proceeds = pos['shares'] * current_price
                            cash += proceeds
                            trades.append(CigarTrade(
                                symbol=symbol,
                                entry_date=pos['entry_date'],
                                entry_price=pos['entry_price'],
                                shares=pos['shares'],
                                exit_date=current_date,
                                exit_price=current_price,
                                exit_reason=f"P/NCAV={p_ncav:.2f}>1.0 价值回归",
                                pnl_pct=current_price / pos['entry_price'] - 1,
                            ))
                            del positions[symbol]
                            continue

                    # 持有超限则卖出
                    days_held = (current_date - pos['entry_date']).days
                    if days_held >= strategy.max_hold_days:
                        proceeds = pos['shares'] * current_price
                        cash += proceeds
                        trades.append(CigarTrade(
                            symbol=symbol,
                            entry_date=pos['entry_date'],
                            entry_price=pos['entry_price'],
                            shares=pos['shares'],
                            exit_date=current_date,
                            exit_price=current_price,
                            exit_reason=f"持有{days_held}天超限",
                            pnl_pct=current_price / pos['entry_price'] - 1,
                        ))
                        del positions[symbol]

        # Rebalance
        if should_rebalance and len(positions) < strategy.max_positions:
            # 选择候选股票（从financials_cache中筛选P/NCAV最低的）
            candidates = []
            for symbol in symbols:
                if symbol not in financials_cache:
                    continue
                fin = financials_cache[symbol]
                ncav = fin.get('ncav', 0)
                if ncav <= 0:
                    continue

                if symbol in market_data:
                    df = market_data[symbol]
                    df_up_to = df[df.index <= current_date]
                    if len(df_up_to) > 0:
                        current_price = float(df_up_to["close"].iloc[-1])
                        market_cap = fin.get('market_cap', 0)
                        if market_cap <= 0:
                            market_cap = ncav * 2
                        p_ncav = market_cap / ncav

                        # 检查是否满足条件
                        if p_ncav < strategy.max_p_ncav:
                            candidates.append({
                                'symbol': symbol,
                                'p_ncav': p_ncav,
                                'ncav': ncav,
                                'price': current_price,
                                'market_cap': market_cap,
                                'fin': fin,
                            })

            # 按P/NCAV排序
            candidates.sort(key=lambda x: x['p_ncav'])

            # 选择前N只
            num_to_add = strategy.max_positions - len(positions)
            if num_to_add > 0 and candidates:
                position_size = cash / (num_to_add + len(positions)) if (num_to_add + len(positions)) > 0 else 0

                for cand in candidates[:num_to_add]:
                    symbol = cand['symbol']
                    if symbol in positions:
                        continue

                    price = cand['price']
                    shares = int(position_size / price)

                    if shares > 0:
                        cost = shares * price
                        if cost <= cash:
                            cash -= cost
                            positions[symbol] = {
                                'shares': shares,
                                'entry_price': price,
                                'entry_date': current_date,
                                'ncav': cand['ncav'],
                                'p_ncav': cand['p_ncav'],
                            }

        # 记录当日净值
        portfolio_value = cash
        for symbol, pos in positions.items():
            if symbol in market_data:
                df = market_data[symbol]
                df_up_to = df[df.index <= current_date]
                if len(df_up_to) > 0:
                    current_price = float(df_up_to["close"].iloc[-1])
                    portfolio_value += pos['shares'] * current_price

        equity_curve.append({
            'date': current_date,
            'cash': cash,
            'portfolio_value': portfolio_value,
        })

        # 下一天
        current_date += pd.Timedelta(days=1)
        # 跳过周末
        while current_date.weekday() >= 5:
            current_date += pd.Timedelta(days=1)

    # 回测结束，强制平仓
    for symbol, pos in list(positions.items()):
        if symbol in market_data:
            df = market_data[symbol]
            if len(df) > 0:
                current_price = float(df["close"].iloc[-1])
                proceeds = pos['shares'] * current_price
                cash += proceeds
                trades.append(CigarTrade(
                    symbol=symbol,
                    entry_date=pos['entry_date'],
                    entry_price=pos['entry_price'],
                    shares=pos['shares'],
                    exit_date=end_date,
                    exit_price=current_price,
                    exit_reason="回测结束强制平仓",
                    pnl_pct=current_price / pos['entry_price'] - 1,
                ))
    positions.clear()

    # 计算指标
    equity_df = pd.DataFrame(equity_curve)
    equity_df.set_index('date', inplace=True)

    total_return = (equity_df['portfolio_value'].iloc[-1] / initial_capital - 1) if len(equity_df) > 0 else 0
    annualized_return = (1 + total_return) ** (365 / max((end_date - start_date).days, 1)) - 1

    # 最大回撤
    rolling_max = equity_df['portfolio_value'].cummax()
    drawdown = (equity_df['portfolio_value'] - rolling_max) / rolling_max
    max_drawdown = abs(drawdown.min()) if len(drawdown) > 0 else 0

    # 夏普比率（简化）
    daily_returns = equity_df['portfolio_value'].pct_change().dropna()
    sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() > 0 else 0

    # 胜率
    closed_trades = [t for t in trades if t.exit_date is not None]
    winning_trades = [t for t in closed_trades if t.pnl_pct > 0]
    win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0

    # 盈亏比
    avg_win = np.mean([t.pnl_pct for t in winning_trades]) if winning_trades else 0
    avg_loss = abs(np.mean([t.pnl_pct for t in closed_trades if t.pnl_pct < 0])) if [t for t in closed_trades if t.pnl_pct < 0] else 0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    return {
        'strategy_name': strategy_name,
        'start_date': start_date,
        'end_date': end_date,
        'initial_capital': initial_capital,
        'final_value': equity_df['portfolio_value'].iloc[-1] if len(equity_df) > 0 else initial_capital,
        'total_return': total_return,
        'annualized_return': annualized_return,
        'max_drawdown': max_drawdown,
        'sharpe_ratio': sharpe,
        'win_rate': win_rate,
        'profit_loss_ratio': profit_loss_ratio,
        'total_trades': len(closed_trades),
        'equity_curve': equity_df,
        'trades': closed_trades,
    }


def print_report(result: dict) -> None:
    """打印回测报告"""
    sep = "=" * 51

    print(f"\n{sep}")
    print(f"  回测报告 — {result['strategy_name']}")
    print(f"  测试区间：{result['start_date'].strftime('%Y-%m-%d')} 至 {result['end_date'].strftime('%Y-%m-%d')}")
    print(sep)

    print(f"\n  【收益指标】")
    print(f"  初始资金:     ${result['initial_capital']:,.0f}")
    print(f"  最终价值:     ${result['final_value']:,.0f}")
    print(f"  总收益率:     {result['total_return']*100:.1f}%")
    print(f"  年化收益率:   {result['annualized_return']*100:.1f}%")

    print(f"\n  【风险指标】")
    print(f"  最大回撤:     {result['max_drawdown']*100:.1f}%")
    print(f"  夏普比率:     {result['sharpe_ratio']:.2f}")

    print(f"\n  【交易统计】")
    print(f"  总交易次数:   {result['total_trades']}")
    print(f"  胜率:         {result['win_rate']*100:.1f}%")
    print(f"  盈亏比:       {result['profit_loss_ratio']:.2f}")

    print(f"\n{sep}\n")


def main():
    parser = argparse.ArgumentParser(description="捡烟蒂V1A/V1B回测")
    parser.add_argument("--strategy", default="both", choices=["v1a", "v1b", "both"])
    parser.add_argument("--start", default="2026-01-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-04-08", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--quick", action="store_true", help="仅使用已缓存的数据快速回测")
    args = parser.parse_args()

    # 解析日期
    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        print(f"[错误] 日期格式不正确: {e}")
        sys.exit(1)

    # 获取股票池
    if args.quick:
        symbols = get_cached_polygon_stocks()
    else:
        symbols = get_universe_us_stocks()
    print(f"股票池: {len(symbols)} 只")

    # 预缓存财务数据（使用2025年数据）
    if args.quick:
        # 仅使用已缓存的数据
        print("[数据] 使用已缓存的Polygon.io财务数据...")
        financials_cache = {}
        for symbol in symbols:
            try:
                financials = get_full_financials(symbol)
                if financials and not financials.get('error'):
                    bs = financials.get('balance_sheet', {})
                    financials_cache[symbol] = {
                        'ncav': financials.get('ncav', 0),
                        'market_cap': financials.get('market_cap', 0),
                        'cash': bs.get('cash', 0),
                        'accounts_receivable': bs.get('accounts_receivable', 0),
                        'inventory': bs.get('inventory', 0),
                        'current_liabilities': bs.get('current_liabilities', 0),
                        'total_liabilities': bs.get('total_liabilities', 0),
                        'total_assets': bs.get('total_assets', 0),
                        'balance_sheet': bs,
                    }
            except Exception as e:
                print(f"[数据] {symbol} 加载失败: {e}")
        print(f"[数据] 已加载 {len(financials_cache)} 只股票的财务数据")
    else:
        financials_cache = pre_cache_financials(symbols)

    # 加载配置
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    strategies_cfg = config.get("strategies", {})

    results = []

    # 回测V1A
    if args.strategy in ["v1a", "both"]:
        v1a_config = strategies_cfg.get("cigar_butt_v1a", {})
        result = run_backtest(
            strategy_name="捡烟蒂V1A-纯NCAV",
            strategy_config=v1a_config,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            financials_cache=financials_cache,
            initial_capital=args.capital,
        )
        print_report(result)
        results.append(("V1A", result))

    # 回测V1B
    if args.strategy in ["v1b", "both"]:
        v1b_config = strategies_cfg.get("cigar_butt_v1b", {})
        result = run_backtest(
            strategy_name="捡烟蒂V1B-深度价值+盈利过滤",
            strategy_config=v1b_config,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            financials_cache=financials_cache,
            initial_capital=args.capital,
        )
        print_report(result)
        results.append(("V1B", result))

    # 对比总结
    if len(results) > 1:
        print("\n" + "=" * 60)
        print("  对比总结")
        print("=" * 60)
        print(f"\n{'指标':<15} {'V1A':>15} {'V1B':>15}")
        print("-" * 45)
        print(f"{'总收益率':<15} {results[0][1]['total_return']*100:>14.1f}% {results[1][1]['total_return']*100:>14.1f}%")
        print(f"{'年化收益率':<15} {results[0][1]['annualized_return']*100:>14.1f}% {results[1][1]['annualized_return']*100:>14.1f}%")
        print(f"{'最大回撤':<15} {results[0][1]['max_drawdown']*100:>14.1f}% {results[1][1]['max_drawdown']*100:>14.1f}%")
        print(f"{'夏普比率':<15} {results[0][1]['sharpe_ratio']:>15.2f} {results[1][1]['sharpe_ratio']:>15.2f}")
        print(f"{'胜率':<15} {results[0][1]['win_rate']*100:>14.1f}% {results[1][1]['win_rate']*100:>14.1f}%")
        print(f"{'盈亏比':<15} {results[0][1]['profit_loss_ratio']:>15.2f} {results[1][1]['profit_loss_ratio']:>15.2f}")
        print(f"{'交易次数':<15} {results[0][1]['total_trades']:>15} {results[1][1]['total_trades']:>15}")
        print()


if __name__ == "__main__":
    main()
