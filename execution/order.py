"""
订单管理模块
创建/执行/取消订单，支持批量下单和订单状态追踪
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict
from enum import Enum

from execution.paper_account import PaperAccount

logger = logging.getLogger("execution.order")


class OrderStatus(Enum):
    PENDING = "pending"       # 待执行
    FILLED = "filled"         # 已成交
    CANCELLED = "cancelled"   # 已取消
    FAILED = "failed"         # 失败


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Order:
    """订单"""
    order_id: str
    code: str
    name: str
    side: str                  # BUY / SELL
    price: float
    shares: int
    status: str = "pending"
    reason: str = ""
    filled_price: float = 0.0
    filled_shares: int = 0
    filled_amount: float = 0.0
    commission: float = 0.0
    created_at: str = ""
    filled_at: str = ""
    error: str = ""
    atr: float = 0.0           # 买入时的ATR值（用于动态止损）

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def __repr__(self):
        return (
            f"Order({self.order_id}) {self.side} {self.code} {self.name} "
            f"{self.shares}股@{self.price} [{self.status}]"
        )


class OrderManager:
    """
    订单管理器

    功能:
        - 创建买入/卖出订单
        - 执行订单（调用PaperAccount）
        - 批量下单
        - 订单状态追踪
        - 撤单
    """

    def __init__(self, account: PaperAccount = None):
        self.account = account or PaperAccount()
        self.orders: List[Order] = []
        self._order_counter = 0

    def _next_order_id(self) -> str:
        self._order_counter += 1
        now = datetime.now().strftime("%m%d%H%M")
        return f"O{now}{self._order_counter:04d}"

    # ── 创建订单 ──────────────────────────────────────────────────

    def create_buy_order(
        self,
        code: str,
        name: str,
        price: float,
        shares: int = None,
        amount: float = None,
        reason: str = "信号买入",
        atr: float = None,
    ) -> Order:
        """
        创建买入订单

        Args:
            code: 股票代码
            name: 股票名称
            price: 预期买入价
            shares: 买入股数（与amount二选一）
            amount: 买入金额
            reason: 买入原因
            atr: 买入时的ATR值（用于动态止损）

        Returns:
            Order对象
        """
        if shares is None and amount is not None:
            shares = int(amount / price / 100) * 100
        if shares is None:
            shares = 100

        order = Order(
            order_id=self._next_order_id(),
            code=code,
            name=name,
            side="BUY",
            price=price,
            shares=shares,
            reason=reason,
            atr=atr if atr and atr > 0 else 0.0,
        )
        self.orders.append(order)
        logger.info(f"创建买入订单: {order}")
        return order

    def create_sell_order(
        self,
        code: str,
        price: float,
        shares: int = None,
        reason: str = "信号卖出",
    ) -> Order:
        """
        创建卖出订单

        Args:
            code: 股票代码
            price: 预期卖出价
            shares: 卖出股数（None=全部）
            reason: 卖出原因

        Returns:
            Order对象
        """
        pos = self.account.get_position(code)
        name = pos["name"] if pos else code

        if shares is None and pos:
            shares = pos["shares"]

        order = Order(
            order_id=self._next_order_id(),
            code=code,
            name=name,
            side="SELL",
            price=price,
            shares=shares,
            reason=reason,
        )
        self.orders.append(order)
        logger.info(f"创建卖出订单: {order}")
        return order

    # ── 执行订单 ──────────────────────────────────────────────────

    def execute_order(self, order: Order, current_price: float = None) -> bool:
        """
        执行订单

        Args:
            order: 订单对象
            current_price: 最新价（None则使用订单价格）

        Returns:
            是否成功
        """
        if order.status != OrderStatus.PENDING.value:
            logger.warning(f"订单 {order.order_id} 状态={order.status}, 无法执行")
            return False

        price = current_price or order.price

        try:
            if order.side == "BUY":
                trade = self.account.buy(
                    code=order.code,
                    name=order.name,
                    price=price,
                    shares=order.shares,
                    reason=order.reason,
                    atr=order.atr if order.atr > 0 else None,
                )
            else:
                trade = self.account.sell(
                    code=order.code,
                    price=price,
                    shares=order.shares,
                    reason=order.reason,
                )

            if trade:
                order.status = OrderStatus.FILLED.value
                order.filled_price = price
                order.filled_shares = trade["shares"]
                order.filled_amount = trade["amount"]
                order.commission = trade.get("commission", 0)
                order.filled_at = datetime.now().isoformat()
                logger.info(f"订单成交: {order.order_id} {order.side} {order.code} {order.filled_shares}股@{price}")
                return True
            else:
                order.status = OrderStatus.FAILED.value
                order.error = "账户执行失败（资金不足/持仓已满等）"
                logger.warning(f"订单失败: {order.order_id} {order.error}")
                return False

        except Exception as e:
            order.status = OrderStatus.FAILED.value
            order.error = str(e)
            logger.error(f"订单异常: {order.order_id} {e}")
            return False

    def execute_pending(self, prices: Dict[str, float] = None) -> int:
        """
        执行所有待处理订单

        Args:
            prices: {code: current_price} 最新价格字典

        Returns:
            成功执行的订单数
        """
        count = 0
        for order in self.orders:
            if order.status != OrderStatus.PENDING.value:
                continue

            price = (prices or {}).get(order.code, order.price)
            if self.execute_order(order, price):
                count += 1

        return count

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        for order in self.orders:
            if order.order_id == order_id and order.status == OrderStatus.PENDING.value:
                order.status = OrderStatus.CANCELLED.value
                logger.info(f"取消订单: {order_id}")
                return True
        return False

    # ── 批量下单 ──────────────────────────────────────────────────

    def batch_buy(self, candidates: list, prices: Dict[str, float]) -> List[Order]:
        """
        批量买入

        Args:
            candidates: [{code, name, price, amount}, ...] 或有 code/name 属性的对象
            prices: {code: price} 最新价

        Returns:
            创建的订单列表
        """
        orders = []
        for c in candidates:
            code = c.code if hasattr(c, "code") else c.get("code", "")
            name = c.name if hasattr(c, "name") else c.get("name", "")
            price = prices.get(code)
            if not price:
                continue

            order = self.create_buy_order(
                code=code, name=name, price=price,
                reason="批量买入",
            )
            orders.append(order)

        return orders

    def batch_sell(self, codes: List[str], prices: Dict[str, float], reason: str = "批量卖出") -> List[Order]:
        """批量卖出"""
        orders = []
        for code in codes:
            price = prices.get(code)
            if not price:
                continue
            order = self.create_sell_order(code=code, price=price, reason=reason)
            orders.append(order)
        return orders

    # ── 查询 ──────────────────────────────────────────────────────

    def get_pending_orders(self) -> List[Order]:
        return [o for o in self.orders if o.status == OrderStatus.PENDING.value]

    def get_filled_orders(self, today_only: bool = False) -> List[Order]:
        filled = [o for o in self.orders if o.status == OrderStatus.FILLED.value]
        if today_only:
            today = datetime.now().strftime("%Y-%m-%d")
            filled = [o for o in filled if o.filled_at.startswith(today)]
        return filled

    def get_order(self, order_id: str) -> Optional[Order]:
        for o in self.orders:
            if o.order_id == order_id:
                return o
        return None

    def today_trade_count(self) -> int:
        return len(self.get_filled_orders(today_only=True))

    # ── 展示 ──────────────────────────────────────────────────────

    def format_orders(self, orders: List[Order] = None) -> str:
        """格式化订单列表"""
        if orders is None:
            orders = self.orders
        if not orders:
            return "无订单"

        lines = [
            "-" * 75,
            f"{'订单号':<14} {'方向':<4} {'代码':<8} {'名称':<8} "
            f"{'股数':>6} {'价格':>8} {'状态':<10} {'原因'}",
            "-" * 75,
        ]

        for o in orders:
            lines.append(
                f"{o.order_id:<14} {o.side:<4} {o.code:<8} {o.name:<8} "
                f"{o.shares:>6} {o.price:>8.2f} {o.status:<10} {o.reason}"
            )

        lines.append("-" * 75)
        pending = sum(1 for o in orders if o.status == "pending")
        filled = sum(1 for o in orders if o.status == "filled")
        lines.append(f"共 {len(orders)} 笔: 待执行={pending} 已成交={filled}")
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    account = PaperAccount()
    om = OrderManager(account)

    # 创建买入订单
    o1 = om.create_buy_order("600519", "贵州茅台", 1800.0, amount=200000)
    o2 = om.create_buy_order("300750", "宁德时代", 200.0, amount=100000)

    print("订单列表:")
    print(om.format_orders())

    # 执行
    om.execute_pending({"600519": 1798.0, "300750": 201.5})

    print("\n执行后:")
    print(om.format_orders())

    print("\n账户状态:")
    print(account.format_status({"600519": 1850.0, "300750": 195.0}))
