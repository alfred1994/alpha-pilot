"""
事件驱动架构单元测试
"""
import asyncio
import logging
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

    # 模拟价格跌破止损位
    event = Event(
        type="quote_update",
        data={"code": "600519", "price": 1650}  # -8.3%
    )

    await handler.on_quote_update(event)
    await asyncio.sleep(0.1)

    assert len(account.sells) == 1, "应该触发1次卖出"
    assert account.sells[0]["code"] == "600519"
    print("[OK] 止损处理器测试通过")


async def test_integration():
    """集成测试"""
    bus = get_event_bus()
    account = MockAccount()
    handler = StopLossHandler(account)

    # 注册处理器
    bus.subscribe("quote_update", handler.on_quote_update)

    # 启动事件循环
    bus_task = asyncio.create_task(bus.start())

    # 发布行情事件
    await bus.publish(Event(
        type="quote_update",
        data={"code": "600519", "price": 1650}
    ))

    await asyncio.sleep(0.5)

    bus.stop()
    await bus_task

    assert len(account.sells) == 1, "应该触发1次止损"
    print("[OK] 集成测试通过")


async def main():
    """运行所有测试"""
    print("=" * 50)
    print("事件驱动架构单元测试")
    print("=" * 50)

    await test_event_bus()
    await test_stop_loss()
    await test_integration()

    print("\n" + "=" * 50)
    print("所有测试通过")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
