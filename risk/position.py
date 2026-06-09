"""
仓位管理模块
单股上限、持仓数量控制、行业分散、资金分配
"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from config import (
    INITIAL_CAPITAL,
    MAX_POSITIONS,
    MAX_SINGLE_PCT,
    MIN_TRADE_UNIT,
)

logger = logging.getLogger("risk.position")


@dataclass
class PositionInfo:
    """持仓信息"""
    code: str
    name: str
    shares: int
    buy_price: float
    cost: float
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    weight: float = 0.0  # 占总资产比例
    industry: str = ""


class PositionManager:
    """
    仓位管理器

    规则:
        - 最大持仓数: MAX_POSITIONS (5)
        - 单只上限: MAX_SINGLE_PCT (20%)
        - 行业分散: 同一行业不超过2只
        - 新建仓位不超过总资产的单只上限
    """

    def __init__(
        self,
        max_positions: int = None,
        max_single_pct: float = None,
        max_industry_count: int = 2,
    ):
        self.max_positions = max_positions or MAX_POSITIONS
        self.max_single_pct = max_single_pct or MAX_SINGLE_PCT
        self.max_industry_count = max_industry_count

    def can_open_position(
        self,
        current_positions: Dict[str, dict],
        total_assets: float,
    ) -> bool:
        """是否可以开新仓"""
        return len(current_positions) < self.max_positions

    def check_position_limit(
        self,
        code: str,
        current_positions: Dict[str, dict],
        total_assets: float,
    ) -> dict:
        """
        检查单股仓位限制

        Returns:
            {allowed: bool, max_amount: float, reason: str}
        """
        if code in current_positions:
            return {
                "allowed": False,
                "max_amount": 0,
                "reason": f"已持有 {code}，不重复开仓",
            }

        if len(current_positions) >= self.max_positions:
            return {
                "allowed": False,
                "max_amount": 0,
                "reason": f"持仓已满({len(current_positions)}/{self.max_positions})",
            }

        max_amount = total_assets * self.max_single_pct
        return {
            "allowed": True,
            "max_amount": max_amount,
            "reason": f"可买入，上限 {max_amount:,.0f}",
        }

    def check_industry_limit(
        self,
        industry: str,
        current_positions: Dict[str, dict],
        industry_map: Dict[str, str],
    ) -> dict:
        """
        检查行业集中度

        Args:
            industry: 要买入的行业
            current_positions: 当前持仓
            industry_map: {code: industry} 行业映射

        Returns:
            {allowed: bool, current_count: int, reason: str}
        """
        count = 0
        for code in current_positions:
            if industry_map.get(code) == industry:
                count += 1

        if count >= self.max_industry_count:
            return {
                "allowed": False,
                "current_count": count,
                "reason": f"行业[{industry}]已持有{count}只，上限{self.max_industry_count}",
            }

        return {
            "allowed": True,
            "current_count": count,
            "reason": f"行业[{industry}]持有{count}/{self.max_industry_count}",
        }

    def calc_buy_shares(
        self,
        price: float,
        total_assets: float,
        cash: float,
        max_amount: float = None,
    ) -> dict:
        """
        计算可买股数

        Args:
            price: 股价
            total_assets: 总资产
            cash: 可用现金
            max_amount: 最大买入金额（None则按单只上限）

        Returns:
            {shares: int, amount: float, commission: float}
        """
        if max_amount is None:
            max_amount = total_assets * self.max_single_pct

        # 取上限和可用现金的较小值
        available = min(max_amount, cash)
        if available <= 0:
            return {"shares": 0, "amount": 0, "commission": 0}

        shares = int(available / price / MIN_TRADE_UNIT) * MIN_TRADE_UNIT
        if shares <= 0:
            return {"shares": 0, "amount": 0, "commission": 0}

        amount = shares * price
        commission = max(amount * 0.0003, 5)  # 佣金万三，最低5元
        return {"shares": shares, "amount": amount, "commission": commission}

    def get_position_infos(
        self,
        positions: Dict[str, dict],
        prices: Dict[str, float],
        total_assets: float,
    ) -> List[PositionInfo]:
        """
        获取所有持仓的详细信息

        Args:
            positions: 账户持仓 {code: pos_dict}
            prices: {code: current_price}
            total_assets: 总资产

        Returns:
            PositionInfo列表
        """
        infos = []
        for code, pos in positions.items():
            current = prices.get(code, pos["buy_price"])
            shares = pos["shares"]
            cost = pos.get("cost", pos["buy_price"] * shares)
            market_value = current * shares
            pnl = market_value - cost
            pnl_pct = (current - pos["buy_price"]) / pos["buy_price"]
            weight = market_value / total_assets if total_assets > 0 else 0

            infos.append(PositionInfo(
                code=code,
                name=pos.get("name", code),
                shares=shares,
                buy_price=pos["buy_price"],
                cost=cost,
                current_price=current,
                market_value=market_value,
                pnl=pnl,
                pnl_pct=pnl_pct,
                weight=weight,
            ))

        return infos

    def suggest_position_size(
        self,
        score: float,
        total_assets: float,
        price: float,
    ) -> dict:
        """
        根据信号分数建议仓位大小

        分数越高，建议仓位越大（但不超过上限）

        Args:
            score: 综合信号分 0-100
            total_assets: 总资产
            price: 股价

        Returns:
            {ratio: float, amount: float, shares: int}
        """
        # 分数60以下不开仓
        if score < 60:
            return {"ratio": 0, "amount": 0, "shares": 0}

        # 60-100 映射到 10%-20% 仓位
        ratio = 0.10 + (score - 60) / 40 * 0.10
        ratio = min(ratio, self.max_single_pct)

        amount = total_assets * ratio
        shares = int(amount / price / MIN_TRADE_UNIT) * MIN_TRADE_UNIT
        actual_amount = shares * price

        return {
            "ratio": ratio,
            "amount": actual_amount,
            "shares": shares,
        }

    def format_position_summary(
        self,
        positions: Dict[str, dict],
        prices: Dict[str, float],
        total_assets: float,
        cash: float,
    ) -> str:
        """格式化仓位汇总"""
        infos = self.get_position_infos(positions, prices, total_assets)

        lines = [
            "=" * 65,
            "仓位管理",
            "=" * 65,
            f"总资产: {total_assets:>14,.0f}  现金: {cash:>14,.0f}  持仓: {len(positions)}/{self.max_positions}",
            "-" * 65,
            f"{'代码':<8} {'名称':<8} {'股数':>6} {'成本价':>8} {'现价':>8} "
            f"{'盈亏%':>8} {'市值':>12} {'仓位%':>6}",
            "-" * 65,
        ]

        for info in infos:
            lines.append(
                f"{info.code:<8} {info.name:<8} {info.shares:>6} "
                f"{info.buy_price:>8.2f} {info.current_price:>8.2f} "
                f"{info.pnl_pct:>+7.1%} {info.market_value:>12,.0f} {info.weight:>5.1%}"
            )

        lines.append("=" * 65)
        return "\n".join(lines)
