from fastapi import APIRouter
from scheduler.agent_status import build_agent_status_snapshot

router = APIRouter()

@router.get("/status")
def get_system_status():
    """获取系统运行健康状态、Watchdog及仓位账户信息"""
    return build_agent_status_snapshot()
