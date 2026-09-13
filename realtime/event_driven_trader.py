"""
事件驱动交易员 - 主循环
"""
import asyncio
import logging
import signal
from datetime import datetime

from realtime.event_bus import get_event_bus, Event
from realtime.quote_monitor import get_quote_monitor
from realtime.event_handlers import StopLossHandler, MarketEventHandler
from scheduler.market_calendar import get_market_status

logger = logging.getLogger("realtime.trader")


class EventDrivenTrader:
    """事件驱动交易员"""

    def __init__(self):
        self._event_bus = get_event_bus()
        self._quote_monitor = get_quote_monitor()
        self._running = False
        self._tasks = []

    def _register_handlers(self, account):
        """注册事件处理器"""
        # 止损处理器
        stop_loss = StopLossHandler(account)
        self._event_bus.subscribe("quote_update", stop_loss.on_quote_update)

        # 市场事件处理器
        market = MarketEventHandler()
        self._event_bus.subscribe("index_change", market.on_index_change)
        self._event_bus.subscribe("position_risk", market.on_position_risk)

    def _subscribe_positions(self, account):
        """订阅持仓股票实时行情"""
        codes = list(account.positions.keys())
        if codes:
            self._quote_monitor.subscribe(codes)
            logger.info(f"订阅持仓股票: {codes}")

    async def _schedule_tasks(self):
        """低频定时任务（复盘、市场环境识别）"""
        from scheduler.pipeline import run_review

        last_review_date = None
        while self._running:
            market = get_market_status()
            now = datetime.now()

            # 收盘后复盘: get_market_status() 返回中文状态("盘后"/"休市"等)，
            # 15:05 之后触发，每个交易日最多一次。
            if (
                market == "盘后"
                and (now.hour, now.minute) >= (15, 5)
                and last_review_date != now.strftime("%Y-%m-%d")
            ):
                last_review_date = now.strftime("%Y-%m-%d")
                try:
                    logger.info("触发收盘复盘")
                    await asyncio.to_thread(run_review)
                except Exception as e:
                    logger.error(f"复盘失败: {e}")

            await asyncio.sleep(60)

    async def run(self):
        """启动事件驱动循环"""
        from execution.broker import get_broker_adapter

        broker = get_broker_adapter()
        account = broker.account if hasattr(broker, "account") else broker

        logger.info("事件驱动交易员启动")

        # 注册处理器
        self._register_handlers(account)

        # 订阅持仓
        self._subscribe_positions(account)

        # 启动任务
        self._running = True
        self._tasks = [
            asyncio.create_task(self._event_bus.start()),
            asyncio.create_task(self._quote_monitor.start()),
            asyncio.create_task(self._schedule_tasks()),
        ]

        # 优雅退出
        def shutdown(sig, frame):
            logger.info(f"收到信号 {sig}，准备退出")
            self.stop()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # 等待所有任务
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

        logger.info("事件驱动交易员退出")

    def stop(self):
        """停止"""
        self._running = False
        self._event_bus.stop()
        self._quote_monitor.stop()

        for task in self._tasks:
            task.cancel()


async def main():
    """主入口"""
    trader = EventDrivenTrader()
    await trader.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    asyncio.run(main())
