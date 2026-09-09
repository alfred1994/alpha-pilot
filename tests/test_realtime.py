"""
事件驱动架构单元测试
"""
import asyncio
import copy
import logging
import os
import sys
import tempfile
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime.event_bus import Event, get_event_bus
from realtime.event_handlers import StopLossHandler
from data.quote_validation import BEIJING_TZ

logging.basicConfig(level=logging.INFO)


def _fresh_timestamp():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
        self.evaluations = []
        self.executions = 0

    def evaluate_stop_conditions(self, prices):
        """模拟纯止损判断，不应修改账户。"""
        self.evaluations.append(dict(prices))
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

    def check_stop_conditions(self, prices):
        """故意带副作用，防止传感器误调用执行型API。"""
        self.executions += 1
        self.sells.append({"code": next(iter(prices)), "reason": "不应执行"})
        return []

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
        data={"code": "600519", "price": 1650, "timestamp": _fresh_timestamp()}  # -8.3%
    )

    try:
        notifier.send_message = lambda message: messages.append(message)
        await handler.on_quote_update(event)
        await asyncio.sleep(0.1)
    finally:
        notifier.send_message = old_send_message

    assert len(account.sells) == 0, "实时传感器不应该直接卖出"
    assert account.executions == 0 and account.evaluations, "传感器只能调用纯判断API"
    assert len(messages) == 1, "应该广播1条止损建议"
    assert "600519" in messages[0]
    print("[OK] 止损处理器测试通过")


async def test_stop_loss_rejects_stale_quote():
    """过期行情既不评估也不广播，避免以旧价驱动风险动作。"""
    account = MockAccount()
    handler = StopLossHandler(account)
    await handler.on_quote_update(Event(
        type="quote_update",
        data={"code": "600519", "price": 1650, "timestamp": "2000-01-01 09:30:00"},
    ))
    assert not account.evaluations and not account.sells, "过期行情不会触发止损传感器"
    print("[OK] 过期实时行情被止损传感器拒绝")


async def test_paper_account_stop_sensor_is_readonly():
    """真实账户的实时传感器只能通知，不能卖出、改最高价或新增成交。"""
    from data.database import Database
    from execution.paper_account import PaperAccount

    with tempfile.TemporaryDirectory(prefix="quote_sensor_") as directory:
        account_path = os.path.join(directory, "paper.json")
        db_path = os.path.join(directory, "paper.db")
        account = PaperAccount(filepath=account_path, db_path=db_path)
        assert account.buy("600519", "测试股票", 100.0, shares=1000)
        before_position = copy.deepcopy(account.positions["600519"])
        with Database(db_path=db_path) as db:
            before_trades = len(db.get_trades(code="600519", limit=20))

        messages = []
        import scheduler.notifier as notifier
        original_send_message = notifier.send_message
        try:
            notifier.send_message = messages.append
            await StopLossHandler(account).on_quote_update(Event(
                type="quote_update",
                data={"code": "600519", "price": 80.0, "timestamp": _fresh_timestamp()},
            ))
        finally:
            notifier.send_message = original_send_message

        with Database(db_path=db_path) as db:
            after_trades = len(db.get_trades(code="600519", limit=20))
        assert account.positions["600519"] == before_position, "真实账户持仓与最高价均未被传感器修改"
        assert after_trades == before_trades, "真实账户不会新增卖出成交记录"
        assert len(messages) == 1, "真实账户止损建议恰好通知一次"
    print("[OK] 真实账户止损传感器只通知不成交")


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
            data={"code": "600519", "price": 1650, "timestamp": _fresh_timestamp()}
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
            "amount_yuan": 6.3e8, "time": _fresh_timestamp(), "data_valid": True,
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


async def test_quote_monitor_longport_subscribes_after_successful_init():
    """长桥初始化成功必须进入longport分支，先注册回调后恢复已有订阅。"""
    import realtime.quote_monitor as qm

    contexts = []

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeContext:
        def __init__(self, config):
            self.subscriptions = []
            self.unsubscriptions = []
            self.callback = None
            self.closed = False
            contexts.append(self)

        def set_on_quote(self, callback):
            self.callback = callback

        def subscribe(self, symbols, sub_types):
            expected_symbols = ["123001.SZ", "301001.SZ", "600519.SH"]
            if symbols != expected_symbols or sub_types != [FakeSubType.Quote]:
                raise AssertionError(f"Longport subscribe参数不符合SDK契约: {symbols!r}, {sub_types!r}")
            self.subscriptions.append((list(symbols), list(sub_types)))

        def unsubscribe(self, symbols, sub_types):
            expected_symbols = ["123001.SZ", "301001.SZ", "600519.SH"]
            if symbols != expected_symbols or sub_types != [FakeSubType.Quote]:
                raise AssertionError(f"Longport unsubscribe参数不符合SDK契约: {symbols!r}, {sub_types!r}")
            self.unsubscriptions.append((list(symbols), list(sub_types)))

        def close(self):
            self.closed = True

    class FakeSubType:
        Quote = "Quote"

    fake_longport = types.ModuleType("longport")
    fake_openapi = types.ModuleType("longport.openapi")
    fake_openapi.Config = FakeConfig
    fake_openapi.QuoteContext = FakeContext
    fake_openapi.SubType = FakeSubType
    fake_openapi.PushQuote = object
    fake_longport.openapi = fake_openapi
    original_longport = sys.modules.get("longport")
    original_openapi = sys.modules.get("longport.openapi")
    sys.modules["longport"] = fake_longport
    sys.modules["longport.openapi"] = fake_openapi

    monitor = qm.QuoteMonitor()
    published = []
    monitor._event_bus = types.SimpleNamespace(publish_sync=published.append)
    monitor.subscribe(["600519", "301001", "123001"])
    try:
        task = asyncio.create_task(monitor.start())
        for _ in range(50):
            if contexts and contexts[-1].callback and contexts[-1].subscriptions:
                break
            await asyncio.sleep(0.02)
        context = contexts[-1]
        assert monitor._mode == "longport", "长桥成功后必须设置longport模式"
        assert context.callback is not None, "恢复订阅前必须已注册行情回调"
        assert context.subscriptions == [(["123001.SZ", "301001.SZ", "600519.SH"], ["Quote"])], "长桥成功后必须用SDK格式订阅沪深与可转债持仓"
        context.callback("600519.SH", types.SimpleNamespace(
            last_done=1330.0, volume=100, turnover=1000.0,
            timestamp=datetime.now(BEIJING_TZ),
        ))
        assert (
            len(published) == 1
            and published[0].data["source"] == "longport"
            and published[0].data["code"] == "600519"
        ), "长桥行情应还原为账户持仓使用的裸代码"
        monitor.unsubscribe(["600519", "301001", "123001"])
        assert context.unsubscriptions == [(["123001.SZ", "301001.SZ", "600519.SH"], ["Quote"])], "长桥退订遵循SDK参数形状"
    finally:
        monitor.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if original_longport is None:
            sys.modules.pop("longport", None)
        else:
            sys.modules["longport"] = original_longport
        if original_openapi is None:
            sys.modules.pop("longport.openapi", None)
        else:
            sys.modules["longport.openapi"] = original_openapi
    print("[OK] 长桥成功初始化后回调和订阅均已启动")


async def main():
    """运行所有测试"""
    print("=" * 50)
    print("事件驱动架构单元测试")
    print("=" * 50)

    await test_event_bus()
    await test_stop_loss()
    await test_stop_loss_rejects_stale_quote()
    await test_paper_account_stop_sensor_is_readonly()
    await test_integration()
    await test_publish_sync_cross_thread()
    await test_quote_monitor_polling_fallback()
    await test_quote_monitor_longport_subscribes_after_successful_init()

    print("\n" + "=" * 50)
    print("所有测试通过")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
