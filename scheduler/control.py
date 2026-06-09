"""
自动盯盘控制开关
====================================================================
提供运行期暂停/恢复能力：
  - 暂停时跳过盘中止损巡检、扫描和模拟执行
  - 盘前数据预热、盘后复盘仍可继续
  - 控制状态写入本地JSON，便于计划任务和人工操作共享
====================================================================
"""
import json
import os
from datetime import datetime
from typing import Dict

from config import DATA_DIR


AUTO_CONTROL_FILE = os.path.join(DATA_DIR, "auto_control.json")


def _default_state() -> Dict:
    """默认控制状态"""
    return {
        "paused": False,
        "reason": "",
        "updated_at": "",
        "updated_by": "system",
    }


def get_auto_control_state(control_file: str = None) -> Dict:
    """读取自动盯盘控制状态"""
    control_file = control_file or AUTO_CONTROL_FILE
    if not os.path.exists(control_file):
        return _default_state()
    try:
        with open(control_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = _default_state()
        state.update(data if isinstance(data, dict) else {})
        return state
    except Exception:
        return _default_state()


def save_auto_control_state(state: Dict, control_file: str = None) -> Dict:
    """保存自动盯盘控制状态"""
    control_file = control_file or AUTO_CONTROL_FILE
    os.makedirs(os.path.dirname(control_file), exist_ok=True)
    payload = _default_state()
    payload.update(state or {})
    payload["updated_at"] = datetime.now().isoformat()
    with open(control_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def pause_auto_trader(reason: str = "", control_file: str = None,
                      updated_by: str = "manual") -> Dict:
    """暂停盘中交易动作"""
    return save_auto_control_state({
        "paused": True,
        "reason": reason or "manual pause",
        "updated_by": updated_by,
    }, control_file=control_file)


def resume_auto_trader(reason: str = "", control_file: str = None,
                       updated_by: str = "manual") -> Dict:
    """恢复盘中交易动作"""
    return save_auto_control_state({
        "paused": False,
        "reason": reason or "manual resume",
        "updated_by": updated_by,
    }, control_file=control_file)


def is_auto_paused(control_file: str = None) -> bool:
    """是否处于暂停状态"""
    return bool(get_auto_control_state(control_file=control_file).get("paused"))


def format_auto_control_state(state: Dict) -> str:
    """格式化控制状态"""
    status = "暂停" if state.get("paused") else "运行"
    lines = [
        "=" * 60,
        "自动盯盘控制状态",
        "=" * 60,
        f"状态: {status}",
        f"原因: {state.get('reason', '')}",
        f"更新人: {state.get('updated_by', '')}",
        f"更新时间: {state.get('updated_at', '')}",
        "=" * 60,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_auto_control_state(get_auto_control_state()))
