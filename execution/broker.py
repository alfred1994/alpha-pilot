"""
交易通道适配层
====================================================================
统一封装模拟盘和未来实盘交易通道。

当前默认只启用 PaperBrokerAdapter，所有真实交易接口都保持显式未实现。
这样主链路可以先按“券商适配器”方式组织，后续接实盘时不需要重写策略、
风控和复盘逻辑。
====================================================================
"""
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from config import BROKER_MODE

logger = logging.getLogger("execution.broker")


class BrokerAdapter(ABC):
    """交易通道抽象基类"""

    mode: str = "base"

    @abstractmethod
    def get_positions(self) -> Dict[str, dict]:
        """获取当前持仓"""

    @abstractmethod
    def get_cash(self) -> float:
        """获取可用现金"""

    @abstractmethod
    def total_assets(self, prices: Dict[str, float] = None) -> float:
        """获取总资产"""

    @abstractmethod
    def has_position(self, code: str) -> bool:
        """是否持有指定证券"""

    @abstractmethod
    def buy(self, code: str, name: str, price: float, shares: int,
            reason: str = "TradePlan执行", atr: float = None) -> Optional[dict]:
        """买入"""

    @abstractmethod
    def sell(self, code: str, price: float, shares: int = None,
             reason: str = "TradePlan执行") -> Optional[dict]:
        """卖出"""

    @abstractmethod
    def check_stop_conditions(self, prices: Dict[str, float],
                              atr_map: Dict[str, float] = None) -> List[dict]:
        """检查并执行止损止盈"""


class PaperBrokerAdapter(BrokerAdapter):
    """模拟盘交易通道"""

    mode = "paper"

    def __init__(self, account=None, account_file: str = None, db_path: str = None):
        from execution.paper_account import PaperAccount
        self.account = account or PaperAccount(filepath=account_file, db_path=db_path)

    def get_positions(self) -> Dict[str, dict]:
        return self.account.positions

    def get_cash(self) -> float:
        return self.account.cash

    def total_assets(self, prices: Dict[str, float] = None) -> float:
        return self.account.total_assets(prices)

    def market_value(self, prices: Dict[str, float] = None) -> float:
        """获取持仓市值"""
        return self.account.market_value(prices)

    def has_position(self, code: str) -> bool:
        return self.account.has_position(code)

    def buy(self, code: str, name: str, price: float, shares: int,
            reason: str = "TradePlan执行", atr: float = None) -> Optional[dict]:
        return self.account.buy(
            code=code,
            name=name,
            price=price,
            shares=shares,
            reason=reason,
            atr=atr,
        )

    def sell(self, code: str, price: float, shares: int = None,
             reason: str = "TradePlan执行") -> Optional[dict]:
        return self.account.sell(code=code, price=price, shares=shares, reason=reason)

    def check_stop_conditions(self, prices: Dict[str, float],
                              atr_map: Dict[str, float] = None) -> List[dict]:
        return self.account.check_stop_conditions(prices, atr_map=atr_map)


class RealBrokerAdapter(BrokerAdapter):
    """
    真实交易通道占位实现

    注意：当前项目先跑模拟盘，真实交易必须在单独完成券商权限、交易品种、
    下单回报、撤单、风控审计后再启用。
    """

    mode = "real"

    def __init__(self):
        raise NotImplementedError("真实交易适配器尚未启用，请使用 BROKER_MODE=paper")

    def get_positions(self) -> Dict[str, dict]:
        raise NotImplementedError

    def get_cash(self) -> float:
        raise NotImplementedError

    def total_assets(self, prices: Dict[str, float] = None) -> float:
        raise NotImplementedError

    def has_position(self, code: str) -> bool:
        raise NotImplementedError

    def buy(self, code: str, name: str, price: float, shares: int,
            reason: str = "TradePlan执行", atr: float = None) -> Optional[dict]:
        raise NotImplementedError

    def sell(self, code: str, price: float, shares: int = None,
             reason: str = "TradePlan执行") -> Optional[dict]:
        raise NotImplementedError

    def check_stop_conditions(self, prices: Dict[str, float],
                              atr_map: Dict[str, float] = None) -> List[dict]:
        raise NotImplementedError


def get_broker_adapter(mode: str = None) -> BrokerAdapter:
    """
    获取交易通道适配器

    默认读取 BROKER_MODE，未配置时使用模拟盘。
    """
    selected = (mode or BROKER_MODE or "paper").lower()
    if selected == "paper":
        return PaperBrokerAdapter()
    if selected == "real":
        return RealBrokerAdapter()

    logger.warning(f"未知BROKER_MODE={selected}，降级为模拟盘")
    return PaperBrokerAdapter()
