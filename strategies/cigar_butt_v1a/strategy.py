"""
strategies/cigar_butt_v1a/strategy.py — 捡烟蒂V1A策略

纯格雷厄姆NCAV深度价值策略：
- 每月扫描全市场，筛选P/NCAV < 0.67的标的
- 等权持有15-20只
- 持有期6-18个月
- P/NCAV > 1.0 或持有超限则卖出

策略ID：cigar_butt_v1a
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from events import EventQueue, SignalEvent
from strategies.base import StrategyBase


@dataclass
class CigarButtPosition:
    """V1A 持仓记录"""
    symbol: str
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int = 0
    shares: float = 0.0
    ncav: float = 0.0  # 入场时的NCAV
    p_ncav: float = 0.0  # 入场时的P/NCAV


class CigarButtV1AStrategy(StrategyBase):
    """
    捡烟蒂V1A策略：纯格雷厄姆NCAV深度价值

    核心逻辑：
    - 每月扫描全市场，筛选P/NCAV < 0.67
    - 等权持有15-20只
    - 持有期6-18个月
    - P/NCAV > 1.0 或持有超限则卖出
    """

    strategy_id = "cigar_butt_v1a"
    strategy_name = "捡烟蒂V1A-纯NCAV"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)

        # 配置参数
        self.max_p_ncav = self.cfg.get("max_p_ncav", 0.67)
        self.min_market_cap = self.cfg.get("min_market_cap", 50_000_000)
        self.max_market_cap = self.cfg.get("max_market_cap", 1_000_000_000)
        self.min_avg_volume = self.cfg.get("min_avg_volume_20d", 100_000)
        self.max_debt_ratio = self.cfg.get("max_debt_ratio", 0.80)

        self.min_positions = self.cfg.get("min_positions", 15)
        self.max_positions = self.cfg.get("max_positions", 20)
        self.max_single_position_pct = self.cfg.get("max_single_position_pct", 0.10)

        self.sell_p_ncav_threshold = self.cfg.get("sell_p_ncav_threshold", 1.0)
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.25)
        self.max_hold_days = self.cfg.get("force_sell_days", 540)

        # 持仓
        self.positions: dict[str, CigarButtPosition] = {}

        # 上次rebalance月份
        self._last_rebalance_month: Optional[int] = None

        # 扫描器（延迟初始化）
        self._scanner = None

    def _get_scanner(self):
        """延迟加载扫描器"""
        if self._scanner is None:
            from signals.cigar_butt_v1a import CigarButtV1AScanner
            self._scanner = CigarButtV1AScanner(self.cfg, self.market_data)
        return self._scanner

    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]:
        """
        每日运行：
        1. 检查持仓退出
        2. 月末rebalance
        """
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        signals = []

        # 1. 检查持仓退出
        exit_signals = self._check_exits(date)
        for sig in exit_signals:
            queue.put(sig)
            signals.append(sig)

        # 2. 月末rebalance
        current_month = date.month
        if self._last_rebalance_month != current_month:
            self._last_rebalance_month = current_month
            rebalance_signals = self._do_rebalance(date)
            for sig in rebalance_signals:
                queue.put(sig)
                signals.append(sig)

        return signals

    def _check_exits(self, date: datetime) -> list[SignalEvent]:
        """检查所有持仓的退出条件"""
        signals = []
        to_close = []

        for symbol, pos in self.positions.items():
            df = self.market_data.get(symbol)
            if df is None or len(df) < 1:
                continue

            current_row = df.iloc[-1]
            current_price = float(current_row["close"])
            pos.days_held += 1

            if current_price > pos.highest_price:
                pos.highest_price = current_price

            # 获取最新财务数据
            financials = self._get_financials(symbol)
            ncav = financials.get('ncav', 0) if financials else 0
            market_cap = financials.get('market_cap', 0) if financials else 0

            should_exit, reason = self._check_exit_single(
                pos, current_price, ncav, market_cap, date
            )

            if should_exit:
                sig = SignalEvent.create(
                    symbol=symbol,
                    timestamp=date,
                    direction="sell",
                    strength=3,
                    reason=reason,
                    shares=pos.shares,
                )
                signals.append(sig)
                to_close.append(symbol)

        # 清理已关闭的持仓
        for symbol in to_close:
            if symbol in self.positions:
                del self.positions[symbol]

        return signals

    def _check_exit_single(
        self,
        pos: CigarButtPosition,
        current_price: float,
        current_ncav: float,
        current_market_cap: float,
        date: datetime,
    ) -> tuple[bool, str]:
        """检查单只持仓的退出条件"""
        days_held = (date - pos.entry_date).days

        # [1] P/NCAV > 阈值（价值已回归）
        if current_ncav > 0:
            p_ncav = current_market_cap / current_ncav
            if p_ncav > self.sell_p_ncav_threshold:
                return True, f"P/NCAV={p_ncav:.2f} > {self.sell_p_ncav_threshold}，价值已回归"

        # [2] 持有期超限
        if days_held >= self.max_hold_days:
            return True, f"持有{days_held}天超过{self.max_hold_days}天，强制卖出"

        # [3] 止损检查
        drawdown = (current_price / pos.entry_price - 1)
        if drawdown <= -self.stop_loss_pct:
            return True, f"跌幅{drawdown*100:.1f}%触发止损"

        return False, ""

    def _do_rebalance(self, date: datetime) -> list[SignalEvent]:
        """月末重新扫描并调仓"""
        print(f"\n[V1A Rebalance] {date.strftime('%Y-%m-%d')} 开始调仓...")

        signals = []

        # 获取候选股
        scanner = self._get_scanner()
        candidates = scanner.scan(date)

        # 计算当前持仓
        current_holdings = set(self.positions.keys())
        new_selections = set(symbol for symbol, _, _ in candidates)

        # 卖出不在新名单中的
        to_sell = current_holdings - new_selections
        for symbol in to_sell:
            pos = self.positions[symbol]
            sig = SignalEvent.create(
                symbol=symbol,
                timestamp=date,
                direction="sell",
                strength=2,
                reason="rebalance: 不在新候选名单",
                shares=pos.shares,
            )
            signals.append(sig)
            del self.positions[symbol]

        # 买入需要新增的
        to_buy = new_selections - current_holdings
        if not to_buy:
            print(f"[V1A Rebalance] 无需买入，继续持有现有仓位")
            return signals

        # 计算可用资金（简化：假设每只仓位相等）
        num_to_buy = len(to_buy)
        available_capital = self._calculate_total_capital()
        position_size = available_capital / num_to_buy if num_to_buy > 0 else 0
        position_size = min(position_size, available_capital * self.max_single_position_pct)

        for symbol, p_ncav, ncav in candidates:
            if symbol not in to_buy:
                continue

            df = self.market_data.get(symbol)
            if df is None:
                continue

            current_price = float(df.iloc[-1]["close"])
            shares = int(position_size / current_price)

            if shares <= 0:
                continue

            sig = SignalEvent.create(
                symbol=symbol,
                timestamp=date,
                direction="buy",
                strength=2,
                reason=f"NCAV选股: P/NCAV={p_ncav:.2f}",
                shares=shares,
            )
            signals.append(sig)

            # 记录持仓
            self.positions[symbol] = CigarButtPosition(
                symbol=symbol,
                entry_price=current_price,
                entry_date=date,
                highest_price=current_price,
                shares=shares,
                ncav=ncav,
                p_ncav=p_ncav,
            )

        print(f"[V1A Rebalance] 买入{len(to_buy)}只，卖出{len(to_sell)}只")
        return signals

    def _get_financials(self, symbol: str) -> Optional[dict]:
        """获取股票财务数据"""
        try:
            from data.balance_sheet import get_full_financials
            financials = get_full_financials(symbol)
            if financials.get('error'):
                return None

            # 计算市值（从market_data估算）
            df = self.market_data.get(symbol)
            if df is not None and len(df) > 0:
                current_price = float(df.iloc[-1]["close"])
                # 估算市值
                bs = financials.get('balance_sheet', {})
                market_cap = bs.get('market_cap', 0)
                if market_cap <= 0:
                    market_cap = current_price * 1_000_000  # 简化估算
                financials['market_cap'] = market_cap

            return financials
        except Exception as e:
            print(f"[V1A] {symbol} 获取财务数据失败: {e}")
            return None

    def _calculate_total_capital(self) -> float:
        """计算总资产（简化版）"""
        # 简化：假设初始100万，每次rebalance平均分配
        # 实际应该追踪实际资金变化
        return 1_000_000
