#!/usr/bin/env python3
"""
分析从 7/1 至今所有放量 (>2.5x) 买入信号的收益情况。

数据源:
- output/daily_2026-07-*.log: 每日 live test 输出，含 [量能] X.Xx
- output/v_eps_v2/positions.json: 当前持仓的入场价
"""
import os
import re
import json
import time
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(ROOT, '.env'))

POLYGON_API_KEY = os.getenv('POLYGON_API_KEY')

LOG_DIR = os.path.join(ROOT, 'output')
POSITIONS_FILE = os.path.join(ROOT, 'output', 'v_eps_v2', 'positions.json')

START_DATE = '2026-07-01'
END_DATE = '2026-07-28'
VOLUME_THRESHOLD = 2.5


def parse_log_buy_signals(log_path):
    """从每日 live test log 提取所有买入信号 + 量能倍数。"""
    if not os.path.exists(log_path):
        return []

    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()

    m = re.search(r'【买入信号】共 \d+ 个\n(.*?)\n【出场信号】', content, re.DOTALL)
    if not m:
        return []

    section = m.group(1)
    results = []
    for line in section.split('\n'):
        line = line.strip()
        if not re.match(r'\d{4}-\d{2}-\d{2}', line):
            continue
        mm = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\S+)\s+[★☆]+\s+\$([\d.]+)\s+(.*)', line)
        if not mm:
            continue
        date = mm.group(1)
        ticker = mm.group(2)
        stop_loss = float(mm.group(3))
        reason = mm.group(4).strip()
        vol_match = re.search(r'\[量能\]\s*([\d.]+)x', reason)
        if not vol_match:
            continue
        vol_mult = float(vol_match.group(1))
        results.append({
            'date': date, 'ticker': ticker,
            'vol_mult': vol_mult, 'stop_loss': stop_loss,
        })
    return results


def parse_log_exit_signals(log_path):
    """从每日 log 提取出场信号 + 入场价 + 当前价。"""
    if not os.path.exists(log_path):
        return []

    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()

    m = re.search(r'【出场信号】共 \d+ 个\n(.*?)(?=\n=+|\n\[输出\]|\Z)', content, re.DOTALL)
    if not m:
        return []

    section = m.group(1)
    results = []
    for line in section.split('\n'):
        line = line.strip()
        if not re.match(r'\d{4}-\d{2}-\d{2}', line):
            continue
        mm = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\S+)\s+（[^）]+）\s+(.*)', line)
        if not mm:
            continue
        date = mm.group(1)
        ticker = mm.group(2)
        reason = mm.group(3).strip()

        # 触发止损：入场$X.XX，当前$Y.YY，跌幅-Z%
        stop_match = re.search(r'入场\$([\d.]+)，当前\$([\d.]+)，跌幅([-\d.]+)%', reason)
        # 追踪止盈：最高$X.XX（+Y%），当前$Z.ZZ
        trail_match = re.search(r'最高\$([\d.]+)\s*（\+?([-\d.]+)%）.*?当前\$([\d.]+)', reason)

        if stop_match:
            results.append({
                'date': date, 'ticker': ticker,
                'entry_price': float(stop_match.group(1)),
                'current_price': float(stop_match.group(2)),
                'return_pct': float(stop_match.group(3)),
            })
        elif trail_match:
            high = float(trail_match.group(1))
            high_pct = float(trail_match.group(2))
            cur = float(trail_match.group(3))
            entry = high / (1 + high_pct / 100) if high_pct > 0 else 0
            ret = (cur - entry) / entry * 100 if entry > 0 else 0
            results.append({
                'date': date, 'ticker': ticker,
                'entry_price': entry,
                'current_price': cur,
                'return_pct': ret,
            })
    return results


def get_latest_prices(tickers):
    """用 Polygon 获取最新收盘价。"""
    if not POLYGON_API_KEY:
        print('WARN: POLYGON_API_KEY not set, using last seen in logs')
        return {}
    prices = {}
    for i, t in enumerate(tickers):
        try:
            url = f'https://api.polygon.io/v2/aggs/ticker/{t}/prev?apiKey={POLYGON_API_KEY}'
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                d = r.json()
                if d.get('results'):
                    prices[t] = d['results'][0]['c']
        except Exception as e:
            print(f'WARN: {t} price fetch failed: {e}')
        if i % 4 == 3 and i < len(tickers) - 1:
            time.sleep(13)  # respect rate limit (5/min)
    return prices


def main():
    # 1. 收集所有 7/1-7/28 的买入信号
    all_buys = []
    for fn in sorted(os.listdir(LOG_DIR)):
        if not (fn.startswith('daily_2026-07-') and fn.endswith('.log')):
            continue
        date_str = fn.replace('daily_', '').replace('.log', '')
        if date_str < START_DATE or date_str > END_DATE:
            continue
        all_buys.extend(parse_log_buy_signals(os.path.join(LOG_DIR, fn)))

    print(f'[1] 共 {len(all_buys)} 条买入信号 (7/1-7/28)')

    # 2. 过滤量能 >= 2.5x
    high_vol_buys = [b for b in all_buys if b['vol_mult'] >= VOLUME_THRESHOLD]
    print(f'[2] 量能 >= {VOLUME_THRESHOLD}x 的买入信号: {len(high_vol_buys)} 条')

    # 3. 收集所有出场信号 (用于已平仓位)
    all_exits = {}
    for fn in sorted(os.listdir(LOG_DIR)):
        if not (fn.startswith('daily_2026-07-') and fn.endswith('.log')):
            continue
        date_str = fn.replace('daily_', '').replace('.log', '')
        if date_str < START_DATE or date_str > END_DATE:
            continue
        for e in parse_log_exit_signals(os.path.join(LOG_DIR, fn)):
            all_exits.setdefault(e['ticker'], []).append(e)

    # 4. positions.json
    with open(POSITIONS_FILE) as f:
        positions = json.load(f)

    # 5. 对每条高量能买入, 找对应出场或当前持仓
    results = []
    for b in high_vol_buys:
        ticker = b['ticker']
        buy_date = b['date']

        # 找首次出场 (>= buy_date)
        exit_for_ticker = None
        if ticker in all_exits:
            later = [e for e in all_exits[ticker] if e['date'] >= buy_date]
            if later:
                exit_for_ticker = min(later, key=lambda x: x['date'])

        pos = positions.get(ticker)
        entry_price = pos['entry_price'] if pos else b['stop_loss']

        if exit_for_ticker:
            status = '已出场'
            close_date = exit_for_ticker['date']
            current_price = exit_for_ticker['current_price']
            return_pct = exit_for_ticker['return_pct']
        elif pos:
            status = '持仓中'
            close_date = '-'
            current_price = None
            return_pct = None
        else:
            status = '无记录'
            close_date = '-'
            current_price = None
            return_pct = None

        results.append({
            'date': buy_date, 'ticker': ticker, 'vol_mult': b['vol_mult'],
            'entry_price': entry_price, 'current_price': current_price,
            'return_pct': return_pct, 'status': status, 'close_date': close_date,
        })

    # 6. 持仓中股票获取最新价
    open_tickers = sorted({r['ticker'] for r in results if r['status'] == '持仓中'})
    print(f'[3] 持仓中 ticker: {len(open_tickers)}')
    if open_tickers:
        print(f'    正在获取最新价... (共 {len(open_tickers)} 只)')
        latest = get_latest_prices(open_tickers)
        for r in results:
            if r['status'] == '持仓中' and r['ticker'] in latest:
                r['current_price'] = latest[r['ticker']]
                if r['entry_price']:
                    r['return_pct'] = (r['current_price'] - r['entry_price']) / r['entry_price'] * 100
        print(f'    获取到 {len(latest)} 个价格')

    # 7. 排序 + 输出
    results.sort(key=lambda x: -x['vol_mult'])

    print()
    print('=' * 110)
    print(f'放量买入信号 (量能 >= {VOLUME_THRESHOLD}x) — 2026-07-01 至 2026-07-28')
    print('=' * 110)
    print(f"{'#':<3} {'买入日':<10} {'Ticker':<6} {'量能':>5} {'入场$':>9} {'当前$':>9} "
          f"{'收益%':>8} {'状态':<8} {'平仓日':<10}")
    print('-' * 110)
    for i, r in enumerate(results, 1):
        cur = f"${r['current_price']:.2f}" if r['current_price'] else '-'
        ret = f"{r['return_pct']:+.1f}%" if r['return_pct'] is not None else '-'
        print(f"{i:<3} {r['date']:<10} {r['ticker']:<6} {r['vol_mult']:>4.1f}x "
              f"${r['entry_price']:>8.2f} {cur:>9} {ret:>8} {r['status']:<8} {r['close_date']:<10}")

    # 8. 统计
    closed = [r for r in results if r['status'] == '已出场']
    open_pos = [r for r in results if r['status'] == '持仓中']
    if closed:
        wins = [r for r in closed if r['return_pct'] > 0]
        losses = [r for r in closed if r['return_pct'] < 0]
        print()
        print('=' * 60)
        print('已平仓统计:')
        print(f'  总数: {len(closed)}')
        print(f'  盈利: {len(wins)} ({len(wins)*100/len(closed):.1f}%)')
        print(f'  亏损: {len(losses)} ({len(losses)*100/len(closed):.1f}%)')
        avg_ret = sum(r['return_pct'] for r in closed) / len(closed)
        print(f'  平均收益: {avg_ret:+.2f}%')
        if wins:
            avg_win = sum(r['return_pct'] for r in wins) / len(wins)
            print(f'  平均盈利: +{avg_win:.2f}%')
        if losses:
            avg_loss = sum(r['return_pct'] for r in losses) / len(losses)
            print(f'  平均亏损: {avg_loss:.2f}%')
    if open_pos:
        with_price = [r for r in open_pos if r['current_price']]
        print()
        print(f'持仓中: {len(open_pos)} (有最新价: {len(with_price)})')
        if with_price:
            avg_ret = sum(r['return_pct'] for r in with_price) / len(with_price)
            print(f'  持仓中平均收益: {avg_ret:+.2f}%')


if __name__ == '__main__':
    main()