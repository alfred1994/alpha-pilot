"""
事件处理器 - 业务逻辑
"""
import asyncio
import logging
from datetime import datetime
from realtime.event_bus import Event

logger = logging.getLogger("realtime.handlers")


class StopLossHandler:
    """实时止损处理器"""

    def __init__(self, account):
        self.account = account
        self._checking = set()  # 防止重复检查

    async def on_quote_update(self, event: Event):
        """行情更新事件"""
        code = event.data.get("code")
        price = event.data.get("price")

        if not code or not price or code in self._checking:
            return

        # 检查是否持仓
        positions = self.account.positions
        if code not in positions:
            return

        self._checking.add(code)
        try:
            # 止损检查
            signals = self.account.check_stop_conditions({code: price})

            for signal in signals:
                action = signal.get("action")
                if action in ("stop_loss", "take_profit", "trailing_stop"):
                    pos = positions[code]
                    shares = pos.get("shares", 0)
                    reason = signal.get("reason", action)

                    logger.warning(f"触发止损: {code} {shares}股@{price} {reason}")
                    self.account.sell(code, price, shares, reason=reason)

        except Exception as e:
            logger.error(f"止损检查失败 {code}: {e}")
        finally:
            self._checking.discard(code)


class MarketEventHandler:
    """市场事件处理器"""

    def __init__(self):
        self._triggered = {}

    async def on_index_change(self, event: Event):
        """指数异动事件"""
        index = event.data.get("index")
        change_pct = event.data.get("change_pct")

        if abs(change_pct) > 0.02:  # 大盘涨跌>2%
            key = f"index_{datetime.now().strftime('%Y%m%d%H')}"
            if key in self._triggered:
                return

            self._triggered[key] = True
            logger.warning(f"指数异动: {index} {change_pct:+.2%}，触发扫描")

            # 触发扫描
            from scheduler.pipeline import run_scan
            try:
                asyncio.create_task(asyncio.to_thread(run_scan))
            except Exception as e:
                logger.error(f"触发扫描失败: {e}")

    async def on_position_risk(self, event: Event):
        """持仓风险事件"""
        code = event.data.get("code")
        risk_level = event.data.get("risk_level")

        if risk_level == "critical":
            logger.error(f"持仓风险告警: {code} {event.data}")
            # TODO: 触发风控动作
