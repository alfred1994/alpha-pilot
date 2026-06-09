"""
系统级风控模块
单日亏损熔断、连续亏损降仓、系统异常停机
"""
import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from config import (
    DAILY_LOSS_LIMIT,
    CONSECUTIVE_LOSS_DAYS,
    POSITION_REDUCE_RATIO,
    DATA_DIR,
)

logger = logging.getLogger("risk.system_risk")

# 系统风控状态持久化文件
SYSTEM_RISK_FILE = os.path.join(DATA_DIR, "system_risk.json")


@dataclass
class SystemRiskState:
    """系统风控状态"""
    # 每日资产记录: [{"date": "2024-01-01", "total_assets": 1000000}, ...]
    daily_records: List[dict] = field(default_factory=list)
    # 昨日是否触发单日亏损熔断（次日禁止开新仓）
    forbid_new_buy: bool = False
    forbid_new_buy_reason: str = ""
    # 连续亏损天数
    consecutive_loss_days: int = 0
    # 是否处于降仓模式
    reduce_position: bool = False
    reduce_position_ratio: float = 1.0
    # 系统是否紧急停机
    system_halted: bool = False
    halt_reason: str = ""
    halt_time: str = ""


class SystemRiskController:
    """
    系统级风控控制器

    功能:
        1. 单日亏损>5%: 次日不开新仓
        2. 连续3日亏损: 降低仓位50%
        3. 系统异常: 立即停止自动交易
        4. 状态持久化到 data/system_risk.json
    """

    def __init__(
        self,
        daily_loss_limit: float = None,
        consecutive_loss_days: int = None,
        position_reduce_ratio: float = None,
        state_file: str = None,
    ):
        self.daily_loss_limit = daily_loss_limit if daily_loss_limit is not None else DAILY_LOSS_LIMIT
        self.consecutive_loss_days_threshold = consecutive_loss_days or CONSECUTIVE_LOSS_DAYS
        self.position_reduce_ratio = position_reduce_ratio or POSITION_REDUCE_RATIO
        self.state_file = state_file or SYSTEM_RISK_FILE

        self.state = SystemRiskState()

        self._load_state()

    def _load_state(self):
        """加载持久化的风控状态"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.state.daily_records = data.get("daily_records", [])
                self.state.forbid_new_buy = data.get("forbid_new_buy", False)
                self.state.forbid_new_buy_reason = data.get("forbid_new_buy_reason", "")
                self.state.consecutive_loss_days = data.get("consecutive_loss_days", 0)
                self.state.reduce_position = data.get("reduce_position", False)
                self.state.reduce_position_ratio = data.get("reduce_position_ratio", 1.0)
                self.state.system_halted = data.get("system_halted", False)
                self.state.halt_reason = data.get("halt_reason", "")
                self.state.halt_time = data.get("halt_time", "")
                logger.info(
                    f"加载系统风控状态: 禁止开新仓={self.state.forbid_new_buy} "
                    f"连续亏损={self.state.consecutive_loss_days}天 "
                    f"降仓={self.state.reduce_position} "
                    f"系统停机={self.state.system_halted}"
                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"系统风控状态文件损坏: {e}")

    def _save_state(self):
        """持久化风控状态"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        data = {
            "daily_records": self.state.daily_records,
            "forbid_new_buy": self.state.forbid_new_buy,
            "forbid_new_buy_reason": self.state.forbid_new_buy_reason,
            "consecutive_loss_days": self.state.consecutive_loss_days,
            "reduce_position": self.state.reduce_position,
            "reduce_position_ratio": self.state.reduce_position_ratio,
            "system_halted": self.state.system_halted,
            "halt_reason": self.state.halt_reason,
            "halt_time": self.state.halt_time,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def update(self, total_assets: float, date: str = None) -> dict:
        """
        每日更新：传入当日总资产，计算盈亏并更新风控状态

        Args:
            total_assets: 当日收盘后总资产
            date: 日期字符串（默认今天）

        Returns:
            {
                daily_pnl: float,          # 当日盈亏比例
                consecutive_loss: int,      # 连续亏损天数
                forbid_new_buy: bool,       # 是否禁止开新仓（明日生效）
                reduce_position: bool,      # 是否降仓
                system_halted: bool,        # 系统是否停机
            }
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 计算当日盈亏
        daily_pnl = 0.0
        if self.state.daily_records:
            prev_assets = self.state.daily_records[-1]["total_assets"]
            if prev_assets > 0:
                daily_pnl = (total_assets - prev_assets) / prev_assets

        # 记录当日资产
        self.state.daily_records.append({
            "date": date,
            "total_assets": total_assets,
            "daily_pnl": daily_pnl,
        })

        # 重置昨日的禁止开新仓标记（次日生效后需清除）
        # 注意：这里不立即清除，由 is_new_buy_allowed 判断日期

        # --- 规则1: 单日亏损超过阈值，次日禁止开新仓 ---
        if daily_pnl <= self.daily_loss_limit:
            self.state.forbid_new_buy = True
            self.state.forbid_new_buy_reason = (
                f"单日亏损{daily_pnl:+.2%}，超过阈值{self.daily_loss_limit:+.2%}，"
                f"次日({date})禁止开新仓"
            )
            logger.warning(f"!!! 单日亏损熔断 !!! {self.state.forbid_new_buy_reason}")

        # --- 规则2: 连续亏损统计 ---
        if daily_pnl < 0:
            self.state.consecutive_loss_days += 1
        else:
            self.state.consecutive_loss_days = 0

        # 连续亏损达到阈值，触发降仓
        if self.state.consecutive_loss_days >= self.consecutive_loss_days_threshold:
            self.state.reduce_position = True
            self.state.reduce_position_ratio = self.position_reduce_ratio
            logger.warning(
                f"!!! 连续亏损降仓 !!! 连续{self.state.consecutive_loss_days}日亏损，"
                f"仓位降至{self.state.reduce_position_ratio:.0%}"
            )
        else:
            self.state.reduce_position = False
            self.state.reduce_position_ratio = 1.0

        self._save_state()

        return {
            "daily_pnl": daily_pnl,
            "consecutive_loss": self.state.consecutive_loss_days,
            "forbid_new_buy": self.state.forbid_new_buy,
            "reduce_position": self.state.reduce_position,
            "system_halted": self.state.system_halted,
        }

    def is_new_buy_allowed(self, today: str = None) -> dict:
        """
        检查是否允许新开仓

        Args:
            today: 今天日期（默认自动获取）

        Returns:
            {
                allowed: bool,
                reason: str,
            }
        """
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")

        # 系统停机 → 禁止一切
        if self.state.system_halted:
            return {
                "allowed": False,
                "reason": f"系统已停机: {self.state.halt_reason}",
            }

        # 检查单日亏损熔断（次日生效）
        if self.state.forbid_new_buy:
            # 如果今天是新一天，清除昨日的禁止标记（已生效过）
            if self.state.daily_records:
                last_date = self.state.daily_records[-1]["date"]
                if today > last_date:
                    # 进入新的一天，禁止标记已生效，现在清除
                    self.state.forbid_new_buy = False
                    self.state.forbid_new_buy_reason = ""
                    self._save_state()
                else:
                    return {
                        "allowed": False,
                        "reason": self.state.forbid_new_buy_reason,
                    }
            else:
                return {
                    "allowed": False,
                    "reason": self.state.forbid_new_buy_reason,
                }

        return {"allowed": True, "reason": "正常"}

    def get_position_scale(self) -> float:
        """
        返回仓位缩放系数

        Returns:
            1.0 = 正常仓位
            0.5 = 降仓模式（连续亏损触发）
            0.0 = 系统停机（不开任何仓）
        """
        if self.state.system_halted:
            return 0.0
        if self.state.reduce_position:
            return self.state.reduce_position_ratio
        return 1.0

    def check_system_health(self) -> dict:
        """
        系统健康检查

        Returns:
            {
                healthy: bool,
                status: str,           # "normal" / "warning" / "danger" / "halted"
                issues: List[str],     # 问题列表
                consecutive_loss: int,
                forbid_new_buy: bool,
                reduce_position: bool,
                system_halted: bool,
                position_scale: float,
            }
        """
        issues = []

        if self.state.system_halted:
            issues.append(f"系统已停机: {self.state.halt_reason}")

        if self.state.forbid_new_buy:
            issues.append(f"禁止开新仓: {self.state.forbid_new_buy_reason}")

        if self.state.reduce_position:
            issues.append(
                f"连续亏损{self.state.consecutive_loss_days}天，"
                f"仓位降至{self.state.reduce_position_ratio:.0%}"
            )

        # 判断状态级别
        if self.state.system_halted:
            status = "halted"
        elif self.state.reduce_position:
            status = "danger"
        elif self.state.forbid_new_buy:
            status = "warning"
        else:
            status = "normal"

        healthy = len(issues) == 0

        return {
            "healthy": healthy,
            "status": status,
            "issues": issues,
            "consecutive_loss": self.state.consecutive_loss_days,
            "forbid_new_buy": self.state.forbid_new_buy,
            "reduce_position": self.state.reduce_position,
            "system_halted": self.state.system_halted,
            "position_scale": self.get_position_scale(),
        }

    def trigger_system_halt(self, reason: str):
        """
        紧急停机：立即停止自动交易

        Args:
            reason: 停机原因
        """
        self.state.system_halted = True
        self.state.halt_reason = reason
        self.state.halt_time = datetime.now().isoformat()
        self._save_state()
        logger.critical(f"!!! 系统紧急停机 !!! 原因: {reason}")

    def resume_system(self):
        """恢复系统运行（手动解除停机）"""
        self.state.system_halted = False
        self.state.halt_reason = ""
        self.state.halt_time = ""
        self._save_state()
        logger.info("系统已恢复运行")

    def reset(self):
        """重置所有风控状态"""
        self.state = SystemRiskState()
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        logger.info("系统风控状态已重置")

    def format_status(self) -> str:
        """格式化系统风控状态"""
        health = self.check_system_health()
        status_label = {
            "normal": "正常",
            "warning": "黄色预警",
            "danger": "红色预警",
            "halted": "系统停机",
        }.get(health["status"], health["status"])

        # 最近5日盈亏
        recent = self.state.daily_records[-5:] if self.state.daily_records else []
        recent_lines = []
        for r in recent:
            pnl_str = f"{r.get('daily_pnl', 0):+.2%}" if "daily_pnl" in r else "N/A"
            recent_lines.append(f"  {r['date']}  资产={r['total_assets']:>14,.0f}  盈亏={pnl_str}")

        lines = [
            "=" * 55,
            "系统级风控",
            "=" * 55,
            f"单日亏损阈值: {self.daily_loss_limit:+.2%}",
            f"连续亏损阈值: {self.consecutive_loss_days_threshold}天",
            f"降仓缩放系数: {self.position_reduce_ratio:.0%}",
            "-" * 55,
            f"系统状态:     {status_label}",
            f"禁止开新仓:   {'是' if self.state.forbid_new_buy else '否'}",
            f"连续亏损天数: {self.state.consecutive_loss_days}天",
            f"降仓模式:     {'是' if self.state.reduce_position else '否'}",
            f"仓位缩放:     {self.get_position_scale():.0%}",
            f"系统停机:     {'是' if self.state.system_halted else '否'}",
        ]

        if self.state.system_halted:
            lines.append(f"停机原因:     {self.state.halt_reason}")
            lines.append(f"停机时间:     {self.state.halt_time}")

        if recent_lines:
            lines.append("-" * 55)
            lines.append("最近交易日:")
            lines.extend(recent_lines)

        if health["issues"]:
            lines.append("-" * 55)
            lines.append("风控告警:")
            for issue in health["issues"]:
                lines.append(f"  ⚠ {issue}")

        lines.append("=" * 55)
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # 测试用：临时文件
    import tempfile
    test_file = os.path.join(tempfile.gettempdir(), "test_system_risk.json")

    src = SystemRiskController(state_file=test_file)

    # 模拟连续亏损
    test_assets = [1_000_000, 960_000, 940_000, 920_000, 950_000]
    dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]

    for i, (assets, date) in enumerate(zip(test_assets, dates)):
        result = src.update(assets, date)
        print(
            f"日期={date} 资产={assets:>12,} "
            f"盈亏={result['daily_pnl']:+.2%} "
            f"连续亏损={result['consecutive_loss']}天 "
            f"禁止开仓={result['forbid_new_buy']} "
            f"降仓={result['reduce_position']}"
        )

    print()
    print(src.format_status())

    # 清理
    src.reset()
