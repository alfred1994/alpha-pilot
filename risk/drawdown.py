"""
回撤控制模块
最大回撤监控、熔断机制、净值追踪
"""
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from functools import wraps
from scheduler.persistence import atomic_json_write, json_file_lock
from scheduler.market_calendar import next_trading_day
from scheduler.market_calendar import _now_bj

from config import MAX_DRAWDOWN, CIRCUIT_BREAKER_DAYS, DATA_DIR

logger = logging.getLogger("risk.drawdown")

# 熔断状态持久化文件
CIRCUIT_BREAKER_FILE = os.path.join(DATA_DIR, "circuit_breaker.json")


def _locked_mutation(func):
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        with json_file_lock(self.state_file):
            self._load_state()
            return func(self, *args, **kwargs)
    return wrapped


@dataclass
class DrawdownState:
    """回撤状态"""
    peak_value: float = 0.0          # 历史最高净值
    current_value: float = 0.0       # 当前净值
    current_drawdown: float = 0.0    # 当前回撤幅度
    max_drawdown: float = 0.0        # 最大回撤
    max_drawdown_date: str = ""      # 最大回撤日期
    is_circuit_breaker: bool = False # 是否处于熔断状态
    circuit_breaker_until: str = ""  # 熔断解除时间


class DrawdownController:
    """
    回撤控制器

    功能:
        1. 追踪净值，计算实时回撤
        2. 最大回撤达到-15%触发熔断
        3. 熔断暂停交易1天
        4. 持久化熔断状态
    """

    def __init__(
        self,
        max_drawdown: float = None,
        circuit_breaker_days: int = None,
        state_file: str = None,
    ):
        self.max_drawdown = max_drawdown if max_drawdown is not None else MAX_DRAWDOWN
        self.circuit_breaker_days = circuit_breaker_days or CIRCUIT_BREAKER_DAYS
        self.state_file = state_file or CIRCUIT_BREAKER_FILE

        self.state = DrawdownState()
        self._nav_history: List[dict] = []  # 净值历史

        self._load_state()

    def _load_state(self):
        """加载持久化的熔断状态"""
        self.state = DrawdownState()
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("状态根节点必须是对象")
                self.state.peak_value = data.get("peak_value", 0)
                self.state.current_value = data.get("current_value", 0)
                self.state.current_drawdown = data.get("current_drawdown", 0)
                self.state.max_drawdown = data.get("max_drawdown", 0)
                self.state.max_drawdown_date = data.get("max_drawdown_date", "")
                self.state.is_circuit_breaker = data.get("is_circuit_breaker", False)
                self.state.circuit_breaker_until = data.get("circuit_breaker_until", "")
                logger.info(
                    f"加载回撤状态: 最大回撤={self.state.max_drawdown:.2%} "
                    f"熔断={self.state.is_circuit_breaker}"
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.error(f"熔断状态文件损坏: {e}")
                raise RuntimeError("熔断状态损坏，禁止继续交易") from e

    def _save_state(self):
        """持久化熔断状态"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        data = {
            "peak_value": self.state.peak_value,
            "current_value": self.state.current_value,
            "current_drawdown": self.state.current_drawdown,
            "max_drawdown": self.state.max_drawdown,
            "max_drawdown_date": self.state.max_drawdown_date,
            "is_circuit_breaker": self.state.is_circuit_breaker,
            "circuit_breaker_until": self.state.circuit_breaker_until,
            "updated_at": datetime.now().isoformat(),
        }
        atomic_json_write(self.state_file, data)

    @_locked_mutation
    def update(self, total_assets: float, date: str = None) -> dict:
        """
        更新净值，计算回撤

        Args:
            total_assets: 当前总资产
            date: 日期字符串（默认今天）

        Returns:
            {
                current_drawdown: float,
                max_drawdown: float,
                peak_value: float,
                is_circuit_breaker: bool,
                trigger_breaker: bool,  # 本次是否新触发熔断
            }
        """
        if date is None:
            date = _now_bj().strftime("%Y-%m-%d")

        self.state.current_value = total_assets

        # 更新历史最高
        if total_assets > self.state.peak_value:
            self.state.peak_value = total_assets

        # 计算当前回撤
        if self.state.peak_value > 0:
            self.state.current_drawdown = (
                (total_assets - self.state.peak_value) / self.state.peak_value
            )
        else:
            self.state.current_drawdown = 0

        # 更新历史最深回撤；熔断是否仍有效独立于最大回撤纪录。
        trigger_breaker = False
        if self.state.current_drawdown < self.state.max_drawdown:
            self.state.max_drawdown = self.state.current_drawdown
            self.state.max_drawdown_date = date
        if self.state.current_drawdown <= self.max_drawdown and not self.state.is_circuit_breaker:
            trigger_breaker = True
            self._trigger_circuit_breaker(date)

        # 记录净值
        self._nav_history.append({
            "date": date,
            "total_assets": total_assets,
            "drawdown": self.state.current_drawdown,
            "peak": self.state.peak_value,
        })

        self._save_state()

        return {
            "current_drawdown": self.state.current_drawdown,
            "max_drawdown": self.state.max_drawdown,
            "peak_value": self.state.peak_value,
            "is_circuit_breaker": self.state.is_circuit_breaker,
            "trigger_breaker": trigger_breaker,
        }

    def _trigger_circuit_breaker(self, date: str):
        """触发熔断"""
        self.state.is_circuit_breaker = True
        # 计算熔断解除时间
        # until 是排他的恢复日期；触发后的 N 个交易日均禁止交易。
        resume = next_trading_day(date, n=self.circuit_breaker_days + 1)
        self.state.circuit_breaker_until = f"{resume[:4]}-{resume[4:6]}-{resume[6:8]}"
        logger.warning(
            f"!!! 熔断触发 !!! 回撤={self.state.current_drawdown:.2%} "
            f"超过阈值{self.max_drawdown:.2%}，"
            f"暂停交易至 {self.state.circuit_breaker_until}"
        )

    def is_trading_allowed(self, today: str = None) -> dict:
        """
        检查是否允许交易

        Args:
            today: 今天日期（默认自动获取）

        Returns:
            {allowed: bool, reason: str}
        """
        if today is None:
            today = _now_bj().strftime("%Y-%m-%d")
        self._load_state()

        if not self.state.is_circuit_breaker:
            if (self.state.current_drawdown <= self.max_drawdown
                    and self.state.current_value > 0):
                return {"allowed": False, "reason": "历史回撤超阈值，禁止交易"}
            return {"allowed": True, "reason": "正常"}

        # 检查熔断是否到期
        if today >= self.state.circuit_breaker_until:
            if (self.state.current_drawdown <= self.max_drawdown
                    and self.state.current_value > 0):
                return {
                    "allowed": False,
                    "reason": "历史回撤仍超阈值，需净值恢复或人工复核后恢复交易",
                }
            return {"allowed": True, "reason": "熔断解除"}

        return {
            "allowed": False,
            "reason": (
                f"熔断中: 回撤{self.state.current_drawdown:.2%}，"
                f"暂停至{self.state.circuit_breaker_until}"
            ),
        }

    def preview(self, total_assets: float, date: str = None) -> dict:
        """
        只读推演：按给定净值算出当前回撤/预警级别，供状态展示使用。

        与 update() 的区别是绝不落盘——"查看风控状态"不应改写峰值或最大回撤，
        否则一次误算（例如临时账户的 INITIAL_CAPITAL）就会永久污染熔断基线。
        """
        if date is None:
            date = _now_bj().strftime("%Y-%m-%d")
        peak = max(self.state.peak_value, total_assets)
        drawdown = (total_assets - peak) / peak if peak > 0 else 0.0
        threshold = abs(self.max_drawdown)
        if self.state.is_circuit_breaker and date < self.state.circuit_breaker_until:
            level = "breaker"
        elif abs(drawdown) >= threshold * 0.8:
            level = "danger"
        elif abs(drawdown) >= threshold * 0.5:
            level = "warning"
        else:
            level = "normal"
        return {
            "current_drawdown": drawdown,
            "max_drawdown": self.state.max_drawdown,
            "peak_value": peak,
            "is_circuit_breaker": self.state.is_circuit_breaker,
            "warning_level": level,
        }

    def get_warning_level(self) -> str:
        """
        获取风险预警级别

        Returns:
            "normal" / "warning" / "danger" / "breaker"
        """
        dd = abs(self.state.current_drawdown)

        if (self.state.is_circuit_breaker
                or (self.state.current_drawdown <= self.max_drawdown
                    and self.state.current_value > 0)):
            return "breaker"
        elif dd >= abs(self.max_drawdown) * 0.8:  # 接近熔断（80%阈值）
            return "danger"
        elif dd >= abs(self.max_drawdown) * 0.5:  # 中等风险（50%阈值）
            return "warning"
        else:
            return "normal"

    def reset(self):
        """重置回撤状态"""
        with json_file_lock(self.state_file):
            self.state = DrawdownState()
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
            self._nav_history.clear()
        logger.info("回撤状态已重置")

    def get_nav_history(self) -> List[dict]:
        """获取净值历史"""
        return self._nav_history.copy()

    def format_status(self, total_assets: float = None) -> str:
        """格式化回撤状态"""
        warning = self.get_warning_level()
        warning_label = {
            "normal": "正常",
            "warning": "黄色预警",
            "danger": "红色预警",
            "breaker": "熔断中",
        }.get(warning, warning)

        lines = [
            "=" * 55,
            "回撤控制",
            "=" * 55,
            f"历史最高:   {self.state.peak_value:>14,.0f}",
            f"当前净值:   {self.state.current_value:>14,.0f}",
            f"当前回撤:   {self.state.current_drawdown:>+13.2%}",
            f"最大回撤:   {self.state.max_drawdown:>+13.2%}",
            f"最大回撤日: {self.state.max_drawdown_date}",
            f"熔断阈值:   {self.max_drawdown:>+13.2%}",
            f"风险级别:   {warning_label}",
        ]

        if self.state.is_circuit_breaker:
            lines.append(f"熔断状态:   暂停交易至 {self.state.circuit_breaker_until}")
        else:
            lines.append("熔断状态:   未触发")

        lines.append("=" * 55)
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="alphapilot-drawdown-demo-") as directory:
        dc = DrawdownController(state_file=os.path.join(directory, "drawdown.json"))
        test_values = [1_000_000, 980_000, 950_000, 920_000, 880_000, 840_000]
        for i, value in enumerate(test_values):
            result = dc.update(value, f"2024-01-{i+1:02d}")
            print(f"日期=2024-01-{i+1:02d} 净值={value:>12,} "
                  f"回撤={result['current_drawdown']:+.2%} "
                  f"熔断={result['is_circuit_breaker']}")
        print()
        print(dc.format_status())
