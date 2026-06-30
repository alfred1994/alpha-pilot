"""Hermes 驾驶员接口关键契约测试。"""
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ok(msg: str):
    print(f"  OK {msg}")


def test_mimo_base_url_priority():
    """统一 LLM 客户端优先使用 Hermes 的 XIAOMI_BASE_URL。"""
    old_xiaomi = os.environ.get("XIAOMI_BASE_URL")
    old_mimo = os.environ.get("MIMO_BASE_URL")

    import strategy.mimo_client as client

    try:
        os.environ["XIAOMI_BASE_URL"] = "https://example.xiaomi/v1"
        os.environ["MIMO_BASE_URL"] = "https://example.mimo/v1"
        importlib.reload(client)
        assert client.MIMO_BASE_URL == "https://example.xiaomi/v1"
        ok("XIAOMI_BASE_URL 优先级高于 MIMO_BASE_URL")

        os.environ.pop("XIAOMI_BASE_URL", None)
        os.environ["MIMO_BASE_URL"] = "https://example.mimo/v1"
        importlib.reload(client)
        assert client.MIMO_BASE_URL == "https://example.mimo/v1"
        ok("缺少 XIAOMI_BASE_URL 时兼容 MIMO_BASE_URL")

        os.environ.pop("XIAOMI_BASE_URL", None)
        os.environ.pop("MIMO_BASE_URL", None)
        importlib.reload(client)
        assert client.MIMO_BASE_URL == "https://token-plan-cn.xiaomimimo.com/v1"
        ok("缺少环境变量时使用项目既有 MiMo 网关")
    finally:
        if old_xiaomi is None:
            os.environ.pop("XIAOMI_BASE_URL", None)
        else:
            os.environ["XIAOMI_BASE_URL"] = old_xiaomi
        if old_mimo is None:
            os.environ.pop("MIMO_BASE_URL", None)
        else:
            os.environ["MIMO_BASE_URL"] = old_mimo
        importlib.reload(client)


def test_web_control_event_json_contract():
    """Web 控制审计写入后必须能按 auto_events JSON 契约读回。"""
    from data.database import Database as RealDatabase
    import data.database as database_module
    from web.routers.control import _insert_web_control_event

    old_database = database_module.Database
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "quant.db")

        def database_factory(*args, **kwargs):
            return RealDatabase(db_path=db_path)

        database_module.Database = database_factory
        try:
            _insert_web_control_event("paused", "pause", "测试暂停")
        finally:
            database_module.Database = old_database

        with RealDatabase(db_path=db_path) as db:
            events = db.get_auto_events(event_type="web_control", limit=1)

        assert len(events) == 1
        assert events[0]["actions"] == ["pause"]
        assert events[0]["details"]["source"] == "web"
        assert events[0]["details"]["reason"] == "测试暂停"
        ok("Web 控制审计保留 actions/details JSON 结构")


if __name__ == "__main__":
    test_mimo_base_url_priority()
    test_web_control_event_json_contract()
    print("Hermes 驾驶员关键契约测试通过")
