#!/usr/bin/env python3
"""
回测：从每只股票的首次放量 (≥2.5x) 信号当天买入，忽略止损，持有至今 (2026-07-28)。

入场价 = 首次放量当天的收盘价 (Polygon)
当前价 = 2026-07-28 的收盘价 (Alpaca 最新 bars)
"""
import os
import re
import json
import time
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(ROOT, '.env'))

POLYGON_API_KEY = os.getenv('POLYGON_API_KEY')
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')

LOG_DIR = os.path.join(ROOT, 'output')
POSITIONS_FILE = os.path.join(ROOT, 'output', 'v_eps_v2', 'positions.json')

START_DATE = '2026-07-01'
END_DATE = '2026-07-28'
VOLUME_THRESHOLD = 2.5

alpaca_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY) if ALPACA_API_KEY else None


def parse_log_buy_signals(log_path):
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()
    m = re.search(r'【买入信号】共 \d+ 个\n(.*?)\n【出场信号】', content, re.DOTALL)
    if not m:
        return []
    results = []
    for line in m.group(1).split('\n'):
        line = line.strip()
        if not re.match(r'\d{4}-\d{2}-\d{2}', line):
            continue
        mm = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\S+)\s+[★☆]+\s+\$([\d.]+)\s+(.*)', line)
        if not mm:
            continue
        reason = mm.group(4).strip()
        vol_match = re.search(r'\[量能\]\s*([\d.]+)x', reason)
        if not vol_match:
            continue
        results.append({
            'date': mm.group(1),
            'ticker': mm.group(2),
            'vol_mult': float(vol_match.group(1)),
        })
    return results


def polygon_get_close(ticker, date_str):
    """Polygon 获取指定日期的收盘价。"""
    if not POLYGON_API_KEY:
        return None
    try:
        url = (f'https://api.polygon.io/v1/open-close/{ticker}/{date_str}'
               f'?adjusted=true&apiKey={POLYGON_API_KEY}')
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'OK' and 'close' in d:
                return d['close']
        return None
    except Exception as e:
        return None


def alpaca_get_close_on(ticker, date_str):
    """从 Alpaca IEX 获取指定日期的收盘价。"""
    if not alpaca_client:
        return None
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=d,
            end=d + timedelta(days=1),
            adjustment='all',
            feed='iex',
        )
        bars = alpaca_client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        if ticker in df.index.get_level_values(0):
            df = df.loc[ticker]
        else:
            return None
        if len(df) > 0:
            return float(df['close'].iloc[-1])
        return None
    except Exception:
        return None


def alpaca_get_latest_close(ticker):
    """从 Alpaca IEX 获取最新收盘价 (可能不包含今日)。"""
    if not alpaca_client:
        return None
    try:
        end = datetime.strptime(END_DATE, '%Y-%m-%d')
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=end - timedelta(days=10),
            end=end + timedelta(days=1),
            adjustment='all',
            feed='iex',
        )
        bars = alpaca_client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        if ticker in df.index.get_level_values(0):
            df = df.loc[ticker]
        else:
            return None
        if len(df) > 0:
            return float(df['close'].iloc[-1])
        return None
    except Exception:
        return None


def main():
    # 1. 收集所有放量买入信号
    all_buys = []
    for fn in sorted(os.listdir(LOG_DIR)):
        if not (fn.startswith('daily_2026-07-') and fn.endswith('.log')):
            continue
        date_str = fn.replace('daily_', '').replace('.log', '')
        if date_str < START_DATE or date_str > END_DATE:
            continue
        all_buys.extend(parse_log_buy_signals(os.path.join(LOG_DIR, fn)))

    high_vol = [b for b in all_buys if b['vol_mult'] >= VOLUME_THRESHOLD]
    print(f'[1] 放量 >= {VOLUME_THRESHOLD}x 信号: {len(high_vol)}')

    # 2. 取每只 ticker 的 FIRST 放量日期
    first_vol = {}
    for b in high_vol:
        t = b['ticker']
        if t not in first_vol or b['date'] < first_vol[t]['date']:
            first_vol[t] = b
    print(f'[2] 唯一 ticker 数: {len(first_vol)}')

    # 3. positions.json
    with open(POSITIONS_FILE) as f:
        positions = json.load(f)

    # 4. 获取每只 ticker 的入场价 (Polygon 首次放量日) 和当前价 (Alpaca 最新)
    results = []
    for ticker, b in sorted(first_vol.items()):
        entry_date = b['date']
        vol_mult = b['vol_mult']

        # 入场价: 优先 Polygon, fallback Alpaca
        entry_price = polygon_get_close(ticker, entry_date)
        if entry_price is None:
            entry_price = alpaca_get_close_on(ticker, entry_date)

        # 当前价: 2026-07-28 收盘 (Alpaca 最新)
        current_price = alpaca_get_latest_close(ticker)

        if entry_price and current_price:
            ret = (current_price - entry_price) / entry_price * 100
        else:
            ret = None

        results.append({
            'ticker': ticker,
            'first_vol_date': entry_date,
            'vol_mult': vol_mult,
            'entry_price': entry_price,
            'current_price': current_price,
            'return_pct': ret,
            'held_now': ticker in positions,
        })

        ep_str = f"${entry_price:.2f}" if entry_price else '-'
        cp_str = f"${current_price:.2f}" if current_price else '-'
        ret_str = f"{ret:+.1f}%" if ret is not None else '-'
        print(f'  {ticker:<6} {entry_date} 量能{vol_mult:>4.1f}x 入场{ep_str:<8} 当前{cp_str:<8} 收益{ret_str}')

    # 5. 按收益排序
    results.sort(key=lambda x: -(x['return_pct'] if x['return_pct'] is not None else -999))

    # 6. 输出
    print()
    print('=' * 110)
    print(f'首次放量买入持有回测 (量能 >= {VOLUME_THRESHOLD}x) — 2026-07-01 至 2026-07-28')
    print(f'入场价: 首次放量日收盘价 | 当前价: 2026-07-28 收盘价 (Alpaca) | 无止损')
    print('=' * 110)
    print(f"{'#':<3} {'Ticker':<6} {'首次放量':<12} {'量能':>5} {'入场$':>9} {'当前$':>9} "
          f"{'收益%':>8} {'持仓中':<6}")
    print('-' * 110)
    for i, r in enumerate(results, 1):
        ep = f"${r['entry_price']:.2f}" if r['entry_price'] else '-'
        cp = f"${r['current_price']:.2f}" if r['current_price'] else '-'
        ret = f"{r['return_pct']:+.1f}%" if r['return_pct'] is not None else '-'
        held = '✓' if r['held_now'] else '✗'
        print(f"{i:<3} {r['ticker']:<6} {r['first_vol_date']:<12} {r['vol_mult']:>4.1f}x "
              f"{ep:>9} {cp:>9} {ret:>8} {held:<6}")

    # 7. 统计
    valid = [r for r in results if r['return_pct'] is not None]
    if valid:
        wins = [r for r in valid if r['return_pct'] > 0]
        losses = [r for r in valid if r['return_pct'] < 0]
        flat = [r for r in valid if r['return_pct'] == 0]
        avg = sum(r['return_pct'] for r in valid) / len(valid)
        print()
        print('=' * 60)
        print('回测统计:')
        print(f'  完整价格: {len(valid)} / {len(results)}')
        print(f'  盈利: {len(wins)} ({len(wins)*100/len(valid):.1f}%)')
        print(f'  亏损: {len(losses)} ({len(losses)*100/len(valid):.1f}%)')
        print(f'  持平: {len(flat)}')
        print(f'  平均收益: {avg:+.2f}%')
        if wins:
            avg_win = sum(r['return_pct'] for r in wins) / len(wins)
            best = max(wins, key=lambda x: x['return_pct'])
            print(f'  平均盈利: +{avg_win:.2f}% | 最佳: {best["ticker"]} {best["return_pct"]:+.1f}%')
        if losses:
            avg_loss = sum(r['return_pct'] for r in losses) / len(losses)
            worst = min(losses, key=lambda x: x['return_pct'])
            print(f'  平均亏损: {avg_loss:.2f}% | 最差: {worst["ticker"]} {worst["return_pct"]:+.1f}%')


if __name__ == '__main__':
    main()