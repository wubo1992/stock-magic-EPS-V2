"""
events/base.py — 事件基类定义

这个文件定义了系统中所有事件的"格式"。

类比：就像物流公司用不同颜色的包裹单来区分包裹类型——
红色（BAR）是价格数据到来，黄色（SIGNAL）是策略产生了信号。
每种颜色对应不同的处理流程，但都用同一个传送带（事件队列）传递。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# 信号类型常量（events/base.py 是 SignalEvent 的源头，signals/generator.py 从这里导入）
V_EPS_V2_BUY_SIGNAL_TYPE = "v_eps_v2_buy"
CROSSBACK_BUY_SIGNAL_TYPE = "crossback_buy"


class EventType(str, Enum):
    """
    系统中所有事件的类型枚举。

    BAR：新的K线数据到来（每次喂入一天的价格数据就产生一个 BAR 事件）
    SIGNAL：策略产生了买入/卖出/观望信号
    """
    BAR = "bar"
    SIGNAL = "signal"


@dataclass
class Event:
    """
    所有事件的基础格式。

    字段：
        type:      事件类型（BAR 或 SIGNAL）
        symbol:    股票代码，如 "AAPL"
        timestamp: 这条数据对应的时间（K线的收盘时间）
        data:      具体数据，根据事件类型不同而不同
    """
    type: EventType
    symbol: str
    timestamp: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        date_str = self.timestamp.strftime("%Y-%m-%d")
        return f"Event({self.type.value}, {self.symbol}, {date_str})"


@dataclass
class BarEvent(Event):
    """
    BAR 事件：代表新的一天K线数据到来。

    data 字段包含：
        open:   开盘价
        high:   最高价
        low:    最低价
        close:  收盘价（已复权）
        volume: 成交量
    """

    @classmethod
    def create(
        cls,
        symbol: str,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> "BarEvent":
        return cls(
            type=EventType.BAR,
            symbol=symbol,
            timestamp=timestamp,
            data={
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
        )


@dataclass
class SignalEvent(Event):
    """
    SIGNAL 事件：策略产生了交易信号。

    data 字段包含：
        direction:  "buy"（买入）/ "sell"（卖出）/ "watch"（观望）
        strength:   1-5（信号强度，5最强）
        reason:     触发原因（一句话描述）
        stop_loss:  建议止损价
        shares:     持仓股数（用户手动填写在 positions.json 中）
        signal_type: 信号类型标识。留空（None）时由 signals/generator.py
                    按「策略 ID + 方向」推导（如 v_eps_v2_buy / v_weinstein_buy）；
                    crossback 信号显式设为 "crossback_buy"
        signal_price: 信号当日收盘价（USD），用于 watchlist 持久化
        rs_data:    结构化相对强度数据（RS相对强度/RS/SPY/RS-10W/RS-10D/板块排名），
                    缺失时为 None —— signals.csv 的 RS 列从这里取值
    """

    @classmethod
    def create(
        cls,
        symbol: str,
        timestamp: datetime,
        direction: str,
        strength: int,
        reason: str,
        stop_loss: float | None = None,
        shares: float = 0.0,
        signal_type: str | None = None,  # None = 按策略推导，见 signals/generator.py
        signal_price: float | None = None,  # NEW: close on signal day, for watchlist
        rs_data: dict[str, Any] | None = None,  # NEW: structured RS columns for signals.csv
    ) -> "SignalEvent":
        return cls(
            type=EventType.SIGNAL,
            symbol=symbol,
            timestamp=timestamp,
            data={
                "direction": direction,
                "strength": strength,
                "reason": reason,
                "stop_loss": stop_loss,
                "shares": shares,
                "signal_type": signal_type,
                "signal_price": signal_price,
                "rs_data": rs_data,
            },
        )
