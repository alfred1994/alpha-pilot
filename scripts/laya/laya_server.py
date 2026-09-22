#!/usr/bin/env python3
"""Laya 本地决策服务（在 laya venv 内运行，保持模型常热）。

用法:
    ~/.laya-venv/bin/python scripts/laya/laya_server.py [--port 8642]

端点（只监听 127.0.0.1）:
    GET  /health   -> {"ok": true, "model": ..., "uptime_s": ...}
    POST /predict  -> body: {"states": [state, ...], "questions": {...}}
                      返回: {"ok": true, "results": [...], "latency_ms": ...}

设计约定:
  - 模块级只 import 标准库，torch/laya 在 main() 延迟加载，AlphaPilot 的
    离线测试可直接 import 本模块做契约测试；
  - 每个请求独立计时，失败以 JSON 信封返回 HTTP 500，不抛裸异常；
  - questions 由调用方声明（typed questions: choice/score/noul），
    本服务不理解业务语义，只执行前向传播。
"""
import argparse
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8642

_ROUTER = None
_STARTED_AT = time.time()


def build_ok_envelope(results, latency_ms):
    return {"ok": True, "results": results, "latency_ms": round(latency_ms, 2)}


def build_error_envelope(error):
    return {"ok": False, "error": str(error)}


def load_router(preload=True):
    """延迟导入 laya 并加载 Router（含全部 checkpoint 预载）。"""
    from laya import Router
    return Router(preload=preload)


def make_handler(router):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 访问日志走 stderr，保持 stdout 干净
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                # 客户端超时先行断开：结果已算完但送不出去，记一行即可，
                # 不刷 traceback（ThreadingHTTPServer 本就逐连接隔离）。
                sys.stderr.write("client disconnected before response: %s\n" % exc)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                self._send_json({
                    "ok": True,
                    "model": "laya-router",
                    "uptime_s": round(time.time() - _STARTED_AT, 1),
                })
            else:
                self._send_json(build_error_envelope("unknown path"), 404)

        def do_POST(self):
            if self.path.rstrip("/") != "/predict":
                self._send_json(build_error_envelope("unknown path"), 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 32 * 1024 * 1024:
                    raise ValueError("empty or oversized body")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                states = request.get("states")
                questions = request.get("questions")
                if not isinstance(states, list) or not states:
                    raise ValueError("states 必须是非空列表")
                if not isinstance(questions, dict) or not questions:
                    raise ValueError("questions 必须是非空对象")
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                self._send_json(build_error_envelope(f"请求非法: {exc}"), 400)
                return
            started = time.time()
            try:
                results = []
                for state in states:
                    res = router.predict(state, questions)
                    results.append(res)
            except Exception as exc:  # noqa: BLE001 —— 信封化一切推理失败
                self._send_json(
                    build_error_envelope(f"{type(exc).__name__}: {exc}"), 500,
                )
                return
            self._send_json(build_ok_envelope(results, (time.time() - started) * 1000))

    return Handler


def main(argv):
    parser = argparse.ArgumentParser(description="Laya 本地决策服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1",
                        help="只允许本机地址；服务无鉴权，禁止暴露公网")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    print(f">> 加载 Laya Router（preload，约需 30s-2min 下载/加载权重）...")
    router = load_router(preload=True)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(router))
    print(f">> Laya 服务就绪: http://{args.host}:{args.port} "
          f"(PID {__import__('os').getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
