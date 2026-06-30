import os
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from scheduler.control import get_auto_control_state, save_auto_control_state
from datetime import datetime

router = APIRouter()


def verify_control_token(request: Request):
    expected_token = os.environ.get("ALPHAPILOT_CONTROL_TOKEN")
    if not expected_token:
        return
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = auth_header.split(" ")[1]
    if token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid control token",
        )


def _insert_web_control_event(status_str: str, action: str, reason: str = ""):
    """记录 Web 控制台操作审计，保持 auto_events 的 JSON 字段契约。"""
    try:
        from data.database import Database

        with Database() as db:
            db.insert_auto_event({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "event_type": "web_control",
                "status": status_str,
                "actions": [action],
                "details": {
                    "source": "web",
                    "reason": reason,
                },
            })
    except Exception:
        pass


@router.post("/control/pause", dependencies=[Depends(verify_control_token)])
def pause_trading(reason: str = Body(..., embed=True)):
    """暂停盘中自动交易动作"""
    try:
        state = {
            "paused": True,
            "reason": reason or "Web端手动暂停",
            "updated_at": datetime.now().isoformat()
        }
        save_auto_control_state(state)
        _insert_web_control_event("paused", "pause", state["reason"])

        return {"success": True, "message": "已暂停日内自动交易动作", "state": state}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/control/resume", dependencies=[Depends(verify_control_token)])
def resume_trading():
    """恢复盘中自动交易动作"""
    try:
        state = {
            "paused": False,
            "reason": "",
            "updated_at": datetime.now().isoformat()
        }
        save_auto_control_state(state)
        _insert_web_control_event("active", "resume", "通过Web页面恢复交易")

        return {"success": True, "message": "已恢复日内自动交易动作", "state": state}
    except Exception as e:
        return {"success": False, "error": str(e)}

