"""
signals/cigar_butt_v1a/__init__.py

捡烟蒂V1A信号模块：纯格雷厄姆NCAV深度价值
"""

from .scanner import CigarButtV1AScanner
from .filters import (
    CigarButtV1ACriteria,
    StockFundamentalsV1A,
    calculate_ncav,
    calculate_p_ncav,
    passes_filters,
    rank_by_p_ncav,
)

__all__ = [
    "CigarButtV1AScanner",
    "CigarButtV1ACriteria",
    "StockFundamentalsV1A",
    "calculate_ncav",
    "calculate_p_ncav",
    "passes_filters",
    "rank_by_p_ncav",
]
