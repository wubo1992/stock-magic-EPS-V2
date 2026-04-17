"""
strategies/base.py — 策略抽象基类

所有策略都必须继承此类并实现 run_date() 方法。

使用方式：
    class MyStrategy(StrategyBase):
        strategy_id = "my_strategy"
        strategy_name = "我的策略"

        def run_date(self, date, queue):
            ...
"""

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from events.queue import EventQueue


class StrategyBase(ABC):
    """
    策略基类。

    类属性（子类必须覆盖）：
        strategy_id:   策略唯一标识符，用于输出文件夹命名，如 "v1_wizard"
        strategy_name: 策略显示名称，用于报告标题，如 "魔法师策略V1"
    """

    strategy_id: str = "base"
    strategy_name: str = "基础策略"

    def __init__(
        self,
        strategy_config: dict,
        market_data: dict[str, pd.DataFrame],
    ) -> None:
        """
        初始化策略。

        参数：
            strategy_config: 本策略专属的配置字典
                             （即 config["strategies"][strategy_id]，不是整个 config）
            market_data:     股票历史数据，key 为股票代码，value 为 DataFrame
        """
        self.cfg = strategy_config
        self.market_data = market_data

    @abstractmethod
    def run_date(self, date: datetime, queue: EventQueue) -> list:
        """
        对单个交易日运行策略，把产生的信号放入 queue。

        参数：
            date:  当前交易日（datetime，含时区）
            queue: 事件队列，调用 queue.put(signal_event) 发布信号

        返回：
            当天产生的信号列表（同时放入 queue）
        """
        ...
