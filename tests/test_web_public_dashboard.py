"""公网仪表盘只读与脱敏契约测试。"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient


def ok(msg: str):
    print(f"  OK {msg}")


def _sample_status():
    return {
        "timestamp": "2026-07-01T09:30:00",
        "health": {"ok": False, "failed_required": ["LONGPORT_ACCESS_TOKEN=secret"]},
        "watchdog": {"ok": False, "criticals": ["python3 main.py --doctor"]},
        "control": {"paused": True, "reason": "doctor unresolved critical: /home/ubuntu/secret"},
        "account": {
            "initial_capital": 1000000.0,
            "total_assets": 1170000.0,
            "cash": 900000.0,
            "positions": ["600160"],
            "total_pnl": 170000.0,
            "total_pnl_pct": 0.17,
        },
        "adaptive": {"buy_threshold": 63, "min_score": 58},
        "crash_open": True,
        "pipeline_progress": {"prefetch": True, "scan": False, "execute": False, "review": False},
        "recent_logs": [
            {
                "time": "09:30:00",
                "type": "auto_doctor",
                "status": "盘中",
                "action": "systemctl --user restart quant-pilot-auto.service",
                "error": "Traceback (most recent call last): secret",
            }
        ],
        "risk_warnings": ["内部风险"],
        "loop_count": 7,
        "last_loop_time": "2026-07-01T09:29:58",
    }


def _load_prod_client():
    old_env = {
        key: os.environ.get(key)
        for key in [
            "ALPHAPILOT_ENV",
            "ENV",
            "PRODUCTION",
            "ALPHAPILOT_ENABLE_CONTROL_API",
            "ALPHAPILOT_EXPOSE_INTERNAL_STATUS",
        ]
    }
    os.environ["ALPHAPILOT_ENV"] = "production"
    os.environ.pop("ALPHAPILOT_ENABLE_CONTROL_API", None)
    os.environ.pop("ALPHAPILOT_EXPOSE_INTERNAL_STATUS", None)

    for name in ["web.server", "web.routers.status"]:
        sys.modules.pop(name, None)

    server = importlib.import_module("web.server")
    status_router = importlib.import_module("web.routers.status")
    status_router.build_agent_status_snapshot = _sample_status
    return TestClient(server.app), old_env


def _restore_env(old_env):
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for name in ["web.server", "web.routers.status"]:
        sys.modules.pop(name, None)


def test_public_status_is_sanitized():
    client, old_env = _load_prod_client()
    try:
        res = client.get("/api/status")
        assert res.status_code == 200
        data = res.json()
        assert data["public_mode"] is True
        assert "failed_required" not in data["health"]
        assert "reason" not in data["control"]
        assert "systemctl" not in data["recent_logs"][0]["action"]
        assert "Traceback" not in data["recent_logs"][0]["error"]
        ok("生产 /api/status 返回公开脱敏快照")
    finally:
        _restore_env(old_env)


def test_control_api_is_not_mounted_by_default_in_prod():
    client, old_env = _load_prod_client()
    try:
        res = client.post("/api/control/pause", json={"reason": "test"})
        assert res.status_code == 404
        ok("生产环境默认不挂载 Web 控制 API")
    finally:
        _restore_env(old_env)


def test_security_headers_are_present():
    client, old_env = _load_prod_client()
    try:
        res = client.get("/api/public/status")
        assert res.status_code == 200
        assert res.headers.get("x-frame-options") == "DENY"
        assert res.headers.get("x-content-type-options") == "nosniff"
        assert "frame-ancestors 'none'" in res.headers.get("content-security-policy", "")
        assert res.headers.get("cache-control") == "no-store"
        ok("公网响应带基础安全头")
    finally:
        _restore_env(old_env)


if __name__ == "__main__":
    test_public_status_is_sanitized()
    test_control_api_is_not_mounted_by_default_in_prod()
    test_security_headers_are_present()
    print("公网仪表盘契约测试通过")
