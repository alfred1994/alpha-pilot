"""
止损止盈模块
支持三种模式: 固定比例、ATR动态止损、移动止损
"""
import logging
from enum import Enum
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from config import STOP_LOSS, TAKE_PROFIT, TRAILING_STOP

logger = logging.getLogger("risk.stop_loss")


class StopType(Enum):
    """止损类型"""
    FIXED = "固定止损"
    ATR = "ATR动态止损"
    TRAILING = "移动止损"
    TAKE_PROFIT = "止盈"


@dataclass
class StopSignal:
    """止损/止盈信号"""
    code: str
    name: str
    stop_type: StopType
    current_price: float
    buy_price: float
    trigger_price: float
    pnl_pct: float
    reason: str


class StopLossManager:
    """
    止损止盈管理器

    三种模式:
    1. 固定止损: 买入价 * (1 + STOP_LOSS) = -5%止损
    2. ATR动态止损: 买入价 - N倍ATR，随波动率自适应
    3. 移动止损: 最高价 * (1 + TRAILING_STOP) = 最高回撤-3%

    止盈: 买入价 * (1 + TAKE_PROFIT) = +10%止盈
    """

    def __init__(
        self,
        stop_loss: float = None,
        take_profit: float = None,
        trailing_stop: float = None,
        atr_multiplier: float = 2.0,
        use_atr: bool = True,
    ):
        self.stop_loss = stop_loss if stop_loss is not None else STOP_LOSS
        self.take_profit = take_profit if take_profit is not None else TAKE_PROFIT
        self.trailing_stop = trailing_stop if trailing_stop is not None else TRAILING_STOP
        self.atr_multiplier = atr_multiplier
        self.use_atr = use_atr

    def calc_fixed_stop_price(self, buy_price: float) -> float:
        """计算固定止损价"""
        return buy_price * (1 + self.stop_loss)

    def calc_take_profit_price(self, buy_price: float) -> float:
        """计算止盈价"""
        return buy_price * (1 + self.take_profit)

    def calc_trailing_stop_price(self, highest_price: float) -> float:
        """计算移动止损价"""
        return highest_price * (1 + self.trailing_stop)

    def calc_atr_stop_price(
        self,
        buy_price: float,
        atr: float,
    ) -> float:
        """
        计算ATR动态止损价

        Args:
            buy_price: 买入价
            atr: Average True Range

        Returns:
            止损价 = 买入价 - ATR * 倍数
        """
        return buy_price - atr * self.atr_multiplier

    def check_stop(
        self,
        code: str,
        name: str,
        buy_price: float,
        current_price: float,
        highest_price: float,
        atr: float = None,
    ) -> Optional[StopSignal]:
        """
        检查是否触发止损/止盈

        优先级: 止损 > 移动止损 > 止盈
        ATR止损和固定止损取更紧的那个

        Args:
            code: 股票代码
            name: 股票名称
            buy_price: 买入价
            current_price: 当前价
            highest_price: 持仓以来最高价
            atr: ATR值（可选，用于动态止损）

        Returns:
            StopSignal 或 None
        """
        pnl_pct = (current_price - buy_price) / buy_price

        # 1. 检查止损
        stop_price = self.calc_fixed_stop_price(buy_price)

        # 如果有ATR，取更紧的止损价
        if self.use_atr and atr and atr > 0:
            atr_stop = self.calc_atr_stop_price(buy_price, atr)
            stop_price = max(stop_price, atr_stop)  # 更高的止损价 = 更紧的止损

        if current_price <= stop_price:
            return StopSignal(
                code=code,
                name=name,
                stop_type=StopType.ATR if (self.use_atr and atr) else StopType.FIXED,
                current_price=current_price,
                buy_price=buy_price,
                trigger_price=stop_price,
                pnl_pct=pnl_pct,
                reason=f"止损触发({pnl_pct:+.1%}) 止损价={stop_price:.2f}",
            )

        # 2. 检查移动止损
        trailing_price = self.calc_trailing_stop_price(highest_price)
        if current_price <= trailing_price and highest_price > buy_price:
            from_peak = (current_price - highest_price) / highest_price
            return StopSignal(
                code=code,
                name=name,
                stop_type=StopType.TRAILING,
                current_price=current_price,
                buy_price=buy_price,
                trigger_price=trailing_price,
                pnl_pct=pnl_pct,
                reason=f"移动止损({from_peak:+.1%} from peak) 最高={highest_price:.2f}",
            )

        # 3. 检查止盈
        tp_price = self.calc_take_profit_price(buy_price)
        if current_price >= tp_price:
            return StopSignal(
                code=code,
                name=name,
                stop_type=StopType.TAKE_PROFIT,
                current_price=current_price,
                buy_price=buy_price,
                trigger_price=tp_price,
                pnl_pct=pnl_pct,
                reason=f"止盈触发({pnl_pct:+.1%}) 止盈价={tp_price:.2f}",
            )

        return None

    def check_all_positions(
        self,
        positions: Dict[str, dict],
        prices: Dict[str, float],
        atr_map: Dict[str, float] = None,
    ) -> List[StopSignal]:
        """
        批量检查所有持仓的止损止盈

        Args:
            positions: {code: pos_dict} 持仓
            prices: {code: current_price} 最新价
            atr_map: {code: atr} ATR值（可选）

        Returns:
            触发的止损/止盈信号列表
        """
        signals = []
        atr_map = atr_map or {}

        for code, pos in positions.items():
            current = prices.get(code)
            if current is None:
                continue

            highest = pos.get("highest_price", pos["buy_price"])
            atr = atr_map.get(code)

            signal = self.check_stop(
                code=code,
                name=pos.get("name", code),
                buy_price=pos["buy_price"],
                current_price=current,
                highest_price=highest,
                atr=atr,
            )
            if signal:
                signals.append(signal)

        return signals

    def update_highest_price(
        self,
        positions: Dict[str, dict],
        prices: Dict[str, float],
    ) -> List[str]:
        """
        更新持仓最高价

        Args:
            positions: 持仓（会原地修改）
            prices: 最新价

        Returns:
            更新了最高价的股票代码列表
        """
        updated = []
        for code, pos in positions.items():
            current = prices.get(code)
            if current and current > pos.get("highest_price", 0):
                pos["highest_price"] = current
                updated.append(code)
        return updated

    def get_stop_levels(
        self,
        buy_price: float,
        highest_price: float,
        atr: float = None,
    ) -> dict:
        """
        获取所有止损止盈价位（用于前端展示）

        Returns:
            {
                fixed_stop: float,
                trailing_stop: float,
                atr_stop: float or None,
                take_profit: float,
            }
        """
        levels = {
            "fixed_stop": self.calc_fixed_stop_price(buy_price),
            "trailing_stop": self.calc_trailing_stop_price(highest_price),
            "take_profit": self.calc_take_profit_price(buy_price),
        }
        if atr and atr > 0:
            levels["atr_stop"] = self.calc_atr_stop_price(buy_price, atr)
        else:
            levels["atr_stop"] = None

        return levels

    def format_stop_summary(
        self,
        positions: Dict[str, dict],
        prices: Dict[str, float],
        atr_map: Dict[str, float] = None,
    ) -> str:
        """格式化止损止盈概览"""
        lines = [
            "=" * 70,
            "止损止盈监控",
            "=" * 70,
            f"{'代码':<8} {'名称':<8} {'买入价':>8} {'现价':>8} "
            f"{'止损价':>8} {'移动止损':>8} {'止盈价':>8} {'盈亏%':>8}",
            "-" * 70,
        ]

        atr_map = atr_map or {}
        for code, pos in positions.items():
            current = prices.get(code, pos["buy_price"])
            highest = pos.get("highest_price", pos["buy_price"])
            levels = self.get_stop_levels(
                pos["buy_price"], highest, atr_map.get(code)
            )
            pnl_pct = (current - pos["buy_price"]) / pos["buy_price"]

            lines.append(
                f"{code:<8} {pos.get('name', code):<8} "
                f"{pos['buy_price']:>8.2f} {current:>8.2f} "
                f"{levels['fixed_stop']:>8.2f} {levels['trailing_stop']:>8.2f} "
                f"{levels['take_profit']:>8.2f} {pnl_pct:>+7.1%}"
            )

        lines.append("=" * 70)
        return "\n".join(lines)
