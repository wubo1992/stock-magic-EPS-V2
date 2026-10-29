#!/usr/bin/env python3
"""
Backtest 8% stop loss for 2026 Weinstein EPS signals.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from data.fetcher import fetch
import sys

# Load signals
signals_df = pd.read_csv('output/v_eps_v2/signals.csv')
buy_signals = signals_df[signals_df['信号'] == '买入'].copy()
buy_signals['日期'] = pd.to_datetime(buy_signals['日期'])
buy_signals = buy_signals.sort_values('日期')

# Filter 2026 signals only
buy_signals = buy_signals[(buy_signals['日期'] >= '2026-01-01') & (buy_signals['日期'] <= '2026-05-18')]

# Remove duplicate entries on same date for same stock (keep first)
buy_signals = buy_signals.drop_duplicates(subset=['日期', '股票'], keep='first')

print(f"Total 2026 buy signals (unique date+stock): {len(buy_signals)}", flush=True)

# Backtest parameters
STOP_LOSS_PCT = 0.08  # 8% stop loss
CAPITAL_PER_TRADE = 1000

results = []
stop_loss_triggered = 0
hold_to_week = 0
errors = 0

for idx, row in buy_signals.iterrows():
    symbol = row['股票']
    entry_date = row['日期']
    entry_price_str = row['参考止损'].replace('$', '').replace(',', '')

    try:
        entry_price = float(entry_price_str)
    except:
        continue

    try:
        # Get data after entry date - use timezone-aware end_date
        end_date = entry_date + pd.Timedelta(days=30)
        end_date_tz = end_date.replace(tzinfo=timezone.utc)

        data = fetch([symbol], history_days=60, live_mode=False, end_date=end_date_tz)

        if symbol not in data or len(data[symbol]) < 2:
            errors += 1
            continue

        df = data[symbol]
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert(None)  # Remove timezone for comparison
        df = df.sort_index()

        # Find entry bar (next day open)
        entry_mask = df.index > entry_date
        if not entry_mask.any():
            errors += 1
            continue

        entry_bar = df[entry_mask].iloc[0]
        entry_open = entry_bar['open']
        shares = int(CAPITAL_PER_TRADE / entry_open)

        # Calculate 8% stop price
        stop_price = entry_open * (1 - STOP_LOSS_PCT)

        # Simulate holding for up to 20 days or until stop loss
        sold = False
        exit_price = None
        exit_date = None
        holding_days = 0

        for i, (date, bar) in enumerate(df[entry_mask].iterrows()):
            if i == 0:  # Skip entry bar
                continue
            if i > 20:  # Max 20 days
                break

            holding_days = i
            low = bar['low']

            # Check stop loss
            if low <= stop_price:
                exit_price = stop_price
                exit_date = date
                sold = True
                stop_loss_triggered += 1
                break

            # End of week (5 days) - take profit
            if i >= 5:
                exit_price = bar['close']
                exit_date = date
                hold_to_week += 1
                break

        # If not sold by day 20, sell at close
        if not sold:
            last_bar = df[entry_mask].iloc[-1]
            exit_price = last_bar['close']
            exit_date = last_bar.name
            hold_to_week += 1
            holding_days = len(df[entry_mask]) - 1

        pnl = (exit_price - entry_open) * shares

        results.append({
            'symbol': symbol,
            'entry_date': entry_date.strftime('%Y-%m-%d'),
            'exit_date': exit_date.strftime('%Y-%m-%d') if exit_date else 'N/A',
            'entry_price': entry_open,
            'exit_price': exit_price,
            'shares': shares,
            'pnl': pnl,
            'pnl_pct': (exit_price / entry_open - 1) * 100,
            'stop_loss': 'Yes' if sold else 'No',
            'holding_days': holding_days
        })

        if idx % 20 == 0:
            print(f"Processed {idx} signals...", flush=True)

    except Exception as e:
        errors += 1
        print(f"Error processing {symbol}: {e}", flush=True)
        continue

results_df = pd.DataFrame(results)

print(f"\n=== 8% Stop Loss Backtest Results ===", flush=True)
print(f"Total trades: {len(results_df)}", flush=True)
print(f"Stop loss triggered: {stop_loss_triggered} ({stop_loss_triggered/len(results_df)*100:.1f}%)", flush=True)
print(f"Hold to week: {hold_to_week} ({hold_to_week/len(results_df)*100:.1f}%)", flush=True)
print(f"Errors: {errors}", flush=True)
print(f"\nTotal P&L: ${results_df['pnl'].sum():.2f}", flush=True)

sl_df = results_df[results_df['stop_loss'] == 'Yes']
hold_df = results_df[results_df['stop_loss'] == 'No']

print(f"\n--- Stop Loss Trades ({len(sl_df)}) ---", flush=True)
print(f"P&L: ${sl_df['pnl'].sum():.2f}", flush=True)

print(f"\n--- Hold to Week Trades ({len(hold_df)}) ---", flush=True)
print(f"P&L: ${hold_df['pnl'].sum():.2f}", flush=True)

results_df['month'] = pd.to_datetime(results_df['entry_date']).dt.to_period('M')
monthly = results_df.groupby('month')['pnl'].sum()
print(f"\n--- Monthly P&L ---", flush=True)
for month, pnl in monthly.items():
    print(f"{month}: ${pnl:.2f}", flush=True)

wins = results_df[results_df['pnl'] > 0]
print(f"\nWin rate: {len(wins)/len(results_df)*100:.1f}%", flush=True)
print(f"Total wins: {len(wins)}, Total losses: {len(results_df) - len(wins)}", flush=True)