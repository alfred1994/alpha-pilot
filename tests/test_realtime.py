"""
事件驱动架构单元测试
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime.event_bus import Event, get_event_bus
from realtime.event_handlers import StopLossHandler

logging.basicConfig(level=logging.INFO)


class MockAccount:
    """模拟账户"""
    def __init__(self):
        self.positions = {
            "600519": {
                "name": "贵州茅台",
                "shares": 100,
                "buy_price": 1800,
                "highest_price": 1850,
            }
        }
        self.sells = []

    def check_stop_conditions(self, prices):
        """模拟止损检查"""
        signals = []
        for code, price in prices.items():
            pos = self.positions.get(code)
            if pos:
                buy_price = pos["buy_price"]
                loss = (price - buy_price) / buy_price
                if loss < -0.08:
                    signals.append({
                        "code": code,
                        "action": "stop_loss",
                        "reason": f"跌破止损位 {loss:.2%}",
                    })
        return signals

    def sell(self, code, price, shares, reason=""):
        """模拟卖出"""
        self.sells.append({
            "code": code,
            "price": price,
            "shares": shares,
            "reason": reason,
        })
        print(f"[模拟卖出] {code} {shares}股 @ {price} | {reason}")


async def test_event_bus():
    """测试事件总线"""
    bus = get_event_bus()

    received = []
    def handler(event):
        received.append(event)
        print(f"收到事件: {event.type} {event.data}")

    bus.subscribe("test", handler)

    # 启动事件循环
    bus_task = asyncio.create_task(bus.start())

    # 发布事件
    await bus.publish(Event(type="test", data={"msg": "hello"}))
    await asyncio.sleep(0.5)

    bus.stop()
    await bus_task

    assert len(received) == 1, "应该收到1个事件"
    print("[OK] 事件总线测试通过")


async def test_stop_loss():
    """测试止损处理器"""
    account = MockAccount()
    handler = StopLossHandler(account)
    messages = []

    import scheduler.notifier as notifier
    old_send_message = notifier.send_message

    # 模拟价格跌破止损位
    event = Event(
        type="quote_update",
        data={"code": "600519", "price": 1650}  # -8.3%
    )

    try:
        notifier.send_message = lambda message: messages.append(message)
        await handler.on_quote_update(event)
        await asyncio.sleep(0.1)
    finally:
        notifier.send_message = old_send_message

    assert len(account.sells) == 0, "实时传感器不应该直接卖出"
    assert len(messages) == 1, "应该广播1条止损建议"
    assert "600519" in messages[0]
    print("[OK] 止损处理器测试通过")


async def test_integration():
    """集成测试"""
    bus = get_event_bus()
    account = MockAccount()
    handler = StopLossHandler(account)
    messages = []

    import scheduler.notifier as notifier
    old_send_message = notifier.send_message

    # 注册处理器
    bus.subscribe("quote_update", handler.on_quote_update)

    # 启动事件循环
    bus_task = asyncio.create_task(bus.start())

    try:
        notifier.send_message = lambda message: messages.append(message)

        # 发布行情事件
        await bus.publish(Event(
            type="quote_update",
            data={"code": "600519", "price": 1650}
        ))

        await asyncio.sleep(0.5)
    finally:
        notifier.send_message = old_send_message
        bus.stop()
        await bus_task

    assert len(account.sells) == 0, "实时传感器不应该直接卖出"
    assert len(messages) == 1, "应该触发1次止损建议广播"
    print("[OK] 集成测试通过")


async def test_publish_sync_cross_thread():
    """跨线程publish_sync: 应投递到运行中的dispatch循环"""
    import threading
    from realtime.event_bus import EventBus

    bus = EventBus()
    received = []
    bus.subscribe("thread_test", lambda event: received.append(event))

    loop = asyncio.new_event_loop()
    bus_thread_done = threading.Event()

    def run_bus():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bus.start())
        finally:
            loop.close()
            bus_thread_done.set()

    thread = threading.Thread(target=run_bus, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            if bus._loop is not None and bus._running:
                break
            await asyncio.sleep(0.02)
        assert bus._loop is not None, "dispatch循环应已启动"

        bus.publish_sync(Event(type="thread_test", data={"msg": "cross"}))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert len(received) == 1, f"跨线程发布应到达handler (收到{len(received)})"
        print("[OK] 跨线程publish_sync测试通过")
    finally:
        bus.stop()
        await asyncio.to_thread(bus_thread_done.wait, 3.0)


async def test_quote_monitor_polling_fallback():
    """无Longport凭证时: 回退新浪轮询并发布同构行情事件"""
    import data.kt_realtime as kt
    import realtime.quote_monitor as qm

    class StubClient:
        def __init__(self):
            self.started_with = None
            self.start_kwargs = None
            self.stop_count = 0
            self.on_tick = None

        def start_stream(self, codes, on_tick=None, on_batch=None,
                         interval=0.5, only_delta=True, on_error=None):
            self.started_with = list(codes)
            self.start_kwargs = {"interval": interval, "only_delta": only_delta}
            self.on_tick = on_tick
            return True

        def stop_stream(self, timeout=5.0):
            self.stop_count += 1
            return True

    stub = StubClient()
    original_init = qm.QuoteMonitor._init_longport
    original_client_getter = kt.get_kt_client
    monitor = qm.QuoteMonitor()
    published = []

    class FakeBus:
        def publish_sync(self, event):
            published.append(event)

    monitor._event_bus = FakeBus()
    try:
        qm.QuoteMonitor._init_longport = lambda self: False
        kt.get_kt_client = lambda: stub

        monitor.subscribe(["600519", "000001"])  # start前订阅: 暂不启动流
        assert stub.started_with is None, "start之前不应启动轮询流"

        task = asyncio.create_task(monitor.start())
        for _ in range(50):
            if monitor._mode == "polling" and stub.started_with:
                break
            await asyncio.sleep(0.02)
        assert monitor._mode == "polling", "无Longport凭证应回退轮询模式"
        assert stub.started_with == ["000001", "600519"], f"start后应补订阅并启动轮询流: {stub.started_with}"
        assert stub.start_kwargs["only_delta"] is True, "轮询流开启delta去重"

        stub.on_tick({
            "code": "600519", "price": 1330.0, "volume_hand": 47487.0,
            "amount_yuan": 6.3e8, "time": "2026-09-04 15:00:03",
        })
        assert len(published) == 1, "tick应发布到事件总线"
        data = published[0].data
        assert published[0].type == "quote_update", "事件类型为quote_update"
        assert data["code"] == "600519" and abs(data["price"] - 1330.0) < 1e-9, "事件代码与价格"
        assert data["volume"] == 47487 and abs(data["turnover"] - 6.3e8) < 1e-6, "事件量额"
        assert data["source"] == "sina_poll", "事件来源标注"

        monitor.stop()
        assert stub.stop_count >= 1, "stop应停止轮询流"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print("[OK] 行情监控轮询回退测试通过")
    finally:
        qm.QuoteMonitor._init_longport = original_init
        kt.get_kt_client = original_client_getter


async def main():
    """运行所有测试"""
    print("=" * 50)
    print("事件驱动架构单元测试")
    print("=" * 50)

    await test_event_bus()
    await test_stop_loss()
    await test_integration()
    await test_publish_sync_cross_thread()
    await test_quote_monitor_polling_fallback()

    print("\n" + "=" * 50)
    print("所有测试通过")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
