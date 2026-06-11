"""
实时行情监控 - 长桥WebSocket订阅
"""
import asyncio
import logging
import os
from typing import Set, Callable
from datetime import datetime

from realtime.event_bus import Event, get_event_bus

logger = logging.getLogger("realtime.quote_monitor")

# 长桥API环境变量
LONGPORT_APP_KEY = os.environ.get("LONGPORT_APP_KEY", "")
LONGPORT_APP_SECRET = os.environ.get("LONGPORT_APP_SECRET", "")
LONGPORT_ACCESS_TOKEN = os.environ.get("LONGPORT_ACCESS_TOKEN", "")


class QuoteMonitor:
    """实时行情监控器"""

    def __init__(self):
        self._subscribed_codes: Set[str] = set()
        self._running = False
        self._ctx = None
        self._event_bus = get_event_bus()

    def _init_longport(self):
        """初始化长桥连接"""
        try:
            from longport.openapi import Config, QuoteContext, SubType, PushQuote

            config = Config(
                app_key=LONGPORT_APP_KEY,
                app_secret=LONGPORT_APP_SECRET,
                access_token=LONGPORT_ACCESS_TOKEN,
            )
            self._ctx = QuoteContext(config)
            self._SubType = SubType
            logger.info("长桥QuoteContext初始化成功")
            return True
        except ImportError:
            logger.error("longport库未安装: pip install longport")
            return False
        except Exception as e:
            logger.error(f"长桥初始化失败: {e}")
            return False

    def subscribe(self, codes: list):
        """订阅股票实时行情"""
        if not codes:
            return

        self._subscribed_codes.update(codes)
        logger.info(f"订阅实时行情: {len(codes)}只")

        if self._ctx:
            try:
                self._ctx.subscribe(codes, self._SubType.QUOTE, is_first_push=True)
            except Exception as e:
                logger.error(f"订阅失败: {e}")

    def unsubscribe(self, codes: list):
        """取消订阅"""
        if not codes:
            return

        self._subscribed_codes -= set(codes)

        if self._ctx:
            try:
                self._ctx.unsubscribe(codes, self._SubType.QUOTE)
            except Exception as e:
                logger.error(f"取消订阅失败: {e}")

    def _on_quote(self, quote):
        """行情回调"""
        event = Event(
            type="quote_update",
            data={
                "code": quote.symbol,
                "price": float(quote.last_done),
                "volume": int(quote.volume),
                "turnover": float(quote.turnover),
                "timestamp": quote.timestamp,
            }
        )
        self._event_bus.publish_sync(event)

    async def start(self):
        """启动监控"""
        if not self._init_longport():
            logger.error("长桥初始化失败，实时行情不可用")
            return

        self._ctx.set_on_quote(self._on_quote)
        self._running = True
        logger.info("实时行情监控启动")

        # 保持连接（长桥SDK内部是事件循环）
        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        """停止监控"""
        self._running = False
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        logger.info("实时行情监控停止")


# 全局单例
_monitor = QuoteMonitor()

def get_quote_monitor() -> QuoteMonitor:
    return _monitor
