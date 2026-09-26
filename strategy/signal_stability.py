"""信号稳定性跟踪：把"单次越过阈值"变成"持续站上阈值"。

盘中技术面全程冻结（同一只票当天 8~15 次扫描的技术分完全一致），而舆情/
情绪/ML 这些非价量维度会在盘中抖动。系统原先每轮扫描都独立判定一次，
再按综合分排序取当日最高的那一轮成交——等价于"从十几次噪声采样里挑最大值"，
会稳定地制造出并不存在的买入信号（2026-09-24 沪电股份全天技术分锁死 54.3，
仅因舆情分 65→78 就在 13:50 触发买入）。

这里要求候选连续 N 轮站上买入门槛才具备开仓资格：技术面既然盘中不变，
重复扫描本就不产生新信息，确认机制只保留"持续为真"的信号，滤掉单次尖峰。
"""
import json
import logging
import os
import threading
from typing import Dict, Optional

from config import DATA_DIR

logger = logging.getLogger("strategy.signal_stability")

SIGNAL_CONFIRM_ROUNDS = int(os.environ.get("SIGNAL_CONFIRM_ROUNDS", "2"))
SIGNAL_STABILITY_FILE = os.path.join(DATA_DIR, "signal_stability.json")


def _default_state_file() -> str:
    """默认状态路径；ALPHAPILOT_RISK_STATE_DIR 存在时跟随该隔离目录。

    回归运行会设置该变量，避免测试把候选连续过线轮数写进生产 data/。
    """
    override = os.environ.get("ALPHAPILOT_RISK_STATE_DIR")
    if override:
        return os.path.join(override, os.path.basename(SIGNAL_STABILITY_FILE))
    return SIGNAL_STABILITY_FILE


class SignalStabilityTracker:
    """按 (交易日, 标的) 统计连续过线轮数。"""

    def __init__(
        self,
        required_rounds: int = None,
        state_file: str = None,
    ):
        self.required_rounds = (
            required_rounds if required_rounds is not None else SIGNAL_CONFIRM_ROUNDS
        )
        self.state_file = state_file or _default_state_file()
        self._lock = threading.RLock()
        self._date = ""
        self._streaks: Dict[str, int] = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._date = str(data.get("date") or "")
                self._streaks = {
                    str(k): int(v) for k, v in (data.get("streaks") or {}).items()
                }
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("信号稳定性状态读取失败，按空状态开始: %s", exc)
            self._date = ""
            self._streaks = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            payload = {"date": self._date, "streaks": self._streaks}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            logger.warning("信号稳定性状态写入失败(非致命): %s", exc)

    def observe(self, date: str, code: str, composite: float, min_score: float) -> bool:
        """登记一轮观察，返回该标的当前是否已具备开仓资格。"""
        with self._lock:
            if date != self._date:
                self._date = date
                self._streaks = {}
            key = str(code)
            if float(composite or 0) >= float(min_score or 0):
                self._streaks[key] = self._streaks.get(key, 0) + 1
            else:
                self._streaks[key] = 0
            self._save()
            return self._streaks[key] >= self.required_rounds

    def streak(self, code: str) -> int:
        with self._lock:
            return self._streaks.get(str(code), 0)

    def reset(self, date: str = ""):
        with self._lock:
            self._date = date
            self._streaks = {}
            self._save()


def technical_gate_score(dimensions: Optional[dict]) -> Optional[float]:
    """从候选的 dimensions 里取技术分；结构异常返回 None。"""
    if not isinstance(dimensions, dict):
        return None
    technical = dimensions.get("technical")
    if isinstance(technical, dict):
        technical = technical.get("score")
    if isinstance(technical, bool) or not isinstance(technical, (int, float)):
        return None
    return float(technical)
