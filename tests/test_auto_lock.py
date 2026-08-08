#!/usr/bin/env python3
"""
自动盯盘单实例锁测试

验证常驻 --auto 循环的锁文件可以防止重复启动，
并且过期锁会被回收。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler.auto_trader as auto_trader
from scheduler.auto_trader import AutoLoopLock, AutoLoopLockError


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    temp_dir = tempfile.mkdtemp(prefix="quant_auto_lock_")
    lock_path = os.path.join(temp_dir, "auto.lock")
    try:
        lock = AutoLoopLock(lock_file=lock_path, stale_after=60).acquire()
        assert_true(os.path.exists(lock_path), "首次获取锁会创建锁文件")

        blocked = False
        try:
            AutoLoopLock(lock_file=lock_path, stale_after=60).acquire()
        except AutoLoopLockError:
            blocked = True
        assert_true(blocked, "第二个实例会被锁阻止")

        before_mtime = os.path.getmtime(lock_path)
        time.sleep(0.02)
        lock.heartbeat()
        after_mtime = os.path.getmtime(lock_path)
        assert_true(after_mtime >= before_mtime, "锁心跳会刷新锁文件")

        lock.release()
        assert_true(not os.path.exists(lock_path), "释放锁会删除锁文件")

        with open(lock_path, "w", encoding="utf-8") as f:
            f.write('{"pid": 999999, "token": "stale"}')
        stale_at = time.time() - 120
        os.utime(lock_path, (stale_at, stale_at))
        recovered = AutoLoopLock(lock_file=lock_path, stale_after=1).acquire()
        assert_true(os.path.exists(lock_path), "过期锁会被新实例回收")
        recovered.release()
        assert_true(not os.path.exists(lock_path), "回收后的锁可正常释放")

        # 独立入口（如Doctor）必须通过带锁包装器进入自动循环。
        calls = []
        original_run_auto_cycle = auto_trader.run_auto_cycle
        auto_trader.run_auto_cycle = lambda **kwargs: calls.append(kwargs) or {"ok": True}
        try:
            wrapped = auto_trader.run_locked_auto_cycle(lock_file=lock_path, marker="wrapped")
            assert_true(wrapped == {"ok": True}, "带锁自动循环返回底层结果")
            assert_true(calls == [{"marker": "wrapped"}], "带锁入口调用一次底层自动循环")
            assert_true(not os.path.exists(lock_path), "带锁自动循环结束后释放锁")

            held = AutoLoopLock(lock_file=lock_path, stale_after=60).acquire()
            blocked_wrapper = False
            try:
                auto_trader.run_locked_auto_cycle(lock_file=lock_path)
            except AutoLoopLockError:
                blocked_wrapper = True
            finally:
                held.release()
            assert_true(blocked_wrapper, "带锁入口会阻止并发自动循环")
        finally:
            auto_trader.run_auto_cycle = original_run_auto_cycle

        print("自动盯盘单实例锁测试通过")

    finally:
        if os.path.exists(lock_path):
            os.unlink(lock_path)
        if os.path.isdir(temp_dir):
            os.rmdir(temp_dir)


if __name__ == "__main__":
    main()
