"""
backtest_cigar_efficient.py — 高效版捡烟蒂回测

结合 SEC EDGAR NCAV + NASDAQ市值 + Polygon价格数据
只处理有完整数据的股票，大幅提升速度
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=str(env_path), override=True)

import requests
from strategies.cigar_butt_v1a import CigarButtV1AStrategy
from strategies.cigar_butt_v1b import CigarButtV1BStrategy

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "tnCPh87aCFFoePifS6310VxRKTBiZUPO")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "d61a0n1r01qn5qe73o7gd61a0n1r01qn5qe73o80")

# V1B额外数据缓存（避免重复API调用）
_v1b_financials_cache: dict[str, dict] = {}
_v1b_neg_cfo_quarters: dict[str, int] = {}  # 追踪连续负现金流季度


def _get_v1b_financials(symbol: str, sec_fin: dict) -> dict:
    """获取V1B所需的额外财务数据（净利润、现金流）
    优先使用SEC数据，如果缺失则从Finnhub API补充
    """
    if symbol in _v1b_financials_cache:
        return _v1b_financials_cache[symbol]

    result = {
        'net_income': sec_fin.get('net_income'),
        'operating_cfo': sec_fin.get('operating_cfo'),
        'net_margin': None,  # Finnhub: 净利润率百分比
        'cfo_per_share': None,  # Finnhub: 每股现金流
    }

    # 如果SEC数据缺失，从Finnhub补充
    if result['net_income'] is None or result['operating_cfo'] is None:
        try:
            url = f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_API_KEY}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                d = r.json()
                metrics = d.get('metric', {})

                net_margin = metrics.get('netProfitMarginTTM')
                cfo_per_share = metrics.get('cashFlowPerShareTTM')

                # 存储 margin 和 cfo_per_share 用于判断
                if net_margin is not None:
                    result['net_margin'] = net_margin
                if cfo_per_share is not None:
                    result['cfo_per_share'] = cfo_per_share

                # 尝试估算绝对值（如果 SEC 没有）
                shares_outstanding = metrics.get('sharesOutstanding')
                if shares_outstanding and shares_outstanding > 0:
                    rev_per_share = metrics.get('revenuePerShareTTM', 0)
                    if result['net_income'] is None and net_margin is not None and rev_per_share:
                        estimated_revenue = rev_per_share * shares_outstanding
                        result['net_income'] = (net_margin / 100) * estimated_revenue
                    if result['operating_cfo'] is None and cfo_per_share is not None:
                        result['operating_cfo'] = cfo_per_share * shares_outstanding
        except Exception:
            pass

    _v1b_financials_cache[symbol] = result
    return result


def load_sec_edgar_ncav() -> dict:
    """从SEC EDGAR缓存加载NCAV数据"""
    cache_dir = Path("data/cache")
    ncav_data = {}

    for json_file in cache_dir.glob("sec_edgar_*.json"):
        try:
            with open(json_file, "r") as f:
                data = json.load(f)

            symbol = data.get("symbol", json_file.stem.replace("sec_edgar_", ""))
            facts = data.get("facts", {})

            # 获取关键财务数据 - 修复嵌套结构: units.USD[-1].val
            def get_tag(facts, namespace, tag):
                if namespace in facts and tag in facts[namespace]:
                    units = facts[namespace][tag].get("units", {})
                    if "USD" in units and units["USD"]:
                        return units["USD"][-1]["val"]  # 最新一期的值
                    elif "USD/shares" in units and units["USD/shares"]:
                        return units["USD/shares"][-1]["val"]
                return None

            # 从US-GAAP获取数据
            us_gaap = facts.get("us-gaap", {})

            cash = get_tag(facts, "us-gaap", "CashAndCashEquivalentsAtCarryingValue")
            if not cash:
                cash = get_tag(facts, "us-gaap", "Cash")
            ar = get_tag(facts, "us-gaap", "AccountsReceivable")
            if not ar:
                ar = get_tag(facts, "us-gaap", "AccountsReceivableNetCurrent")
            inventory = get_tag(facts, "us-gaap", "Inventory")
            if not inventory:
                inventory = get_tag(facts, "us-gaap", "InventoryNet")
            current_liabilities = get_tag(facts, "us-gaap", "LiabilitiesCurrent")

            # Graham NCAV公式: Cash + 0.75*AR + 0.5*Inventory - Current Liabilities
            if cash and current_liabilities and cash > 0 and current_liabilities > 0:
                ncav = cash + 0.75 * (ar or 0) + 0.5 * (inventory or 0) - current_liabilities
                if ncav > 0:
                    # V1B需要的数据：净利润和经营现金流
                    net_income = get_tag(facts, "us-gaap", "NetIncomeLoss")
                    operating_cfo = get_tag(facts, "us-gaap", "NetCashProvidedByUsedInOperatingActivities")
                    if not operating_cfo:
                        operating_cfo = get_tag(facts, "us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")

                    ncav_data[symbol] = {
                        'ncav': ncav,
                        'cash': cash,
                        'accounts_receivable': ar or 0,
                        'inventory': inventory or 0,
                        'current_liabilities': current_liabilities,
                        'net_income': net_income,
                        'operating_cfo': operating_cfo,
                    }
        except Exception:
            pass

    print(f"[SEC EDGAR] 加载 {len(ncav_data)} 只股票的NCAV数据")
    return ncav_data


def fetch_nasdaq_market_caps(symbols: list[str]) -> dict:
    """从本地缓存获取市值数据"""
    market_caps = {}

    print("[NASDAQ] 使用本地缓存获取市值...")

    # 首先从 nasdaq_market_caps.json 读取
    cache_file = Path("data/cache/nasdaq_market_caps.json")
    if cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                cached_mcaps = json.load(f)
            for symbol in symbols:
                if symbol in cached_mcaps and cached_mcaps[symbol] > 0:
                    market_caps[symbol] = cached_mcaps[symbol]
            if market_caps:
                print(f"[NASDAQ] 从缓存获取 {len(market_caps)} 只股票的市值")
                return market_caps
        except Exception:
            pass

    # 如果缓存不存在或为空，使用Polygon financials获取市值
    print("[NASDAQ] 缓存为空，使用Polygon API...")
    for symbol in symbols:
        try:
            fin = get_full_financials(symbol)
            if fin and fin.get('market_cap') and fin['market_cap'] > 0:
                market_caps[symbol] = fin['market_cap']
        except Exception:
            pass

    print(f"[NASDAQ] 获取 {len(market_caps)} 只股票的市值")
    return market_caps


def fetch_polygon_prices(symbols: list[str], start_date: datetime, end_date: datetime) -> dict:
    """使用Polygon.io获取多只股票的价格数据"""
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    market_data = {}
    for i, symbol in enumerate(symbols):
        if i > 0 and i % 50 == 0:
            print(f"[Polygon] 已获取 {i}/{len(symbols)} 只...")

        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_str}/{end_str}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                data = r.json()
                results = data.get('results', [])
                if results:
                    df = pd.DataFrame(results)
                    df['date'] = pd.to_datetime(df['t'], unit='ms', utc=True)
                    df.set_index('date', inplace=True)
                    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume', 'vw': 'vwap'})
                    market_data[symbol] = df[['open', 'high', 'low', 'close', 'volume', 'vwap']]
        except Exception:
            pass

        time.sleep(0.05)

    print(f"[Polygon] 获取完成: {len(market_data)}/{len(symbols)} 只")
    return market_data


def load_local_market_data(symbols: list[str], start_date: datetime, end_date: datetime) -> dict:
    """从本地缓存加载市场数据"""
    cache_dir = Path("data/cache")
    market_data = {}
    loaded = 0

    start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
    end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

    for symbol in symbols:
        pkl_path = cache_dir / f"{symbol}.pkl"
        if pkl_path.exists():
            try:
                df = pd.read_pickle(pkl_path)
                # 确保index是tz-aware以匹配backtest中的比较
                if df.index.tz is None:
                    df.index = df.index.tz_localize('UTC')
                # 过滤日期范围
                df_filtered = df[(df.index >= start_date) & (df.index <= end_date)]
                if len(df_filtered) > 0:
                    market_data[symbol] = df_filtered
                    loaded += 1
            except Exception:
                pass

    print(f"[本地] 加载完成: {loaded}/{len(symbols)} 只")
    return market_data


@dataclass
class CigarTrade:
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
    market_data: dict,
    initial_capital: float = 100000.0,
) -> dict:
    """运行简化版回测"""

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

    strategy = strategy_cls(strategy_config, market_data)

    # 回测变量
    cash = initial_capital
    positions = {}
    trades = []
    equity_curve = []
    is_v1b = strategy_name.upper().startswith("V1B")

    # V1B需要追踪连续负现金流季度
    neg_cfo_quarters = {}  # symbol -> consecutive negative CFO quarters

    print(f"[回测] 回测区间: {start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")

    # 每日回测：每天检查买卖信号
    current_date = start_date
    while current_date <= end_date:
        # 更新持仓状态 - 检查卖出信号
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            if symbol in market_data:
                df = market_data[symbol]
                df_up_to = df[df.index <= current_date]
                if len(df_up_to) > 0:
                    current_price = float(df_up_to["close"].iloc[-1])

                    # 更新最高价
                    if current_price > pos.get('highest_price', pos['entry_price']):
                        positions[symbol]['highest_price'] = current_price

                    ncav = pos['ncav']
                    fin = financials_cache.get(symbol, {})
                    market_cap = fin.get('market_cap', 0)

                    # V1B需要额外财务数据（净利润、现金流）
                    if is_v1b:
                        v1b_fin = _get_v1b_financials(symbol, fin)
                        net_income = v1b_fin['net_income']
                        operating_cfo = v1b_fin['operating_cfo']
                        net_margin = v1b_fin.get('net_margin')
                        cfo_per_share = v1b_fin.get('cfo_per_share')
                    else:
                        net_income = fin.get('net_income')
                        operating_cfo = fin.get('operating_cfo')
                        net_margin = None
                        cfo_per_share = None

                    # 计算PE（如果净利润>0）
                    pe_ratio = 0
                    if net_income and net_income > 0 and market_cap > 0:
                        pe_ratio = market_cap / net_income

                    # === V1B 额外卖出条件 ===
                    if is_v1b:
                        # [1] 净利润转负（用绝对值或margin判断）
                        net_is_negative = (net_income is not None and net_income < 0) or \
                                        (net_margin is not None and net_margin < 0)
                        if net_is_negative:
                            proceeds = pos['shares'] * current_price
                            cash += proceeds
                            reason = f"净利润转负"
                            if net_margin is not None:
                                reason += f" (margin={net_margin:.1f}%)"
                            trades.append(CigarTrade(
                                symbol=symbol,
                                entry_date=pos['entry_date'],
                                entry_price=pos['entry_price'],
                                shares=pos['shares'],
                                exit_date=current_date,
                                exit_price=current_price,
                                exit_reason=reason,
                                pnl_pct=current_price / pos['entry_price'] - 1,
                            ))
                            del positions[symbol]
                            if symbol in _v1b_neg_cfo_quarters:
                                del _v1b_neg_cfo_quarters[symbol]
                            continue

                        # [2] 经营现金流连续2+季度为负（用绝对值或per-share判断）
                        cfo_is_negative = (operating_cfo is not None and operating_cfo < 0) or \
                                        (cfo_per_share is not None and cfo_per_share < 0)
                        if cfo_is_negative:
                            _v1b_neg_cfo_quarters[symbol] = _v1b_neg_cfo_quarters.get(symbol, 0) + 1
                            if _v1b_neg_cfo_quarters[symbol] >= 2:
                                proceeds = pos['shares'] * current_price
                                cash += proceeds
                                trades.append(CigarTrade(
                                    symbol=symbol,
                                    entry_date=pos['entry_date'],
                                    entry_price=pos['entry_price'],
                                    shares=pos['shares'],
                                    exit_date=current_date,
                                    exit_price=current_price,
                                    exit_reason=f"经营现金流连续{_v1b_neg_cfo_quarters[symbol]}季度为负",
                                    pnl_pct=current_price / pos['entry_price'] - 1,
                                ))
                                del positions[symbol]
                                del _v1b_neg_cfo_quarters[symbol]
                                continue
                        else:
                            _v1b_neg_cfo_quarters[symbol] = 0

                        # [3] P/E > 25 卖出
                        if pe_ratio > 25:
                            proceeds = pos['shares'] * current_price
                            cash += proceeds
                            trades.append(CigarTrade(
                                symbol=symbol,
                                entry_date=pos['entry_date'],
                                entry_price=pos['entry_price'],
                                shares=pos['shares'],
                                exit_date=current_date,
                                exit_price=current_price,
                                exit_reason=f"P/E={pe_ratio:.1f}>25，高估",
                                pnl_pct=current_price / pos['entry_price'] - 1,
                            ))
                            del positions[symbol]
                            if symbol in neg_cfo_quarters:
                                del neg_cfo_quarters[symbol]
                            continue

                    # === 止损检查 - 固定10%止损 ===
                    stop_loss_pct = 0.10
                    highest_price = pos.get('highest_price', pos['entry_price'])
                    drawdown = (current_price - highest_price) / highest_price
                    if drawdown <= -stop_loss_pct:
                        proceeds = pos['shares'] * current_price
                        cash += proceeds
                        trades.append(CigarTrade(
                            symbol=symbol,
                            entry_date=pos['entry_date'],
                            entry_price=pos['entry_price'],
                            shares=pos['shares'],
                            exit_date=current_date,
                            exit_price=current_price,
                            exit_reason=f"止损-从高点回落{drawdown*100:.1f}%",
                            pnl_pct=current_price / pos['entry_price'] - 1,
                        ))
                        del positions[symbol]
                        if is_v1b and symbol in neg_cfo_quarters:
                            del neg_cfo_quarters[symbol]
                        continue

                    # === P/NCAV > 1.0 卖出 ===
                    if ncav > 0:
                        p_ncav = (pos['shares'] * current_price) / ncav
                        if p_ncav > strategy.sell_p_ncav_threshold:
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
                            if is_v1b and symbol in neg_cfo_quarters:
                                del neg_cfo_quarters[symbol]
                            continue

        # 检查买入信号 - 每日检查，不等rebalance
        if len(positions) < strategy.max_positions:
            candidates = []
            for symbol in symbols:
                if symbol in positions:
                    continue
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

                        if p_ncav < strategy.max_p_ncav:
                            candidates.append({
                                'symbol': symbol,
                                'p_ncav': p_ncav,
                                'ncav': ncav,
                                'price': current_price,
                                'market_cap': market_cap,
                                'fin': fin,
                            })

            candidates.sort(key=lambda x: x['p_ncav'])

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
                                'highest_price': price,  # 追踪最高价
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
            'portfolio_value': portfolio_value,
        })

        # 下一天
        current_date += timedelta(days=1)

    # 回测结束时平所有持仓
    for symbol, pos in positions.items():
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

    rolling_max = equity_df['portfolio_value'].cummax()
    drawdown = (equity_df['portfolio_value'] - rolling_max) / rolling_max
    max_drawdown = abs(drawdown.min()) if len(drawdown) > 0 else 0

    daily_returns = equity_df['portfolio_value'].pct_change().dropna()
    sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() > 0 else 0

    closed_trades = [t for t in trades if t.exit_date is not None]
    winning_trades = [t for t in closed_trades if t.pnl_pct > 0]
    win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0

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

    # 打印交易详情
    if result['trades']:
        print(f"\n  【交易明细】")
        for t in result['trades']:
            exit_date_str = t.exit_date.strftime('%Y-%m-%d') if t.exit_date else 'N/A'
            exit_price_str = f"{t.exit_price:.2f}" if t.exit_price else '0.00'
            print(f"    {t.symbol}: {t.entry_date.strftime('%Y-%m-%d')}@{t.entry_price:.2f} -> {exit_date_str}@{exit_price_str} | {t.exit_reason} | P/L: {t.pnl_pct*100:.1f}%")

    print(f"\n{sep}\n")


def main():
    parser = argparse.ArgumentParser(description="捡烟蒂V1A/V1B高效回测")
    parser.add_argument("--strategy", default="both", choices=["v1a", "v1b", "both"])
    parser.add_argument("--start", default="2026-01-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-04-08", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    args = parser.parse_args()

    # 解析日期
    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        print(f"[错误] 日期格式不正确: {e}")
        sys.exit(1)

    # 1. 加载SEC EDGAR NCAV数据
    print("\n[数据] 加载SEC EDGAR NCAV数据...")
    ncav_data = load_sec_edgar_ncav()
    print(f"[数据] 有效NCAV股票: {len(ncav_data)} 只")

    if len(ncav_data) == 0:
        print("[错误] 没有找到SEC EDGAR NCAV数据")
        sys.exit(1)

    # 2. 加载配置
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    strategies_cfg = config.get("strategies", {})

    # 3. 获取有NCAV数据的股票列表
    symbols_with_ncav = list(ncav_data.keys())
    print(f"[数据] 有NCAV数据的股票: {len(symbols_with_ncav)} 只")

    # 4. 尝试获取市值数据
    print("\n[数据] 获取市值数据...")
    market_caps = fetch_nasdaq_market_caps(symbols_with_ncav)

    # 5. 合并NCAV和市值数据
    financials_cache = {}
    for symbol in symbols_with_ncav:
        if symbol in market_caps and market_caps[symbol] > 0:
            ncav = ncav_data[symbol]['ncav']
            market_cap = market_caps[symbol]
            p_ncav = market_cap / ncav if ncav > 0 else float('inf')

            financials_cache[symbol] = {
                **ncav_data[symbol],
                'market_cap': market_cap,
                'p_ncav': p_ncav,
            }

    print(f"[数据] 有NCAV+市值的股票: {len(financials_cache)} 只")

    # 打印Top 10候选
    sorted_by_pncav = sorted(financials_cache.items(), key=lambda x: x[1].get('p_ncav', 999))
    print("\n[候选] Top 10 P/NCAV最低的股票:")
    for i, (sym, data) in enumerate(sorted_by_pncav[:10]):
        print(f"  {i+1}. {sym}: P/NCAV={data.get('p_ncav', 0):.2f}, NCAV=${data.get('ncav', 0)/1e9:.2f}B, MCap=${data.get('market_cap', 0)/1e9:.2f}B")

    # 6. 加载市场数据（价格）
    print("\n[数据] 加载市场数据...")
    market_data = load_local_market_data(list(financials_cache.keys()), start_date, end_date)

    # 只保留有市场数据的股票
    symbols_with_both = [s for s in financials_cache if s in market_data]
    financials_cache = {s: financials_cache[s] for s in symbols_with_both}
    print(f"[数据] 有NCAV+市值+价格的股票: {len(financials_cache)} 只")

    results = []

    # 回测V1A
    if args.strategy in ["v1a", "both"]:
        v1a_config = strategies_cfg.get("cigar_butt_v1a", {})
        result = run_backtest(
            strategy_name="捡烟蒂V1A-纯NCAV",
            strategy_config=v1a_config,
            symbols=list(financials_cache.keys()),
            start_date=start_date,
            end_date=end_date,
            financials_cache=financials_cache,
            market_data=market_data,
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
            symbols=list(financials_cache.keys()),
            start_date=start_date,
            end_date=end_date,
            financials_cache=financials_cache,
            market_data=market_data,
            initial_capital=args.capital,
        )
        print_report(result)
        results.append(("V1B", result))

    # 对比总结
    if len(results) > 1:
        print("\n" + "=" * 60)
        print("  对比总结")
        print("=" * 60)
        print(f"\n指标                          V1A             V1B")
        print("-" * 50)
        for key in ['total_return', 'annualized_return', 'max_drawdown', 'sharpe_ratio', 'win_rate', 'profit_loss_ratio', 'total_trades']:
            v1 = results[0][1].get(key, 0)
            v2 = results[1][1].get(key, 0)
            if key in ['total_return', 'annualized_return', 'max_drawdown', 'win_rate']:
                print(f"{key:28} {v1*100:6.1f}%         {v2*100:6.1f}%")
            elif key == 'total_trades':
                print(f"{key:28} {v1:6d}          {v2:6d}")
            else:
                print(f"{key:28} {v1:6.2f}          {v2:6.2f}")


if __name__ == "__main__":
    main()
