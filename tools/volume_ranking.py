#!/usr/bin/env python3
"""按 20 日均量倍数排名"""
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(ROOT, '.env'))

ALPACA_API_KEY = os.getenv('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')

client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# 今日 7 个买入标的
tickers = ['COEP', 'GAINN', 'ANIX', 'CFFI', 'HIG', 'MBWM', 'TPC']

# 获取最近 30 天的 bars（保险起见多取几天）
end = datetime(2026, 7, 22)
start = end - timedelta(days=30)

# 加载 sector cache
cache_path = os.path.join(ROOT, 'data', 'sector_cache.json')
sector_cache = {}
if os.path.exists(cache_path):
    with open(cache_path) as f:
        sector_cache = json.load(f)

print(f"{'Ticker':<6} {'Industry':<28} {'Today Vol':>12} {'20d Avg':>12} {'倍数':>8} {'今日占比'}")
print('-' * 90)

results = []
for ticker in tickers:
    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment='all',
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            print(f'{ticker:<6} 无数据')
            continue
        if ticker in df.index.get_level_values(0):
            df = df.loc[ticker]
        else:
            print(f'{ticker:<6} 无 ticker 数据')
            continue

        df = df.sort_index()
        if len(df) < 2:
            print(f'{ticker:<6} 数据不足')
            continue

        # 最后一根 = 今天（2026-07-22）
        today_vol = int(df['volume'].iloc[-1])
        # 之前 20 根 = 20 日均量
        prior_20 = df['volume'].iloc[:-1].tail(20)
        avg_vol = prior_20.mean() if len(prior_20) > 0 else 0
        mult = today_vol / avg_vol if avg_vol > 0 else 0

        industry = sector_cache.get(ticker, {}).get('industry', 'Unknown')[:28]

        results.append({
            'ticker': ticker,
            'industry': industry,
            'today_vol': today_vol,
            'avg_vol': int(avg_vol),
            'mult': mult,
        })
    except Exception as e:
        print(f'{ticker:<6} 错误: {type(e).__name__}: {str(e)[:50]}')

# 按倍数降序
results.sort(key=lambda x: -x['mult'])

# 输出表格
print()
print('| 排名 | Ticker | 板块 | 今日量 | 20日均量 | 倍数 | 风险 |')
print('|---|---|---|---|---|---|---|')
for i, r in enumerate(results, 1):
    if r['mult'] >= 5:
        risk = '🔴 异常高'
    elif r['mult'] >= 3:
        risk = '🟡 较高'
    elif r['mult'] >= 2:
        risk = '🟢 正常'
    else:
        risk = '⚪ 偏低'
    print(f"| {i} | **{r['ticker']}** | {r['industry']} | {r['today_vol']:,} | {r['avg_vol']:,} | **{r['mult']:.2f}x** | {risk} |")

print()
print('=' * 60)
print('关键观察:')
print('=' * 60)

if results:
    top = results[0]
    print(f'1. **最高量能: {top["ticker"]} ({top["mult"]:.2f}x)** - {top["industry"]}')
    if top['mult'] >= 5:
        print(f'   ⚠️ 超过 5x 量能，疑似假突破风险')
        print(f'   历史参考:')
        print(f'   - VRAX 7/10 买入 137.5x 量能 → 现 -39.1%')
        print(f'   - QTTB 7/14 买入 22.7x 量能 → 现 -27.9%')

    print(f'2. **最低量能: {results[-1]["ticker"]} ({results[-1]["mult"]:.2f}x)** - {results[-1]["industry"]}')
    if results[-1]['mult'] < 2:
        print(f'   ⚠️ 量能偏低（<2x），突破力度不足')

    avg = sum(r['mult'] for r in results) / len(results)
    print(f'3. **平均倍数: {avg:.2f}x**（7 个买入）')
    high_count = sum(1 for r in results if r['mult'] >= 3)
    print(f'4. **≥3x 高量能: {high_count} 只** / {len(results)}')