"""有限缓存与在途合并；失败短暂退避，不把过期健康状态当成当前成功。"""
import copy
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone


class SnapshotCache:
    def __init__(self, ttl=10, failure_backoff=3, max_entries=128):
        self.ttl = ttl
        self.failure_backoff = failure_backoff
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries = {}
        self._flights = {}

    def get(self, key, build):
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and now < entry[0]:
                if entry[2] is not None:
                    raise RuntimeError("数据快照构建失败，稍后重试") from entry[2]
                return copy.deepcopy(entry[1])
            future = self._flights.get(key)
            leader = future is None
            if leader:
                future = self._flights[key] = Future()
        if not leader:
            return copy.deepcopy(future.result(timeout=30))
        try:
            value = build()
            if isinstance(value, dict):
                value = copy.deepcopy(value)
                value.setdefault("snapshot_at", datetime.now(timezone.utc).isoformat())
            with self._lock:
                if len(self._entries) >= self.max_entries:
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = (time.monotonic() + self.ttl, value, None)
            future.set_result(value)
            return copy.deepcopy(value)
        except Exception as exc:
            with self._lock:
                if len(self._entries) >= self.max_entries:
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = (time.monotonic() + self.failure_backoff, None, exc)
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)


cache = SnapshotCache()
