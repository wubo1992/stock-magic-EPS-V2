"""
signals/cigar_butt_v1b/__init__.py

捡烟蒂V1B信号模块：深度价值 + 盈利过滤
"""

from .scanner import CigarButtV1BSCanner
from .filters import CigarButtV1BCriteria, StockFundamentalsV1B, passes_filters_v1b

__all__ = ["CigarButtV1BSCanner", "CigarButtV1BCriteria", "StockFundamentalsV1B", "passes_filters_v1b"]
