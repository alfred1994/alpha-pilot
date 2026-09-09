#!/usr/bin/env python3
"""CloakBrowser 共享生命周期测试（完全使用假浏览器，不启动真实进程）。"""
import concurrent.futures
import os
import sys
import asyncio
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.eastmoney import _CloakBrowserManager, get_cloakbrowser_process_snapshot


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


class _FakePage:
    def __init__(self):
        self.closed = False

    async def goto(self, *_args, **_kwargs):
        return None

    async def evaluate(self, _script):
        return {"data": {"ok": True}}

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.closed = False
        self.pages = []

    def is_connected(self):
        return not self.closed

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


def main():
    launches = []

    async def launcher(**_kwargs):
        browser = _FakeBrowser()
        launches.append(browser)
        return browser

    manager = _CloakBrowserManager(
        launcher=launcher,
        idle_seconds=0.05,
        navigation_wait_seconds=0,
        retry_wait_seconds=0,
    )
    try:
        # 延迟首个事件循环发布，稳定复现多个调用方同时进入 _ensure_worker 的窗口。
        # 启动锁必须覆盖 ready 等待，确保不会创建第二个 loop。
        loop_creation_started = threading.Event()
        allow_loop_creation = threading.Event()
        original_new_event_loop = asyncio.new_event_loop

        def delayed_new_event_loop():
            loop_creation_started.set()
            assert allow_loop_creation.wait(timeout=2), "测试未释放事件循环启动"
            return original_new_event_loop()

        import data.eastmoney as eastmoney
        original_factory = eastmoney.asyncio.new_event_loop
        eastmoney.asyncio.new_event_loop = delayed_new_event_loop
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(
                    manager.fetch, "https://example.invalid/data", "https://example.invalid/"
                ) for _ in range(4)]
                assert_true(loop_creation_started.wait(timeout=1), "首个事件循环进入延迟启动窗口")
                time.sleep(0.05)
                allow_loop_creation.set()
                results = [future.result(timeout=2) for future in futures]
        finally:
            eastmoney.asyncio.new_event_loop = original_factory
        assert_true(len(launches) == 1, "并发请求只启动一个共享浏览器实例")
        assert_true(all(result == {"data": {"ok": True}} for result in results), "共享浏览器返回全部请求结果")
        assert_true(all(page.closed for page in launches[0].pages), "每次请求结束均关闭页面子进程资源")
        time.sleep(0.15)
        assert_true(launches[0].closed, "空闲20分钟策略可由可配置短周期验证自动回收")
        assert_true(not manager.snapshot()["browser_active"], "回收后管理器不再保留浏览器引用")

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = "101 30 /opt/CloakBrowser --headless\n102 7201 /usr/bin/chromium --remote-debugging-port=0 --type=renderer\n103 1 /usr/bin/google-chrome\n"

        snapshot = get_cloakbrowser_process_snapshot(process_runner=lambda *_args, **_kwargs: _Proc())
        assert_true(snapshot["instance_count"] == 1, "进程快照按browser主进程统计实例")
        assert_true(snapshot["process_count"] == 2, "进程快照保留受管子进程总数")
        assert_true(snapshot["oldest_age_seconds"] == 7201, "进程快照报告最长存活时间")

        class _ProcWithCrashpad:
            returncode = 0
            stderr = ""
            stdout = (
                "201 100 /home/u/.cloakbrowser/chromium-146/chrome --headless --user-data-dir=/tmp/p1\n"
                "202 100 /home/u/.cloakbrowser/chromium-146/chrome_crashpad_handler --monitor-self\n"
                "203 100 /home/u/.cloakbrowser/chromium-146/chrome_crashpad_handler --no-periodic-tasks\n"
                "204 100 /home/u/.cloakbrowser/chromium-146/chrome --type=zygote --headless\n"
            )

        snapshot = get_cloakbrowser_process_snapshot(process_runner=lambda *_args, **_kwargs: _ProcWithCrashpad())
        assert_true(snapshot["instance_count"] == 1, "crashpad_handler伴生进程不计入浏览器实例数")
        assert_true(snapshot["process_count"] == 4, "crashpad_handler仍计入受管进程总数")
    finally:
        manager.close()
    print("浏览器生命周期测试通过")


if __name__ == "__main__":
    main()
