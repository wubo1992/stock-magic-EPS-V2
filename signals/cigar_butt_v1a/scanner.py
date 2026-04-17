"""
signals/cigar_butt_v1a/scanner.py — V1A 全市场扫描器

职责：扫描全市场股票，筛选出P/NCAV < 0.67的深度价值标的
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from data.balance_sheet import get_full_financials
from data.fetcher import fetch

from .filters import (
    CigarButtV1ACriteria,
    StockFundamentalsV1A,
    calculate_ncav,
    calculate_p_ncav,
    passes_filters,
    rank_by_p_ncav,
)


class CigarButtV1AScanner:
    """
    V1A 扫描器：全市场扫描低P/NCAV股票

    使用方式：
        scanner = CigarButtV1AScanner(config)
        candidates = scanner.scan(date)
    """

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
    ) -> None:
        self.cfg = strategy_config
        self.market_data = market_data

        # 解析配置
        self.max_p_ncav = self.cfg.get("max_p_ncav", 0.67)
        self.min_market_cap = self.cfg.get("min_market_cap", 50_000_000)
        self.max_market_cap = self.cfg.get("max_market_cap", 1_000_000_000)
        self.min_avg_volume = self.cfg.get("min_avg_volume_20d", 100_000)
        self.max_debt_ratio = self.cfg.get("max_debt_ratio", 0.80)
        self.max_positions = self.cfg.get("max_positions", 20)

        self.criteria = CigarButtV1ACriteria(
            max_p_ncav=self.max_p_ncav,
            min_market_cap=self.min_market_cap,
            max_market_cap=self.max_market_cap,
            min_avg_volume=self.min_avg_volume,
            max_debt_ratio=self.max_debt_ratio,
        )

    def scan(
        self,
        date: datetime,
        universe: Optional[list[str]] = None,
    ) -> list[tuple[str, float, float]]:
        """
        扫描全市场，返回符合条件的股票

        参数：
            date: 当前日期
            universe: 股票列表，默认使用market_data中的所有股票

        返回：
            [(symbol, p_ncav, ncav), ...] 按P/NCAV升序排列
        """
        if universe is None:
            universe = list(self.market_data.keys())

        print(f"[V1A扫描] 开始扫描 {len(universe)} 只股票...")

        candidates = []
        skipped = 0

        for symbol in universe:
            # 跳过指数
            if symbol.startswith("^"):
                continue

            # 获取市场数据
            df = self.market_data.get(symbol)
            if df is None or len(df) < 20:
                skipped += 1
                continue

            # 获取最新价格
            current_row = df.iloc[-1]
            current_price = float(current_row["close"])
            volume_20d = int(df["volume"].tail(20).mean())

            # 获取财务数据
            financials = self._get_financials(symbol)
            if financials is None:
                skipped += 1
                continue

            # 构建股票数据
            bs = financials.get("balance_sheet", {})
            if bs.get('error'):
                skipped += 1
                continue

            market_cap = financials.get("market_cap", 0)
            if market_cap <= 0:
                # 如果没有市值数据，尝试用股价 * 流通股估算
                try:
                    shares_outstanding = bs.get("shares_outstanding", 0)
                    if shares_outstanding <= 0:
                        shares_outstanding = bs.get("commonStockSharesOutstanding", 0)
                    if shares_outstanding > 0:
                        market_cap = shares_outstanding * current_price
                except:
                    pass

            if market_cap <= 0:
                skipped += 1
                continue

            stock = StockFundamentalsV1A(
                symbol=symbol,
                market_cap=market_cap,
                share_price=current_price,
                cash=bs.get("cash", 0),
                accounts_receivable=bs.get("accounts_receivable", 0),
                inventory=bs.get("inventory", 0),
                current_liabilities=bs.get("current_liabilities", 0),
                total_liabilities=bs.get("total_liabilities", 0),
                total_assets=bs.get("total_assets", 0),
                avg_volume_20d=volume_20d,
                is_liquidating=bs.get("is_liquidating", False),
                has_audit_issues=bs.get("has_audit_issues", False),
            )

            # 通过过滤器
            passed, reason = passes_filters(stock, self.criteria)
            if passed:
                candidates.append(stock)
                ncav = calculate_ncav(stock)
                p_ncav = calculate_p_ncav(stock, ncav)
                print(f"  [V1A] {symbol}: P/NCAV={p_ncav:.2f}, NCAV=${ncav:,.0f}, 市值=${market_cap/1e6:.0f}M")
            else:
                skipped += 1

            # 防限流
            time.sleep(0.1)

        print(f"[V1A扫描] 完成: {len(candidates)} 只通过, {skipped} 只跳过")

        # 按P/NCAV排序
        ranked = rank_by_p_ncav(candidates)

        # 取前N只
        selected = ranked[: self.max_positions]
        print(f"[V1A扫描] 选取前 {len(selected)} 只")

        return selected

    def _get_financials(self, symbol: str) -> Optional[dict]:
        """
        获取股票财务数据（带缓存）

        返回：
            {
                'balance_sheet': dict,
                'cash_flow': dict,
                'income': dict,
                'ncav': float,
                'market_cap': float,
                'error': str/None
            }
        """
        try:
            # 获取资产负债表
            bs_result = get_full_financials(symbol)
            if bs_result.get('error'):
                return None

            # 从市场数据获取市值（更准确）
            df = self.market_data.get(symbol)
            if df is not None and len(df) > 0:
                current_price = float(df.iloc[-1]["close"])
                # market_cap 在 bs_result 外层，不在 balance_sheet 内
                # 如果 balance_sheet 中没有市值，尝试用股价 * 流通股估算
                market_cap = bs_result.get('market_cap', 0)
                if market_cap <= 0:
                    bs = bs_result.get('balance_sheet', {})
                    shares_outstanding = bs.get("shares_outstanding", 0)
                    if shares_outstanding <= 0:
                        shares_outstanding = bs.get("commonStockSharesOutstanding", 0)
                    if shares_outstanding > 0:
                        market_cap = shares_outstanding * current_price
                bs_result['market_cap'] = market_cap

            return bs_result

        except Exception as e:
            print(f"[V1A扫描] {symbol} 获取财务数据失败: {e}")
            return None

    def get_candidates_for_date(
        self,
        date: datetime,
        universe: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        获取候选股票列表（包含完整信息，用于策略选择）

        返回：
            [{
                'symbol': str,
                'p_ncav': float,
                'ncav': float,
                'market_cap': float,
                'current_price': float,
            }, ...]
        """
        candidates = self.scan(date, universe)

        result = []
        for symbol, p_ncav, ncav in candidates:
            df = self.market_data.get(symbol)
            if df is not None:
                current_price = float(df.iloc[-1]["close"])
                result.append({
                    'symbol': symbol,
                    'p_ncav': p_ncav,
                    'ncav': ncav,
                    'market_cap': df.iloc[-1].get('market_cap', 0) if hasattr(df.iloc[-1], 'get') else 0,
                    'current_price': current_price,
                })

        return result
