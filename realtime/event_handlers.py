"""
事件处理器 - 业务逻辑
"""
import asyncio
import logging
from datetime import datetime
from realtime.event_bus import Event
from data.quote_validation import validate_quote

logger = logging.getLogger("realtime.handlers")


class StopLossHandler:
    """实时止损处理器"""

    def __init__(self, account):
        self.account = account
        self._checking = set()  # 防止重复检查

    async def on_quote_update(self, event: Event):
        """行情更新事件"""
        code = event.data.get("code")
        validation = validate_quote(event.data, expected_code=str(code or ""))
        price = validation.price

        if not code or not validation.valid or code in self._checking:
            if code and not validation.valid:
                logger.debug("忽略不安全实时止损行情 %s: %s", code, validation.reason)
            return

        # 检查是否持仓
        positions = self.account.positions
        if code not in positions:
            return

        self._checking.add(code)
        try:
            # 实时传感器只能评估并广播；成交必须经过自动执行链路。
            evaluate = getattr(self.account, "evaluate_stop_conditions", None)
            if not callable(evaluate):
                logger.error("实时止损传感器拒绝运行：账户未提供纯判断 evaluate_stop_conditions")
                return
            signals = evaluate({code: price})

            for signal in signals:
                # PaperAccount 的纯评估契约使用 type；兼容旧账户适配器的 action。
                action = signal.get("type") or signal.get("action")
                if action in ("stop_loss", "take_profit", "trailing_stop"):
                    pos = positions[code]
                    shares = pos.get("shares", 0)
                    reason = signal.get("reason", action)

                    logger.warning(f"触发止损建议: {code} {shares}股@{price} {reason}。实时传感器广播，不直接执行交易。")
                    from scheduler.notifier import send_message
                    send_message(f"🚨【实时传感器】股票 {code} 触发 {reason} 建议！当前价格: {price}。建议卖出。")

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
