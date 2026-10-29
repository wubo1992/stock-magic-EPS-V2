#!/usr/bin/env python3
"""
板块轮动分析 - 基于 signals.csv 和 positions.json

数据源：
- signals.csv: v_eps_v2 策略历史买入/出场信号
- positions.json: 当前持仓
- Finnhub stock/profile2: ticker -> finnhubIndustry
- INDUSTRY_TO_GICS: Finnhub industry -> GICS Level 1 (11 个板块)

输出：
- data/sector_cache.json: ticker -> {industry, name, market_cap} 缓存
- output/sector_rotation/rotation_YYYY-MM-DD.md: 板块轮动报告
"""

import os
import sys
import json
import csv
import time
import requests
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv

# --- 路径 ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(ROOT, '.env'))

CACHE_FILE = os.path.join(ROOT, 'data', 'sector_cache.json')
SIGNALS_FILE = os.path.join(ROOT, 'output', 'v_eps_v2', 'signals.csv')
POSITIONS_FILE = os.path.join(ROOT, 'output', 'v_eps_v2', 'positions.json')
REPORT_DIR = os.path.join(ROOT, 'output', 'sector_rotation')

# 注意：这里不能在 import 期做 sys.exit —— data/rs.py 在模块加载时会 import 本文件，
# 无 key 的干净环境（比如只跑单测）会直接 SystemExit，连 pytest 都进不去。
# 改成在 fetch_profile() 里惰性检查，缺 key 返回 None（与 data/finnhub_metrics.py 同款做法）。
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', '')

# --- Finnhub industry -> GICS Level 1 (11 板块) ---
# Finnhub 行业名称接近 GICS Level 2/3，这里聚合到 Level 1
INDUSTRY_TO_GICS = {
    # === Information Technology ===
    'Technology': 'Information Technology',
    'Software': 'Information Technology',
    'Software—Application': 'Information Technology',
    'Software—Infrastructure': 'Information Technology',
    'Semiconductors': 'Information Technology',
    'Semiconductor Equipment & Materials': 'Information Technology',
    'Computer Hardware': 'Information Technology',
    'Electronic Components': 'Information Technology',
    'Electronics & Computer Distribution': 'Information Technology',
    'IT Services': 'Information Technology',
    'Information Technology Services': 'Information Technology',
    'Communication Equipment': 'Information Technology',
    'Computer & Electronics Distribution': 'Information Technology',
    'Scientific & Technical Instruments': 'Information Technology',
    'Solar': 'Information Technology',

    # === Financials ===
    'Banking': 'Financials',
    'Banks—Regional': 'Financials',
    'Banks—Diversified': 'Financials',
    'Banks': 'Financials',
    'Financial Services': 'Financials',
    'Financial Data & Stock Exchanges': 'Financials',
    'Capital Markets': 'Financials',
    'Insurance': 'Financials',
    'Insurance—Life': 'Financials',
    'Insurance—Property & Casualty': 'Financials',
    'Insurance—Diversified': 'Financials',
    'Insurance—Reinsurance': 'Financials',
    'Insurance—Specialty': 'Financials',
    'Insurance Brokerage': 'Financials',
    'Asset Management': 'Financials',
    'Asset Management—Mutual Funds': 'Financials',
    'Asset Management—Custody': 'Financials',
    'Mortgage Finance': 'Financials',
    'Credit Services': 'Financials',
    'Shell Companies': 'Financials',
    'Savings & Cooperative Banks': 'Financials',

    # === Health Care ===
    'Healthcare': 'Health Care',
    'Biotechnology': 'Health Care',
    'Drug Manufacturers—General': 'Health Care',
    'Drug Manufacturers—Specialty & Generic': 'Health Care',
    'Medical Devices': 'Health Care',
    'Medical Instruments & Supplies': 'Health Care',
    'Medical Care Facilities': 'Health Care',
    'Medical Distribution': 'Health Care',
    'Healthcare Plans': 'Health Care',
    'Diagnostics & Research': 'Health Care',
    'Pharmaceutical Retailers': 'Health Care',
    'Health Information Services': 'Health Care',
    'Medical Healthcare': 'Health Care',

    # === Consumer Discretionary ===
    'Consumer Cyclical': 'Consumer Discretionary',
    'Auto Manufacturers': 'Consumer Discretionary',
    'Auto Parts': 'Consumer Discretionary',
    'Restaurants': 'Consumer Discretionary',
    'Retail—Apparel & Specialty': 'Consumer Discretionary',
    'Retail—Cyclical': 'Consumer Discretionary',
    'Home Improvement Retail': 'Consumer Discretionary',
    'Specialty Retail': 'Consumer Discretionary',
    'Department Stores': 'Consumer Discretionary',
    'Discount Stores': 'Consumer Discretionary',
    'Luxury Goods': 'Consumer Discretionary',
    'Residential Construction': 'Consumer Discretionary',
    'Textile Manufacturing': 'Consumer Discretionary',
    'Footwear & Accessories': 'Consumer Discretionary',
    'Furnishings, Fixtures & Appliances': 'Consumer Discretionary',
    'Gambling, Resorts & Casinos': 'Consumer Discretionary',
    'Leisure': 'Consumer Discretionary',
    'Hotels & Motels': 'Consumer Discretionary',
    'Recreational Vehicles': 'Consumer Discretionary',
    'Travel Services': 'Consumer Discretionary',
    'Entertainment': 'Consumer Discretionary',
    'Automobiles': 'Consumer Discretionary',
    'Auto & Truck Dealerships': 'Consumer Discretionary',
    'Education & Training Services': 'Consumer Discretionary',
    'Personal Services': 'Consumer Discretionary',
    'Photography/Imaging Services': 'Consumer Discretionary',

    # === Consumer Staples ===
    'Consumer Defensive': 'Consumer Staples',
    'Beverages—Brewers': 'Consumer Staples',
    'Beverages—Wineries & Distilleries': 'Consumer Staples',
    'Beverages—Non-Alcoholic': 'Consumer Staples',
    'Food Distribution': 'Consumer Staples',
    'Grocery Stores': 'Consumer Staples',
    'Packaged Foods': 'Consumer Staples',
    'Confectioners': 'Consumer Staples',
    'Tobacco': 'Consumer Staples',
    'Household & Personal Products': 'Consumer Staples',
    'Agricultural Inputs': 'Consumer Staples',
    'Farm Products': 'Consumer Staples',
    'Education': 'Consumer Staples',

    # === Energy ===
    'Energy': 'Energy',
    'Oil & Gas E&P': 'Energy',
    'Oil & Gas Integrated': 'Energy',
    'Oil & Gas Midstream': 'Energy',
    'Oil & Gas Refining & Marketing': 'Energy',
    'Oil & Gas Equipment & Services': 'Energy',
    'Oil & Gas Drilling': 'Energy',
    'Thermal Coal': 'Energy',
    'Uranium': 'Energy',

    # === Industrials ===
    'Industrials': 'Industrials',
    'Aerospace & Defense': 'Industrials',
    'Airlines': 'Industrials',
    'Airports & Air Services': 'Industrials',
    'Railroads': 'Industrials',
    'Trucking': 'Industrials',
    'Marine Shipping': 'Industrials',
    'Construction & Engineering': 'Industrials',
    'Engineering & Construction': 'Industrials',
    'Construction Machinery & Vehicles': 'Industrials',
    'Industrial Distribution': 'Industrials',
    'Machinery': 'Industrials',
    'Specialty Industrial Machinery': 'Industrials',
    'Farm & Heavy Construction Machinery': 'Industrials',
    'Tools & Accessories': 'Industrials',
    'Electrical Equipment & Parts': 'Industrials',
    'Pollution & Treatment Controls': 'Industrials',
    'Building Products & Equipment': 'Industrials',
    'Conglomerates': 'Industrials',
    'Staffing & Employment Services': 'Industrials',
    'Rental & Leasing Services': 'Industrials',
    'Security & Protection Services': 'Industrials',
    'Business Equipment & Supplies': 'Industrials',
    'Transportation & Logistics': 'Industrials',
    'Freight & Logistics': 'Industrials',
    'Waste Management': 'Industrials',
    'Aviation': 'Industrials',
    'Manufacturing': 'Industrials',
    'Metal Fabrication': 'Industrials',

    # === Communication Services ===
    'Communication Services': 'Communication Services',
    'Telecom Services': 'Communication Services',
    'Telecom—Wireless': 'Communication Services',
    'Telecom—Fiber Optic': 'Communication Services',
    'Media': 'Communication Services',
    'Internet Content & Information': 'Communication Services',
    'Publishing': 'Communication Services',
    'Broadcasting': 'Communication Services',
    'Advertising Agencies': 'Communication Services',
    'Electronic Gaming & Multimedia': 'Communication Services',
    'Telecommunications': 'Communication Services',

    # === Real Estate ===
    'Real Estate': 'Real Estate',
    'Real Estate—Development': 'Real Estate',
    'Real Estate—Diversified': 'Real Estate',
    'Real Estate Services': 'Real Estate',
    'REIT—Diversified': 'Real Estate',
    'REIT—Healthcare Facilities': 'Real Estate',
    'REIT—Hotel & Motel': 'Real Estate',
    'REIT—Industrial': 'Real Estate',
    'REIT—Mortgage': 'Real Estate',
    'REIT—Office': 'Real Estate',
    'REIT—Residential': 'Real Estate',
    'REIT—Retail': 'Real Estate',
    'REIT—Specialty': 'Real Estate',

    # === Materials ===
    'Materials': 'Materials',
    'Steel': 'Materials',
    'Aluminum': 'Materials',
    'Copper': 'Materials',
    'Gold': 'Materials',
    'Silver': 'Materials',
    'Other Precious Metals & Mining': 'Materials',
    'Other Industrial Metals & Mining': 'Materials',
    'Chemicals': 'Materials',
    'Specialty Chemicals': 'Materials',
    'Cement & Concrete': 'Materials',
    'Paper & Paper Products': 'Materials',
    'Lumber & Wood Production': 'Materials',
    'Packaging & Container': 'Materials',
    'Building Materials': 'Materials',
    'Mining': 'Materials',
    'Metals & Mining': 'Materials',

    # === Utilities ===
    'Utilities': 'Utilities',
    'Utilities—Regulated Electric': 'Utilities',
    'Utilities—Regulated Gas': 'Utilities',
    'Utilities—Regulated Water': 'Utilities',
    'Utilities—Renewable': 'Utilities',
    'Utilities—Independent Power Producers': 'Utilities',
    'Utilities—Diversified': 'Utilities',

    # === Finnhub 备用 industry（粗分类，对应 GICS 板块）===
    # 以下是 Finnhub 返回的 industry 但不在标准分类中
    # 按 GICS Level 1 板块归类
    'Electrical Equipment': 'Industrials',
    'Health Care': 'Health Care',
    'Commercial Services & Supplies': 'Industrials',
    'Pharmaceuticals': 'Health Care',
    'Drug & Pharmaceutical': 'Health Care',
    'Food Products': 'Consumer Staples',
    'Retail': 'Consumer Discretionary',
    'Road & Rail': 'Industrials',
    'Railroads': 'Industrials',
    'Communications': 'Communication Services',
    'Hotels, Restaurants & Leisure': 'Consumer Discretionary',
    'Diversified Consumer Services': 'Consumer Discretionary',
    'Textiles, Apparel & Luxury Goods': 'Consumer Discretionary',
    'Apparel & Luxury Goods': 'Consumer Discretionary',
    'Construction': 'Industrials',
    'Construction & Engineering': 'Industrials',
    'Engineering & Construction': 'Industrials',
    'Consumer products': 'Consumer Discretionary',
    'Internet Retail': 'Consumer Discretionary',
    'Specialty Retail': 'Consumer Discretionary',
    'Apparel Retail': 'Consumer Discretionary',
    'Food & Staples Retailing': 'Consumer Staples',
    'Beverages': 'Consumer Staples',
    'Tobacco': 'Consumer Staples',
    'Aerospace & Defense': 'Industrials',
    'Airlines': 'Industrials',
    'Auto Components': 'Consumer Discretionary',
    'Automobiles': 'Consumer Discretionary',
    'Trading Companies & Distributors': 'Industrials',
    'Distributors': 'Consumer Discretionary',
    'Professional Services': 'Industrials',
    'Marine': 'Industrials',
    'Transportation': 'Industrials',
    'Transportation Infrastructure': 'Industrials',
    'Wireless Telecommunication Services': 'Communication Services',
    'Diversified Telecommunication Services': 'Communication Services',
    'Media': 'Communication Services',
    'Entertainment': 'Communication Services',
    'Interactive Media & Services': 'Communication Services',
    'Software & Services': 'Information Technology',
    'Technology Hardware, Storage & Peripherals': 'Information Technology',
    'Semiconductors & Semiconductor Equipment': 'Information Technology',
    'Consumer Electronics': 'Consumer Discretionary',
    'Household Durables': 'Consumer Discretionary',
    'Household Products': 'Consumer Staples',
    'Personal Products': 'Consumer Staples',
    'Food Products': 'Consumer Staples',
    'Tobacco': 'Consumer Staples',
    'Health Care Equipment & Services': 'Health Care',
    'Health Care Providers & Services': 'Health Care',
    'Health Care Technology': 'Health Care',
    'Biotechnology': 'Health Care',
    'Life Sciences Tools & Services': 'Health Care',
    'Pharmaceuticals': 'Health Care',
    'Banks': 'Financials',
    'Diversified Financial Services': 'Financials',
    'Consumer Finance': 'Financials',
    'Capital Markets': 'Financials',
    'Insurance': 'Financials',
    'Real Estate Management & Development': 'Real Estate',
    'Real Estate': 'Real Estate',
    'Equity Real Estate Investment Trusts (REITs)': 'Real Estate',
    'Metals & Mining': 'Materials',
    'Paper & Forest Products': 'Materials',
    'Construction Materials': 'Materials',
    'Chemicals': 'Materials',
    'Containers & Packaging': 'Materials',
    'Energy Equipment & Services': 'Energy',
    'Oil, Gas & Consumable Fuels': 'Energy',
}


def industry_to_gics(industry):
    """Map Finnhub industry to GICS Level 1 sector."""
    if not industry or industry == 'Unknown':
        return 'Unknown'
    if industry in INDUSTRY_TO_GICS:
        return INDUSTRY_TO_GICS[industry]
    # Case-insensitive fallback
    for key, sector in INDUSTRY_TO_GICS.items():
        if industry.lower() == key.lower():
            return sector
    return 'Unmapped'


def fetch_profile(symbol, max_retries=4):
    """Fetch company profile from Finnhub with retry/backoff."""
    if not FINNHUB_API_KEY:
        print('ERROR: FINNHUB_API_KEY 未在 .env 配置，无法拉取板块数据', file=sys.stderr)
        return None
    url = f'https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={FINNHUB_API_KEY}'
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                d = r.json()
                if d and 'finnhubIndustry' in d:
                    return {
                        'industry': d.get('finnhubIndustry', ''),
                        'name': d.get('name', ''),
                        'market_cap': d.get('marketCapitalization', 0),
                    }
                # Empty response (e.g., delisted ticker)
                return {'industry': 'Unknown', 'name': '', 'market_cap': 0, 'empty': True}
            if r.status_code == 429:
                time.sleep(5 + attempt * 5)
                continue
            time.sleep(1)
        except (requests.exceptions.SSLError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError):
            time.sleep(2 + attempt * 3)
        except Exception:
            time.sleep(1)
    return None


def load_cache(path=None):
    """读取板块缓存。path 省略时用默认的 data/sector_cache.json。"""
    target = path or CACHE_FILE
    if os.path.exists(target):
        with open(target) as f:
            return json.load(f)
    return {}


def save_cache(cache, path=None):
    """保存板块缓存。path 省略时用默认的 data/sector_cache.json。"""
    target = path or CACHE_FILE
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fetch_missing_tickers(tickers, cache):
    """Fetch profiles for tickers not in cache. Returns updated cache + list of failures."""
    missing = [t for t in tickers if t not in cache]
    if not missing:
        print(f'[sector] Cache 已覆盖全部 {len(tickers)} tickers')
        return cache, []

    print(f'[sector] Cache 有 {len(cache)}，缺 {len(missing)} 只 ticker')

    failures = []
    consecutive_failures = 0
    for i, ticker in enumerate(missing):
        profile = fetch_profile(ticker)
        if profile:
            cache[ticker] = profile
            consecutive_failures = 0
            if i < 10 or i % 50 == 0:
                print(f'  [{i+1}/{len(missing)}] {ticker:6s} -> {profile["industry"]:35s} | {profile["name"][:30]}')
        else:
            cache[ticker] = {'industry': 'Unknown', 'name': '', 'market_cap': 0, 'fetch_failed': True}
            failures.append(ticker)
            consecutive_failures += 1
            print(f'  [{i+1}/{len(missing)}] {ticker:6s} -> FETCH FAILED')

        # Rate limit: 60 calls/min = 1/sec
        time.sleep(1.05)

        # Save cache periodically
        if (i + 1) % 20 == 0:
            save_cache(cache)
            print(f'[sector] 缓存已保存 ({len(cache)} entries)')

        # Backoff if many consecutive failures
        if consecutive_failures >= 5:
            wait = min(60, 10 * consecutive_failures)
            print(f'[sector] 连续 {consecutive_failures} 次失败，等待 {wait}s 后继续')
            time.sleep(wait)

    save_cache(cache)
    return cache, failures


def load_signals():
    """Load signals.csv (with UTF-8 BOM handling)."""
    rows = []
    with open(SIGNALS_FILE, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for r in reader:
            # Normalize keys (strip BOM from first key if present)
            r = {k.lstrip('﻿'): v for k, v in r.items()}
            rows.append(r)
    return rows


def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return {}
    with open(POSITIONS_FILE) as f:
        return json.load(f)


def compute_signals_by_date_sector(signals, cache):
    """For each (date, sector), compute buy count, exit count, ticker lists."""
    by_date_sector = defaultdict(lambda: {'buy': [], 'exit': []})
    sector_industry_map = defaultdict(set)
    unmapped_tickers = {}

    for r in signals:
        ticker = r['股票']
        sig = r['信号']
        date = r['日期']

        profile = cache.get(ticker, {})
        industry = profile.get('industry', 'Unknown')
        sector = industry_to_gics(industry)

        sector_industry_map[sector].add(industry)
        if sector == 'Unmapped':
            unmapped_tickers[ticker] = industry

        if sig == '买入':
            by_date_sector[(date, sector)]['buy'].append(ticker)
        elif sig in ('出场', '卖出'):
            by_date_sector[(date, sector)]['exit'].append(ticker)

    return by_date_sector, sector_industry_map, unmapped_tickers


def compute_recent_activity(by_date_sector, signals, days=30):
    """Aggregate last N trading days."""
    dates = sorted(set(r['日期'] for r in signals))  # ascending
    if not dates:
        return {}, []

    cutoff = dates[min(days, len(dates)) - 1]
    recent_dates = [d for d in dates if d >= cutoff]  # still ascending
    recent_set = set(recent_dates)

    activity = defaultdict(lambda: {'buy_count': 0, 'exit_count': 0,
                                     'buy_tickers': set(), 'exit_tickers': set()})

    for (date, sector), data in by_date_sector.items():
        if date not in recent_set:
            continue
        activity[sector]['buy_count'] += len(data['buy'])
        activity[sector]['exit_count'] += len(data['exit'])
        activity[sector]['buy_tickers'].update(data['buy'])
        activity[sector]['exit_tickers'].update(data['exit'])

    # Convert sets to lists
    for sector, d in activity.items():
        d['buy_tickers'] = sorted(d['buy_tickers'])
        d['exit_tickers'] = sorted(d['exit_tickers'])

    return dict(activity), recent_dates


def compute_portfolio_sectors(positions, cache):
    """Compute sector allocation of current portfolio (by ticker count and rough market value)."""
    sector_count = defaultdict(int)
    sector_value = defaultdict(float)
    sector_market_cap = defaultdict(float)  # Total market cap of stocks in this sector
    sector_unmapped = []
    sector_unknown = []

    for ticker, pos in positions.items():
        profile = cache.get(ticker, {})
        industry = profile.get('industry', 'Unknown')
        sector = industry_to_gics(industry)

        if sector == 'Unmapped':
            sector_unmapped.append((ticker, industry))
        elif sector == 'Unknown':
            sector_unknown.append(ticker)

        sector_count[sector] += 1
        # Estimated value: highest_price * shares (proxy for max position value)
        shares = pos.get('shares', 0)
        high = pos.get('highest_price', 0)
        sector_value[sector] += shares * high

        mc = profile.get('market_cap', 0)
        if mc and not profile.get('fetch_failed'):
            sector_market_cap[sector] += mc

    return dict(sector_count), dict(sector_value), dict(sector_market_cap), sector_unmapped, sector_unknown


def generate_report(by_date_sector, activity, recent_dates,
                    portfolio_count, portfolio_value, portfolio_mcap,
                    sector_industry_map, unmapped_tickers, sector_unmapped, sector_unknown,
                    cache=None):
    """Generate Markdown report."""
    today = datetime.now().strftime('%Y-%m-%d')
    lines = []
    lines.append(f'# 板块轮动分析报告 — {today}\n')
    lines.append(f'数据源：`signals.csv` (v_eps_v2 策略) + `positions.json`')
    lines.append(f'板块分类：Finnhub `finnhubIndustry` → GICS Level 1 (11 板块)')
    lines.append(f'报告期：最近 {len(recent_dates)} 个交易日')
    lines.append('')

    # === Section 1: 30-day sector activity ===
    lines.append('## 1. 板块活动汇总（最近 30 交易日）\n')
    lines.append('| 板块 | 买入信号数 | 出场信号数 | 净流入 | 买入标的 | 出场标的 |')
    lines.append('|---|---|---|---|---|---|')

    sorted_activity = sorted(activity.items(),
                              key=lambda x: -(x[1]['buy_count'] - x[1]['exit_count']))
    for sector, data in sorted_activity:
        net = data['buy_count'] - data['exit_count']
        buys = ', '.join(data['buy_tickers'][:8]) + ('…' if len(data['buy_tickers']) > 8 else '')
        exits = ', '.join(data['exit_tickers'][:8]) + ('…' if len(data['exit_tickers']) > 8 else '')
        lines.append(f'| {sector} | {data["buy_count"]} | {data["exit_count"]} | {net:+d} | {buys} | {exits} |')
    lines.append('')

    # === Section 2: Daily heatmap ===
    lines.append('## 2. 每日板块净流入热力图\n')
    lines.append('正数=净流入（买入−出场），负数=净流出\n')
    display_dates = recent_dates[-14:]  # Last 14 trading days for readability

    header = '| 板块 |' + '|'.join(d[5:] for d in display_dates) + '| 合计 |'
    sep = '|---|' + '|'.join('---' for _ in display_dates) + '|---|'
    lines.append(header)
    lines.append(sep)

    # Compute sector totals over display window
    sector_totals = {}
    for sector in activity.keys():
        total = 0
        for d in display_dates:
            data = by_date_sector.get((d, sector), {'buy': [], 'exit': []})
            total += len(data['buy']) - len(data['exit'])
        sector_totals[sector] = total

    sorted_sectors = sorted(sector_totals.keys(), key=lambda s: -sector_totals[s])

    for sector in sorted_sectors:
        row = [sector]
        total = 0
        for d in display_dates:
            data = by_date_sector.get((d, sector), {'buy': [], 'exit': []})
            v = len(data['buy']) - len(data['exit'])
            total += v
            if v > 0:
                row.append(f'+{v}')
            elif v < 0:
                row.append(str(v))
            else:
                row.append('·')
        row.append(f'**{total:+d}**')
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # === Section 3: Current portfolio ===
    lines.append('## 3. 当前持仓板块分布\n')
    lines.append('| 板块 | 持仓数 | 占比 | 估算最高市值 | 板块总市值(M) |')
    lines.append('|---|---|---|---|---|')
    total_count = sum(portfolio_count.values())
    total_value = sum(portfolio_value.values())
    sorted_port = sorted(portfolio_count.items(), key=lambda x: -x[1])
    for sector, count in sorted_port:
        pct = count * 100 / total_count if total_count else 0
        val = portfolio_value.get(sector, 0)
        mcap = portfolio_mcap.get(sector, 0)
        lines.append(f'| {sector} | {count} | {pct:.1f}% | ${val:,.0f} | {mcap:,.0f} |')
    lines.append(f'| **合计** | **{total_count}** | **100%** | **${total_value:,.0f}** | — |')
    lines.append('')

    if sector_unmapped:
        lines.append('### ⚠️ 未映射板块的持仓（Finnhub 返回了 INDUSTRY_TO_GICS 未覆盖的 industry）\n')
        lines.append('| Ticker | Industry |')
        lines.append('|---|---|')
        for ticker, ind in sector_unmapped:
            lines.append(f'| {ticker} | `{ind}` |')
        lines.append('')

    if sector_unknown:
        lines.append(f'### ⚠️ Unknown sector 的持仓（Finnhub 未返回 industry，{len(sector_unknown)} 只）\n')
        lines.append(', '.join(sector_unknown))
        lines.append('')

    if unmapped_tickers:
        lines.append(f'### ⚠️ 历史信号中未映射 industry 的 ticker（{len(unmapped_tickers)} 只）\n')
        sample = list(unmapped_tickers.items())[:30]
        lines.append(', '.join(f'{t} ({i})' for t, i in sample))
        if len(unmapped_tickers) > 30:
            lines.append(f'… 还有 {len(unmapped_tickers) - 30} 只')
        lines.append('')

    # === Section 4: Strongest inflow / outflow ===
    lines.append('## 4. 最近 30 日最强流入 / 流出\n')
    lines.append('**最强流入板块：**')
    for sector, data in sorted_activity:
        net = data['buy_count'] - data['exit_count']
        if net <= 0:
            break
        lines.append(f'- **{sector}**: +{net} ({data["buy_count"]} 买入 − {data["exit_count"]} 出场)')
    lines.append('')
    lines.append('**最强流出板块：**')
    # Reverse-sort by net (most negative first), but skip zero/positive
    outflow_sorted = sorted(activity.items(),
                              key=lambda x: x[1]['buy_count'] - x[1]['exit_count'])
    for sector, data in outflow_sorted:
        net = data['buy_count'] - data['exit_count']
        if net >= 0:
            continue
        lines.append(f'- **{sector}**: {net} ({data["buy_count"]} 买入 − {data["exit_count"]} 出场)')
        if data['exit_tickers']:
            lines.append(f'  - 出场标的: {", ".join(data["exit_tickers"][:15])}')
    lines.append('')

    # === Section 5: Coverage stats ===
    if cache is not None:
        cache_size = len([c for c in cache.values() if not c.get('fetch_failed')])
        fail_count = len([c for c in cache.values() if c.get('fetch_failed')])
        empty_count = len([c for c in cache.values() if c.get('empty')])
        lines.append('## 5. 数据覆盖\n')
        lines.append(f'- 缓存 ticker 总数：{len(cache)}')
        lines.append(f'- 成功获取 industry：{cache_size}')
        lines.append(f'- Finnhub 返回空（已退市等）：{empty_count}')
        lines.append(f'- 获取失败：{fail_count}')
        lines.append('')

    return '\n'.join(lines)


def main():
    print(f'[sector] 板块轮动分析 - {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    print(f'[sector] signals: {SIGNALS_FILE}')
    print(f'[sector] positions: {POSITIONS_FILE}')
    print(f'[sector] cache: {CACHE_FILE}')

    # Step 1: Load signals
    signals = load_signals()
    print(f'[sector] 加载 {len(signals)} 条信号')

    unique_tickers = sorted(set(r['股票'] for r in signals))
    print(f'[sector] {len(unique_tickers)} 只唯一 ticker')

    # Step 2: Load cache + fetch missing
    cache = load_cache()
    print(f'[sector] 缓存现有 {len(cache)} 只 ticker')
    cache, failures = fetch_missing_tickers(unique_tickers, cache)
    if failures:
        print(f'[sector] {len(failures)} 只 fetch 失败: {failures[:10]}{"…" if len(failures) > 10 else ""}')

    # Step 3: Compute signal-by-date-sector matrix
    by_date_sector, sector_industry_map, unmapped_tickers = compute_signals_by_date_sector(signals, cache)
    activity, recent_dates = compute_recent_activity(by_date_sector, signals, days=30)

    # Step 4: Portfolio sector breakdown
    positions = load_positions()
    print(f'[sector] 当前持仓 {len(positions)} 个')
    portfolio_count, portfolio_value, portfolio_mcap, sector_unmapped, sector_unknown = \
        compute_portfolio_sectors(positions, cache)

    # Step 5: Generate report
    report = generate_report(by_date_sector, activity, recent_dates,
                             portfolio_count, portfolio_value, portfolio_mcap,
                             sector_industry_map, unmapped_tickers, sector_unmapped, sector_unknown,
                             cache=cache)

    today = datetime.now().strftime('%Y-%m-%d')
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f'rotation_{today}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f'[sector] 报告已保存：{report_path}')

    # Print summary to console
    print()
    print('=' * 70)
    print(f'板块轮动 - 最近 30 日汇总 (按净流入降序)')
    print('=' * 70)
    print(f"{'板块':<32} {'买入':>5} {'出场':>5} {'净流入':>7}")
    print('-' * 70)
    for sector, data in sorted(activity.items(),
                                key=lambda x: -(x[1]['buy_count'] - x[1]['exit_count'])):
        net = data['buy_count'] - data['exit_count']
        print(f'{sector:<32} {data["buy_count"]:>5} {data["exit_count"]:>5} {net:>+7}')
    print()
    print(f'当前持仓板块分布 (按持仓数降序)：')
    total_count = sum(portfolio_count.values())
    total_value = sum(portfolio_value.values())
    print(f"{'板块':<32} {'持仓数':>5} {'占比':>6} {'估算市值':>14}")
    print('-' * 70)
    for sector, count in sorted(portfolio_count.items(), key=lambda x: -x[1]):
        pct = count * 100 / total_count if total_count else 0
        val = portfolio_value.get(sector, 0)
        print(f'{sector:<32} {count:>5} {pct:>5.1f}% ${val:>13,.0f}')
    print('-' * 70)
    print(f'{"合计":<32} {total_count:>5} {"100.0%":>6} ${total_value:>13,.0f}')

    if sector_unmapped:
        print()
        print(f'⚠️ {len(sector_unmapped)} 只持仓 industry 未映射：')
        for t, i in sector_unmapped:
            print(f'  {t}: `{i}`')


if __name__ == '__main__':
    main()