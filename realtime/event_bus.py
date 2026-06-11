"""
事件总线 - 发布订阅模式
"""
import asyncio
import logging
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

    def subscribe(self, event_type: str, handler: Callable):
        """订阅事件"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.info(f"订阅事件: {event_type} → {handler.__name__}")

    async def publish(self, event: Event):
        """发布事件（异步）"""
        try:
            await self._queue.put(event)
        except asyncio.QueueFull:
            logger.warning(f"事件队列已满，丢弃事件: {event.type}")

    def publish_sync(self, event: Event):
        """发布事件（同步包装）"""
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self.publish(event))
        except RuntimeError:
            # 无事件循环时直接处理
            asyncio.run(self.publish(event))

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
        self._running = True
        logger.info("事件总线启动")
        await self._dispatch_loop()

    def stop(self):
        """停止事件循环"""
        self._running = False
        logger.info("事件总线停止")


# 全局单例
_bus = EventBus()

def get_event_bus() -> EventBus:
    return _bus
