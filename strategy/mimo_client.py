"""
MiMo LLM API 安全调用封装。

生产环境中 requests 线程一旦卡在底层网络读写，Python 线程无法被强制杀死。
本模块在 POSIX 环境用独立子进程发起 HTTP 请求，超过硬超时后直接终止子进程，
避免自动盯盘主进程被外部 LLM 调用拖死。
"""
import logging
import multiprocessing as mp
import os
import queue
from typing import Optional

import requests

logger = logging.getLogger("strategy.mimo_client")

DEFAULT_HTTP_TIMEOUT = int(os.environ.get("MIMO_HTTP_TIMEOUT", "120"))
DEFAULT_CONNECT_TIMEOUT = int(os.environ.get("MIMO_CONNECT_TIMEOUT", "10"))


def _request_worker(url: str, headers: dict, payload: dict, timeout: tuple, result_queue):
    """子进程内执行HTTP请求，只通过Queue返回可序列化结果。"""
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        result_queue.put({"ok": True, "data": resp.json()})
    except Exception as e:
        result_queue.put({"ok": False, "error": str(e)})


def post_chat_completion(
    *,
    base_url: str,
    api_key: str,
    payload: dict,
    http_timeout: int = DEFAULT_HTTP_TIMEOUT,
    hard_timeout: int = None,
) -> Optional[dict]:
    """
    调用MiMo chat/completions接口。

    Args:
        base_url: MiMo API base URL
        api_key: API key
        payload: OpenAI兼容chat/completions payload
        http_timeout: requests read timeout秒数
        hard_timeout: 进程级硬超时秒数，默认http_timeout+5

    Returns:
        JSON响应dict；失败或超时返回None
    """
    if not api_key:
        logger.warning("XIAOMI_API_KEY 未设置，跳过LLM调用")
        return None

    hard_timeout = hard_timeout or (int(http_timeout) + 5)
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = (DEFAULT_CONNECT_TIMEOUT, int(http_timeout))

    # Windows spawn 在交互/测试入口下容易受 __main__ 保护影响；本地开发直接用requests超时。
    if os.name == "nt":
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return None

    ctx = mp.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_request_worker, args=(url, headers, payload, timeout, result_queue))
    proc.daemon = True
    proc.start()
    proc.join(float(hard_timeout))

    if proc.is_alive():
        proc.terminate()
        proc.join(3)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        logger.error(f"LLM调用硬超时({hard_timeout}s)，已终止HTTP子进程")
        return None

    try:
        result = result_queue.get_nowait()
    except queue.Empty:
        logger.error(f"LLM调用无返回，exitcode={proc.exitcode}")
        return None

    if result.get("ok"):
        return result.get("data") or {}

    logger.error(f"LLM调用失败: {result.get('error')}")
    return None
