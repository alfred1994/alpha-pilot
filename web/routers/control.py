from fastapi import APIRouter, Body
from scheduler.control import get_auto_control_state, save_auto_control_state
from datetime import datetime

router = APIRouter()

@router.post("/control/pause")
def pause_trading(reason: str = Body(..., embed=True)):
    """暂停盘中自动交易动作"""
    try:
        state = {
            "paused": True,
            "reason": reason or "Web端手动暂停",
            "updated_at": datetime.now().isoformat()
        }
        save_auto_control_state(state)
        return {"success": True, "message": "已暂停日内自动交易动作", "state": state}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/control/resume")
def resume_trading():
    """恢复盘中自动交易动作"""
    try:
        state = {
            "paused": False,
            "reason": "",
            "updated_at": datetime.now().isoformat()
        }
        save_auto_control_state(state)
        return {"success": True, "message": "已恢复日内自动交易动作", "state": state}
    except Exception as e:
        return {"success": False, "error": str(e)}
