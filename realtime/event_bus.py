"""
事件总线 - 发布订阅模式
"""
import asyncio
import logging
import threading
from collections import deque
from typing import Callable, Dict, List
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("realtime.event_bus")


@dataclass
class Event:
    """事件基类"""
    type: str
    data: dict
    timestamp: str = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


class EventBus:
    """异步事件总线"""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._queue = asyncio.Queue(maxsize=1000)
        self._running = False
        self._loop: asyncio.AbstractEventLoop = None  # dispatch循环所在线程的loop，跨线程发布用
        self._pending = deque(maxlen=1000)
        self._pending_lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Callable):
        """订阅事件"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.info(f"订阅事件: {event_type} → {handler.__name__}")

    async def publish(self, event: Event):
        """发布事件（异步）"""
        if not self._running or self._loop is None:
            self._remember(event)
            return
        if asyncio.get_running_loop() is not self._loop:
            future = asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
            await asyncio.wrap_future(future)
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug(f"事件队列已满，丢弃事件: {event.type}")

    def _remember(self, event):
        with self._pending_lock:
            if len(self._pending) == self._pending.maxlen:
                logger.debug("启动前事件缓存已满，丢弃最旧事件")
            self._pending.append(event)

    def publish_sync(self, event: Event):
        """同步/跨线程发布。

        优先级:
          1. dispatch循环已在别的线程运行 → run_coroutine_threadsafe 投递
             (行情SDK回调线程/轮询线程都在此路径，避免跨loop操作Queue)
          2. 当前线程已有运行中的loop → create_task (原行为)
          3. 无loop → 兼容旧路径 asyncio.run 入队
        """
        if self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
                return
            except RuntimeError:
                pass  # loop刚关闭，走后面的兑底
        # 未启动时只缓存普通对象，不创建临时loop或跨loop操作Queue。
        self._remember(event)

    async def _dispatch_loop(self):
        """事件分发循环"""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                handlers = self._handlers.get(event.type, [])

                for handler in handlers:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(event)
                        else:
                            handler(event)
                    except Exception as e:
                        logger.error(f"事件处理失败 {event.type}: {e}", exc_info=True)

            except asyncio.TimeoutError:
                continue

    async def start(self):
        """启动事件循环"""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=1000)
        self._running = True
        with self._pending_lock:
            while self._pending:
                self._queue.put_nowait(self._pending.popleft())
        logger.info("事件总线启动")
        try:
            await self._dispatch_loop()
        finally:
            self._running = False
            self._loop = None

    def stop(self):
        """停止事件循环"""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                owner = asyncio.get_running_loop() is loop
            except RuntimeError:
                owner = False
            if not owner:
                loop.call_soon_threadsafe(self._stop_in_loop)
                return
        self._stop_in_loop()

    def _stop_in_loop(self):
        self._running = False
        dropped = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            dropped += 1
        if dropped:
            logger.info("事件总线关闭，明确丢弃%d条未处理传感器事件", dropped)
        logger.info("事件总线停止")


# 全局单例
_bus = EventBus()

def get_event_bus() -> EventBus:
    return _bus
