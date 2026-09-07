"""
实时行情监控 - 长桥WebSocket订阅 / 新浪快照轮询回退

优先使用长桥OpenAPI推送（需 LONGPORT_APP_KEY/SECRET/ACCESS_TOKEN）。
凭证缺失或SDK不可用时，自动回退到新浪公开快照轮询（KT适配器，
data/kt_realtime.py），保证无凭证环境（纸面模式）也有实时行情事件。
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

# 轮询回退间隔(秒)。新浪接口有频率限制，不要低于1秒。
POLL_INTERVAL_SECONDS = float(os.environ.get("REALTIME_POLL_INTERVAL", "3"))


class QuoteMonitor:
    """实时行情监控器（长桥推送优先，新浪快照轮询回退）"""

    def __init__(self):
        self._subscribed_codes: Set[str] = set()
        self._running = False
        self._ctx = None
        self._mode = None  # "longport" | "polling"
        self._poll_client = None
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

    def _init_polling(self) -> bool:
        """初始化新浪快照轮询回退（无凭证/无SDK时）"""
        try:
            from data.kt_realtime import get_kt_client
            self._poll_client = get_kt_client()
        except Exception as e:
            logger.error(f"轮询回退初始化失败: {e}")
            return False
        self._mode = "polling"
        logger.info(
            "使用新浪快照轮询回退 (无推送，间隔%.0fs，标的数上限受分片限制)",
            POLL_INTERVAL_SECONDS,
        )
        return True

    def subscribe(self, codes: list):
        """订阅股票实时行情"""
        if not codes:
            return

        self._subscribed_codes.update(codes)
        logger.info(f"订阅实时行情 ({self._mode or 'pending'}): {len(codes)}只")

        if self._mode == "longport" and self._ctx:
            try:
                self._ctx.subscribe(codes, self._SubType.QUOTE, is_first_push=True)
            except Exception as e:
                logger.error(f"订阅失败: {e}")
        elif self._mode == "polling":
            self._restart_poll_stream()

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

    def _restart_poll_stream(self):
        """用当前订阅集重启轮询流（KT轮询线程退出后才能重启）"""
        if self._poll_client is None:
            return
        codes = sorted(self._subscribed_codes)
        if not codes:
            return
        try:
            self._poll_client.stop_stream(timeout=8.0)
        except Exception as e:
            logger.debug(f"停止旧轮询流异常(忽略): {e}")
        try:
            started = self._poll_client.start_stream(
                codes,
                on_tick=self._on_poll_tick,
                interval=POLL_INTERVAL_SECONDS,
                only_delta=True,
                on_error=lambda message: logger.warning("行情轮询: %s", message),
            )
        except Exception as e:
            logger.error(f"轮询流启动失败: {e}")
            return
        if not started:
            logger.warning("轮询流未立即启动(旧线程退出中)，将在下次订阅时重试")

    def _on_poll_tick(self, row: dict):
        """轮询tick回调（在轮询线程执行）→ 发布与长桥同构的行情事件"""
        volume = row.get("volume_hand")
        try:
            volume = int(float(volume)) if volume == volume else 0
        except (TypeError, ValueError):
            volume = 0
        try:
            price = float(row.get("price") or 0)
            turnover = float(row.get("amount_yuan") or 0)
        except (TypeError, ValueError):
            price, turnover = 0.0, 0.0
        event = Event(
            type="quote_update",
            data={
                "code": str(row.get("code") or ""),
                "price": price,
                "volume": volume,
                "turnover": turnover,
                "timestamp": str(row.get("time") or ""),
                "source": "sina_poll",
            },
        )
        self._event_bus.publish_sync(event)

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
        """启动监控: 长桥优先，失败自动轮询回退"""
        if self._init_longport():
            pass
        elif self._init_polling():
            pass
        else:
            logger.error("长桥初始化失败且轮询回退不可用，实时行情不可用")
            return

        # 兼容先subscribe后start的调用顺序（EventDrivenTrader就是先订阅）
        if self._subscribed_codes:
            self.subscribe(list(self._subscribed_codes))

        self._running = True
        logger.info(f"实时行情监控启动 ({self._mode})")

        if self._mode == "longport":
            self._ctx.set_on_quote(self._on_quote)

        # 保持任务存活（轮询模式由KT后台线程推送事件）
        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        """停止监控"""
        self._running = False
        if self._mode == "polling" and self._poll_client is not None:
            try:
                self._poll_client.stop_stream(timeout=8.0)
            except Exception:
                pass
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
