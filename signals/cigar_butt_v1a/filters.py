"""
signals/cigar_butt_v1a/filters.py — V1A 过滤器

纯格雷厄姆NCAV深度价值过滤：只关注P/NCAV，不考虑盈利质量
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CigarButtV1ACriteria:
    """V1A 过滤器标准"""
    max_p_ncav: float = 0.67
    min_market_cap: float = 50_000_000  # $5000万
    max_market_cap: float = 1_000_000_000  # $10亿
    min_avg_volume: int = 100_000  # 20日均成交 > 10万股
    max_debt_ratio: float = 0.80  # 负债/资产 < 80%


@dataclass
class StockFundamentalsV1A:
    """V1A 股票基本面数据"""
    symbol: str
    market_cap: float  # 市值
    share_price: float  # 当前股价
    cash: float  # 现金
    accounts_receivable: float  # 应收账款
    inventory: float  # 存货
    current_liabilities: float  # 流动负债
    total_liabilities: float  # 总负债
    total_assets: float  # 总资产
    avg_volume_20d: int  # 20日均成交股数
    is_liquidating: bool = False  # 是否正在清算
    has_audit_issues: bool = False  # 审计问题


def calculate_ncav(stock: StockFundamentalsV1A) -> float:
    """
    计算 NCAV（Net Current Asset Value）

    公式：NCAV = 现金 + 0.75×应收账款 + 0.5×存货 - 全部流动负债
    """
    ncav = (
        stock.cash +
        0.75 * stock.accounts_receivable +
        0.5 * stock.inventory -
        stock.current_liabilities
    )
    return ncav


def calculate_p_ncav(stock: StockFundamentalsV1A, ncav: float) -> float:
    """
    计算 P/NCAV 比率

    返回：
        P/NCAV 比率，越低越便宜
        如果 NCAV <= 0，返回 infinity
    """
    if ncav <= 0:
        return float('inf')
    return stock.market_cap / ncav


def passes_filters(
    stock: StockFundamentalsV1A,
    criteria: CigarButtV1ACriteria,
) -> tuple[bool, Optional[str]]:
    """
    检查股票是否通过V1A所有过滤器

    返回：
        (通过, 失败原因)
    """
    # 计算 NCAV
    ncav = calculate_ncav(stock)

    # 数据质量检查：必须有流动负债数据
    if stock.current_liabilities <= 0:
        return False, f"流动负债=${stock.current_liabilities:.0f} <= 0，数据不可靠"

    if ncav <= 0:
        return False, f"NCAV=${ncav:.0f} <= 0"

    # 计算 P/NCAV
    p_ncav = calculate_p_ncav(stock, ncav)
    if p_ncav >= criteria.max_p_ncav:
        return False, f"P/NCAV={p_ncav:.2f} >= {criteria.max_p_ncav}"

    # 市值过滤
    if not (criteria.min_market_cap <= stock.market_cap <= criteria.max_market_cap):
        mcap_m = stock.market_cap / 1_000_000
        min_m = criteria.min_market_cap / 1_000_000
        max_m = criteria.max_market_cap / 1_000_000
        return False, f"市值${mcap_m:.0f}M不在${min_m:.0f}M-${max_m:.0f}M范围"

    # 流动性过滤
    if stock.avg_volume_20d < criteria.min_avg_volume:
        return False, f"成交量{stock.avg_volume_20d:,} < {criteria.min_avg_volume:,}"

    # 负债率过滤
    if stock.total_assets > 0:
        debt_ratio = stock.total_liabilities / stock.total_assets
        if debt_ratio >= criteria.max_debt_ratio:
            return False, f"负债率={debt_ratio:.1%} >= {criteria.max_debt_ratio:.1%}"

    # 排除清单
    if stock.is_liquidating:
        return False, "正在清算中"

    if stock.has_audit_issues:
        return False, "审计问题"

    return True, None


def rank_by_p_ncav(
    candidates: list[StockFundamentalsV1A],
) -> list[tuple[str, float, float]]:
    """
    按P/NCAV从低到高排序（越低越优先）

    返回：
        [(symbol, p_ncav, ncav), ...] 按P/NCAV升序排列
    """
    scored = []
    for stock in candidates:
        ncav = calculate_ncav(stock)
        if ncav > 0:
            p_ncav = calculate_p_ncav(stock, ncav)
            scored.append((stock.symbol, p_ncav, ncav))

    # 按P/NCAV升序排列
    scored.sort(key=lambda x: x[1])
    return [(s, p, ncav) for s, p, ncav in scored]
