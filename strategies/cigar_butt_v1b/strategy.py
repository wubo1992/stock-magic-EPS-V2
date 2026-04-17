"""
strategies/cigar_butt_v1b/strategy.py — 捡烟蒂V1B策略

深度价值 + 盈利过滤策略：
- 三层筛选：深度价值 + 盈利质量 + 流动性
- 综合评分选股
- 等权持有15-25只
- 持有期12-24个月
- 盈利质量恶化立即检查

策略ID：cigar_butt_v1b
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from events import EventQueue, SignalEvent
from strategies.base import StrategyBase


@dataclass
class CigarButtV1BPosition:
    """V1B 持仓记录（扩展版，包含盈利质量追踪）"""
    symbol: str
    entry_price: float
    entry_date: datetime
    highest_price: float
    days_held: int = 0
    shares: float = 0.0

    # V1B特有字段
    entry_ncav: float = 0.0
    entry_p_ncav: float = 0.0
    entry_pe: float = 0.0
    entry_debt_ratio: float = 0.0
    entry_cfo: float = 0.0

    # 盈利质量追踪
    consecutive_negative_cfo_quarters: int = 0


class CigarButtV1BStrategy(StrategyBase):
    """
    捡烟蒂V1B策略：深度价值 + 盈利质量过滤

    核心逻辑：
    - 三层筛选：深度价值 + 盈利质量 + 流动性
    - 综合评分选股
    - 等权持有15-25只
    - 持有期12-24个月
    - 盈利质量恶化立即检查
    """

    strategy_id = "cigar_butt_v1b"
    strategy_name = "捡烟蒂V1B-深度价值+盈利过滤"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
        live_mode: bool = False,
    ) -> None:
        super().__init__(strategy_config, market_data)

        # 配置参数
        # 第一层：深度价值
        self.max_p_ncav = self.cfg.get("max_p_ncav", 0.75)
        self.max_p_netnet = self.cfg.get("max_p_netnet", 0.85)
        self.max_p_b = self.cfg.get("max_p_b", 1.2)
        self.max_p_e = self.cfg.get("max_p_e", 15)
        self.max_debt_ratio = self.cfg.get("max_debt_ratio", 0.50)

        # 第二层：盈利质量
        self.require_positive_eps = self.cfg.get("require_positive_eps", True)
        self.require_positive_cfo = self.cfg.get("require_positive_cfo", True)
        self.max_revenue_decline_1y = self.cfg.get("max_revenue_decline_1y", 0.20)

        # 第三层：流动性
        self.min_market_cap = self.cfg.get("min_market_cap", 100_000_000)
        self.max_market_cap = self.cfg.get("max_market_cap", 5_000_000_000)
        self.min_avg_dollar_volume = self.cfg.get("min_avg_dollar_volume_20d", 1_000_000)

        # 仓位管理
        self.min_positions = self.cfg.get("min_positions", 15)
        self.max_positions = self.cfg.get("max_positions", 25)
        self.max_single_position_pct = self.cfg.get("max_single_position_pct", 0.08)

        # 退出规则
        self.sell_p_ncav_threshold = self.cfg.get("sell_p_ncav_threshold", 1.0)
        self.sell_pe_threshold = self.cfg.get("sell_pe_threshold", 25)
        self.stop_loss_pct = self.cfg.get("stop_loss_pct", 0.20)
        self.time_stop_days = self.cfg.get("time_stop_days", 720)
        self.min_gain_for_time_stop = self.cfg.get("min_gain_for_time_stop", 0.05)

        # 评分权重
        self.score_weights = {
            'p_ncav': self.cfg.get("score_p_ncav_weight", 0.30),
            'p_e': self.cfg.get("score_pe_weight", 0.20),
            'debt': self.cfg.get("score_debt_weight", 0.20),
            'cfo': self.cfg.get("score_cfo_weight", 0.15),
            'size': self.cfg.get("score_size_weight", 0.15),
        }

        # 持仓
        self.positions: dict[str, CigarButtV1BPosition] = {}
        self._last_rebalance_month: Optional[int] = None
        self._scanner = None

    def _get_scanner(self):
        """延迟加载扫描器"""
        if self._scanner is None:
            from signals.cigar_butt_v1b import CigarButtV1BSCanner
            self._scanner = CigarButtV1BSCanner(self.cfg, self.market_data)
        return self._scanner

    def run_date(self, date: datetime, queue: EventQueue) -> list[SignalEvent]:
        """每日运行"""
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
            current_metrics = self._extract_metrics(financials, current_price) if financials else None

            should_exit, reason = self._check_exit_single(
                pos, current_price, current_metrics, date
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

        for symbol in to_close:
            if symbol in self.positions:
                del self.positions[symbol]

        return signals

    def _check_exit_single(
        self,
        pos: CigarButtV1BPosition,
        current_price: float,
        current_metrics: Optional[dict],
        date: datetime,
    ) -> tuple[bool, str]:
        """检查V1B单只持仓的退出条件"""
        days_held = (date - pos.entry_date).days

        # [1] P/NCAV > 阈值（价值已回归）
        if current_metrics:
            p_ncav = current_metrics.get('p_ncav', 0)
            if p_ncav > 0 and p_ncav > self.sell_p_ncav_threshold:
                return True, f"P/NCAV={p_ncav:.2f} > {self.sell_p_ncav_threshold}，价值已回归"

        # [2] 净利润转负
        if current_metrics:
            net_income = current_metrics.get('net_income_ttm', 0)
            if net_income < 0:
                return True, f"净利润转负=${net_income:.0f}，基本面恶化"

        # [3] 经营现金流连续为负
        if current_metrics:
            cfo = current_metrics.get('operating_cash_flow_ttm', 0)
            if cfo < 0:
                pos.consecutive_negative_cfo_quarters += 1
                if pos.consecutive_negative_cfo_quarters >= 2:
                    return True, f"经营现金流连续为负=${cfo:.0f}"
            else:
                pos.consecutive_negative_cfo_quarters = 0

        # [4] 止损检查
        drawdown = (current_price / pos.entry_price - 1)
        if drawdown <= -self.stop_loss_pct:
            return True, f"跌幅{drawdown*100:.1f}%触发止损"

        # [5] P/E > 阈值
        if current_metrics:
            pe = current_metrics.get('pe_ratio', 0)
            if pe > self.sell_pe_threshold:
                return True, f"P/E={pe:.1f} > {self.sell_pe_threshold}，高估"

        # [6] 时间止损
        if days_held >= self.time_stop_days:
            current_gain = (current_price / pos.entry_price - 1)
            if current_gain < self.min_gain_for_time_stop:
                return True, f"时间止损：持有{days_held}天，盈利仅{current_gain*100:.1f}%"

        return False, ""

    def _do_rebalance(self, date: datetime) -> list[SignalEvent]:
        """月末重新扫描并调仓"""
        print(f"\n[V1B Rebalance] {date.strftime('%Y-%m-%d')} 开始调仓...")

        signals = []

        # 获取候选股
        scanner = self._get_scanner()
        candidates = scanner.scan(date)

        # 计算当前持仓
        current_holdings = set(self.positions.keys())
        new_selections = set(symbol for symbol, _ in candidates)

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
            print(f"[V1B Rebalance] 无需买入，继续持有现有仓位")
            return signals

        # 计算可用资金
        num_to_buy = len(to_buy)
        available_capital = self._calculate_total_capital()
        position_size = available_capital / num_to_buy if num_to_buy > 0 else 0
        position_size = min(position_size, available_capital * self.max_single_position_pct)

        for symbol, score in candidates:
            if symbol not in to_buy:
                continue

            df = self.market_data.get(symbol)
            if df is None:
                continue

            current_price = float(df.iloc[-1]["close"])
            shares = int(position_size / current_price)

            if shares <= 0:
                continue

            # 获取财务数据记录
            financials = self._get_financials(symbol)

            sig = SignalEvent.create(
                symbol=symbol,
                timestamp=date,
                direction="buy",
                strength=2,
                reason=f"V1B综合评分选股: score={score:.1f}",
                shares=shares,
            )
            signals.append(sig)

            # 记录持仓
            metrics = self._extract_metrics(financials, current_price) if financials else {}
            self.positions[symbol] = CigarButtV1BPosition(
                symbol=symbol,
                entry_price=current_price,
                entry_date=date,
                highest_price=current_price,
                shares=shares,
                entry_ncav=metrics.get('ncav', 0),
                entry_p_ncav=metrics.get('p_ncav', 0),
                entry_pe=metrics.get('pe_ratio', 0),
                entry_debt_ratio=metrics.get('debt_ratio', 0),
                entry_cfo=metrics.get('operating_cash_flow_ttm', 0),
            )

        print(f"[V1B Rebalance] 买入{len(to_buy)}只，卖出{len(to_sell)}只")
        return signals

    def _get_financials(self, symbol: str) -> Optional[dict]:
        """获取股票完整财务数据"""
        try:
            from data.balance_sheet import get_full_financials, get_financials_ttm

            financials = get_full_financials(symbol)
            if financials.get('error'):
                return None

            # 获取TTM数据
            ttm = get_financials_ttm(symbol)
            if not ttm.get('error'):
                financials.update({
                    'net_income_ttm': ttm.get('net_income_ttm', 0),
                    'operating_cash_flow_ttm': ttm.get('operating_cash_flow_ttm', 0),
                    'revenue_ttm': ttm.get('revenue_ttm', 0),
                })

            # 计算市值
            df = self.market_data.get(symbol)
            if df is not None and len(df) > 0:
                current_price = float(df.iloc[-1]["close"])
                bs = financials.get('balance_sheet', {})
                market_cap = bs.get('market_cap', 0)
                if market_cap <= 0:
                    market_cap = current_price * 1_000_000  # 简化估算
                financials['market_cap'] = market_cap

                # 计算PE/PB
                inc = financials.get('income', {})
                net_income = financials.get('net_income_ttm', inc.get('net_income', 0))
                if net_income > 0:
                    financials['pe_ratio'] = market_cap / net_income
                if bs.get('total_assets', 0) > 0:
                    financials['pb_ratio'] = market_cap / bs.get('total_assets', 0)

            return financials

        except Exception as e:
            print(f"[V1B] {symbol} 获取财务数据失败: {e}")
            return None

    def _extract_metrics(self, financials: Optional[dict], current_price: float) -> dict:
        """从财务数据提取关键指标"""
        if not financials:
            return {}

        bs = financials.get('balance_sheet', {})
        cf = financials.get('cash_flow', {})

        # 计算NCAV
        ncav = (
            bs.get('cash', 0) +
            0.75 * bs.get('accounts_receivable', 0) +
            0.5 * bs.get('inventory', 0) -
            bs.get('current_liabilities', 0)
        )

        market_cap = financials.get('market_cap', 0)
        p_ncav = market_cap / ncav if ncav > 0 else float('inf')

        # 负债率
        total_assets = bs.get('total_assets', 0)
        debt_ratio = bs.get('total_liabilities', 0) / total_assets if total_assets > 0 else 0

        return {
            'ncav': ncav,
            'p_ncav': p_ncav,
            'pe_ratio': financials.get('pe_ratio', 0),
            'pb_ratio': financials.get('pb_ratio', 0),
            'debt_ratio': debt_ratio,
            'net_income_ttm': financials.get('net_income_ttm', 0),
            'operating_cash_flow_ttm': financials.get('operating_cash_flow_ttm', 0),
            'market_cap': market_cap,
        }

    def _calculate_total_capital(self) -> float:
        """计算总资产（简化版）"""
        return 1_000_000
