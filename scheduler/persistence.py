"""进程间串行更新 JSON 快照；锁文件独立于被原子替换的数据文件。"""
import json
import os
import tempfile
import time
from contextlib import contextmanager


@contextmanager
def json_file_lock(path, timeout=10):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("JSON状态更新锁超时")
                time.sleep(0.02)
        try:
            # Windows允许锁定EOF之后的字节；初始化必须在领取锁后，不能先读被锁区域。
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json_write(path, payload):
    """调用方已持锁时原子替换 JSON 快照。"""
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_json(path, update):
    """在文件锁内读取、合并并原子替换；损坏快照由调用方决定如何恢复。"""
    path = os.path.abspath(path)
    with json_file_lock(path):
        previous = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                previous = json.load(handle)
        payload = update(previous)
        atomic_json_write(path, payload)
        return payload
