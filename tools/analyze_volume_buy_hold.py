#!/usr/bin/env python3
"""
回测：从每只股票的首次放量 (≥2.5x) 信号买入，忽略止损，持有至今 (2026-07-28)。
不复利，仅展示单票从首次放量到今天的累计收益。
"""
import os
import re
import json
import time
import requests
from collections import defaultdict
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
            'stop_loss': float(mm.group(3)),
        })
    return results


def parse_log_exit_signals(log_path):
    """提取出场信号中 (入场$, 当前$)，用于已平仓位的入场价回溯。"""
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()
    m = re.search(r'【出场信号】共 \d+ 个\n(.*?)(?=\n=+|\n\[输出\]|\Z)', content, re.DOTALL)
    if not m:
        return []
    results = []
    for line in m.group(1).split('\n'):
        line = line.strip()
        if not re.match(r'\d{4}-\d{2}-\d{2}', line):
            continue
        mm = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\S+)\s+（[^）]+）\s+(.*)', line)
        if not mm:
            continue
        date, ticker, reason = mm.group(1), mm.group(2), mm.group(3).strip()
        stop_match = re.search(r'入场\$([\d.]+)，当前\$([\d.]+)，跌幅([-\d.]+)%', reason)
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
    """Polygon 获取最新收盘价。"""
    if not POLYGON_API_KEY:
        print('WARN: POLYGON_API_KEY not set')
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
            time.sleep(13)
    return prices


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
    print(f'[1] 总买入信号: {len(all_buys)}')

    # 2. 过滤放量 >= 2.5x
    high_vol = [b for b in all_buys if b['vol_mult'] >= VOLUME_THRESHOLD]
    print(f'[2] 放量 >= {VOLUME_THRESHOLD}x 信号: {len(high_vol)}')

    # 3. 每只 ticker 取 FIRST 放量信号
    first_vol = {}
    for b in high_vol:
        t = b['ticker']
        if t not in first_vol or b['date'] < first_vol[t]['date']:
            first_vol[t] = b
    print(f'[3] 唯一 ticker 数 (按首次放量): {len(first_vol)}')

    # 4. 收集所有出场信号，用于已平仓位回溯入场价
    all_exits = defaultdict(list)
    for fn in sorted(os.listdir(LOG_DIR)):
        if not (fn.startswith('daily_2026-07-') and fn.endswith('.log')):
            continue
        date_str = fn.replace('daily_', '').replace('.log', '')
        if date_str < START_DATE or date_str > END_DATE:
            continue
        for e in parse_log_exit_signals(os.path.join(LOG_DIR, fn)):
            all_exits[e['ticker']].append(e)

    # 5. positions.json 当前持仓的入场价
    with open(POSITIONS_FILE) as f:
        positions = json.load(f)

    # 6. 构建回测结果
    results = []
    for ticker, b in sorted(first_vol.items()):
        entry_date = b['date']
        vol_mult = b['vol_mult']

        # 入场价: 优先 positions.json, 其次 exit 信号中最早的入场价
        entry_price = None
        entry_source = None
        if ticker in positions:
            entry_price = positions[ticker]['entry_price']
            entry_source = 'positions.json'
        else:
            # 从出场信号中找最早入场价
            earliest = None
            for e in all_exits.get(ticker, []):
                if e['entry_price'] > 0:
                    if earliest is None or e['date'] < earliest['date']:
                        earliest = e
            if earliest:
                entry_price = earliest['entry_price']
                entry_source = 'exit_log'

        results.append({
            'ticker': ticker,
            'first_vol_date': entry_date,
            'vol_mult': vol_mult,
            'entry_price': entry_price,
            'entry_source': entry_source,
        })

    # 7. 获取当前价
    tickers = [r['ticker'] for r in results]
    print(f'[4] 正在获取 {len(tickers)} 只 ticker 的最新价...')
    latest_prices = get_latest_prices(tickers)
    print(f'    获取到 {len(latest_prices)} 个价格')

    for r in results:
        cur = latest_prices.get(r['ticker'])
        r['current_price'] = cur
        if cur and r['entry_price']:
            r['return_pct'] = (cur - r['entry_price']) / r['entry_price'] * 100
        else:
            r['return_pct'] = None

    # 8. 按收益排序
    results.sort(key=lambda x: -(x['return_pct'] if x['return_pct'] is not None else -999))

    # 9. 输出
    print()
    print('=' * 120)
    print(f'首次放量买入持有回测 (量能 >= {VOLUME_THRESHOLD}x) — 2026-07-01 至 2026-07-28')
    print(f'入场价: positions.json 当前持仓 OR 出场信号中的入场价 | 卖出价: 今天收盘 | 无止损')
    print('=' * 120)
    print(f"{'#':<3} {'Ticker':<6} {'首次放量日':<12} {'量能':>5} {'入场$':>9} {'当前$':>9} "
          f"{'收益%':>8} {'持仓中':<6} {'入场来源':<15}")
    print('-' * 120)
    for i, r in enumerate(results, 1):
        cur = f"${r['current_price']:.2f}" if r['current_price'] else '-'
        ret = f"{r['return_pct']:+.1f}%" if r['return_pct'] is not None else '-'
        held = '✓' if r['ticker'] in positions else '✗'
        print(f"{i:<3} {r['ticker']:<6} {r['first_vol_date']:<12} {r['vol_mult']:>4.1f}x "
              f"${r['entry_price']:>8.2f} {cur:>9} {ret:>8} {held:<6} {r['entry_source']:<15}")

    # 10. 统计
    valid = [r for r in results if r['return_pct'] is not None]
    if valid:
        wins = [r for r in valid if r['return_pct'] > 0]
        losses = [r for r in valid if r['return_pct'] < 0]
        flat = [r for r in valid if r['return_pct'] == 0]
        avg = sum(r['return_pct'] for r in valid) / len(valid)
        print()
        print('=' * 60)
        print('回测统计:')
        print(f'  总数: {len(valid)} (有完整价格)')
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

        held_now = [r for r in valid if r['ticker'] in positions]
        closed = [r for r in valid if r['ticker'] not in positions]
        print()
        print(f'  仍在持仓: {len(held_now)} (平均 {sum(r["return_pct"] for r in held_now)/len(held_now) if held_now else 0:+.2f}%)')
        print(f'  已平仓 (按入场价回溯): {len(closed)} (平均 {sum(r["return_pct"] for r in closed)/len(closed) if closed else 0:+.2f}%)')


if __name__ == '__main__':
    main()