"""
events/queue.py — 事件总线

这是整个系统的"传送带"。

工作方式：
- 数据模块把 BAR 事件放进来（put）
- 策略模块从里面取出来处理（get）
- 处理完把 SIGNAL 事件放进来
- 信号输出模块再取出来打印/保存

这种设计的好处：每个模块只和队列打交道，互相不直接调用。
回测时按时间顺序放入历史数据，策略代码完全不需要改动。
"""

from queue import Empty, Queue

from .base import Event


class EventQueue:
    """
    线程安全的事件队列（FIFO：先进先出）。

    FIFO 的意思：先放进去的事件先被处理，就像排队买票，先到先得。
    """

    def __init__(self) -> None:
        self._queue: Queue[Event] = Queue()

    def put(self, event: Event) -> None:
        """把一个事件放入队列（非阻塞）。"""
        self._queue.put(event)

    def get(self, timeout: float = 0.1) -> Event | None:
        """
        从队列取出一个事件。

        参数：
            timeout: 等待时间（秒）。如果队列为空且超时，返回 None。

        在回测模式中，队列处理很快，通常不会超时。
        """
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def empty(self) -> bool:
        """队列是否为空。"""
        return self._queue.empty()

    def size(self) -> int:
        """队列中当前有多少个事件。"""
        return self._queue.qsize()

    def task_done(self) -> None:
        """标记当前任务完成（用于 join 等待）。"""
        self._queue.task_done()
