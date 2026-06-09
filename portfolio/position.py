"""
持仓管理模块
买入/卖出/调仓逻辑，止损止盈
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from signals import Direction, logger


@dataclass
class Position:
    """单只股票持仓"""
    code: str
    name: str
    buy_price: float
    shares: int
    buy_date: str
    cost: float = 0.0
    highest_price: float = 0.0

    def __post_init__(self):
        self.cost = self.buy_price * self.shares
        self.highest_price = self.buy_price


@dataclass
class TradeRecord:
    """交易记录"""
    code: str
    name: str
    action: str        # "买入" / "卖出"
    price: float
    shares: int
    date: str
    amount: float      # 交易金额
    reason: str = ""


class PositionManager:
    """
    持仓管理器

    规则:
        - 最多持有 max_stocks 只股票
        - 单只股票持仓上限 max_single_pct
        - 止损 stop_loss (默认 -5%)
        - 止盈 take_profit (默认 +10%)
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        max_stocks: int = 5,
        max_single_pct: float = 0.20,
        stop_loss: float = -0.05,
        take_profit: float = 0.10,
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.max_stocks = max_stocks
        self.max_single_pct = max_single_pct
        self.stop_loss = stop_loss
        self.take_profit = take_profit

        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []

    @property
    def total_value(self) -> float:
        """当前总资产 = 现金 + 持仓市值（按买入成本估算，实时市值需外部传入）"""
        return self.cash + sum(p.cost for p in self.positions.values())

    def market_value(self, prices: Dict[str, float]) -> float:
        """根据实时价格计算持仓市值"""
        value = 0.0
        for code, pos in self.positions.items():
            price = prices.get(code, pos.buy_price)
            value += price * pos.shares
        return value

    def total_assets(self, prices: Dict[str, float]) -> float:
        """总资产 = 现金 + 持仓市值"""
        return self.cash + self.market_value(prices)

    def can_buy(self) -> bool:
        """是否还能买入新股"""
        return len(self.positions) < self.max_stocks

    def max_buy_amount(self) -> float:
        """单只股票最大买入金额"""
        return self.total_value * self.max_single_pct

    def buy(
        self,
        code: str,
        name: str,
        price: float,
        date: str,
        amount: Optional[float] = None,
    ) -> Optional[TradeRecord]:
        """
        买入股票

        Args:
            code: 股票代码
            name: 股票名称
            price: 买入价格
            date: 交易日期
            amount: 买入金额（None则按单只上限自动计算）

        Returns:
            TradeRecord 或 None（买入失败）
        """
        if not self.can_buy():
            logger.warning(f"持仓已满({self.max_stocks}只)，无法买入 {name}")
            return None

        if code in self.positions:
            logger.warning(f"已持有 {name}，不重复买入")
            return None

        if amount is None:
            amount = self.max_buy_amount()

        # 不超过可用现金
        amount = min(amount, self.cash)
        if amount <= 0:
            logger.warning("可用资金不足")
            return None

        # A股最小交易单位100股
        shares = int(amount / price / 100) * 100
        if shares <= 0:
            logger.warning(f"资金不足以买入1手 {name}")
            return None

        actual_amount = shares * price
        self.cash -= actual_amount

        pos = Position(
            code=code, name=name,
            buy_price=price, shares=shares,
            buy_date=date,
        )
        self.positions[code] = pos

        trade = TradeRecord(
            code=code, name=name,
            action="买入", price=price,
            shares=shares, date=date,
            amount=actual_amount,
            reason="信号买入",
        )
        self.trades.append(trade)
        logger.info(f"买入 {name}({code}) {shares}股 @ {price}，金额 {actual_amount:.0f}")
        return trade

    def sell(self, code: str, price: float, date: str, reason: str = "信号卖出") -> Optional[TradeRecord]:
        """卖出全部持仓"""
        if code not in self.positions:
            return None

        pos = self.positions.pop(code)
        amount = pos.shares * price
        self.cash += amount

        trade = TradeRecord(
            code=code, name=pos.name,
            action="卖出", price=price,
            shares=pos.shares, date=date,
            amount=amount, reason=reason,
        )
        self.trades.append(trade)
        profit_pct = (price - pos.buy_price) / pos.buy_price * 100
        logger.info(f"卖出 {pos.name}({code}) {pos.shares}股 @ {price}，"
                     f"盈亏 {profit_pct:+.1f}%，原因: {reason}")
        return trade

    def check_stop_loss_take_profit(self, prices: Dict[str, float], date: str) -> List[TradeRecord]:
        """
        检查止损止盈，返回触发的卖出记录

        Args:
            prices: {code: current_price}
            date: 当前日期

        Returns:
            触发的卖出交易记录列表
        """
        triggered = []
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            current_price = prices.get(code)
            if current_price is None:
                continue

            # 更新最高价
            pos.highest_price = max(pos.highest_price, current_price)

            change_pct = (current_price - pos.buy_price) / pos.buy_price

            if change_pct <= self.stop_loss:
                trade = self.sell(code, current_price, date,
                                  reason=f"止损({change_pct:+.1%})")
                if trade:
                    triggered.append(trade)
            elif change_pct >= self.take_profit:
                trade = self.sell(code, current_price, date,
                                  reason=f"止盈({change_pct:+.1%})")
                if trade:
                    triggered.append(trade)

        return triggered

    def rebalance(self, signals: list, prices: Dict[str, float], date: str) -> List[TradeRecord]:
        """
        根据信号调仓

        Args:
            signals: CompositeSignal 列表（按分数降序）
            prices: {code: price}
            date: 日期

        Returns:
            本次调仓产生的交易记录
        """
        trades = []

        # 1. 卖出：信号为卖出的持仓
        signal_map = {s.code: s for s in signals}
        for code in list(self.positions.keys()):
            sig = signal_map.get(code)
            if sig and sig.direction == Direction.SELL:
                price = prices.get(code, self.positions[code].buy_price)
                trade = self.sell(code, price, date, reason="信号卖出")
                if trade:
                    trades.append(trade)

        # 2. 买入：信号为买入且未持仓的，取前N只
        buy_signals = [s for s in signals
                       if s.direction == Direction.BUY
                       and s.code not in self.positions]
        for sig in buy_signals:
            if not self.can_buy():
                break
            price = prices.get(sig.code)
            if price is None:
                continue
            trade = self.buy(sig.code, sig.name, price, date)
            if trade:
                trades.append(trade)

        return trades
