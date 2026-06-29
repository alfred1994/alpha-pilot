"""
MiMo LLM API 及 备用大模型安全调用封装。

生产环境中 requests 线程一旦卡在底层网络读写，Python 线程无法被强制杀死。
本模块在 POSIX 环境用独立子进程发起 HTTP 请求，超过硬超时后直接终止子进程，
避免自动盯盘主进程被外部 LLM 调用拖死；同时提供三级 API 降级 Fallback 保护网。
"""
import logging
import multiprocessing as mp
import os
import queue
from typing import Optional

import requests

logger = logging.getLogger("strategy.mimo_client")

# 从环境变量统一获取密钥和端点。
# Hermes 服务器主配置使用 XIAOMI_BASE_URL；MIMO_BASE_URL 作为兼容别名保留。
MIMO_BASE_URL = (
    os.environ.get("XIAOMI_BASE_URL")
    or os.environ.get("MIMO_BASE_URL")
    or "https://token-plan-cn.xiaomimimo.com/v1"
)
XIAOMI_API_KEY = os.environ.get("XIAOMI_API_KEY")

# 备用大模型网关 (Fallback)
FALLBACK_BASE_URL = os.environ.get("FALLBACK_BASE_URL", "https://api.deepseek.com/v1")
FALLBACK_API_KEY = os.environ.get("FALLBACK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "deepseek-chat")

DEFAULT_HTTP_TIMEOUT = int(os.environ.get("MIMO_HTTP_TIMEOUT", "45"))
DEFAULT_CONNECT_TIMEOUT = int(os.environ.get("MIMO_CONNECT_TIMEOUT", "8"))


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
    payload: dict,
    base_url: str = None,
    api_key: str = None,
    http_timeout: int = DEFAULT_HTTP_TIMEOUT,
    hard_timeout: int = None,
) -> Optional[dict]:
    """
    统一调用 chat/completions 接口，支持硬超时拦截与主备降级 (Fallback)

    Args:
        payload: OpenAI 兼容的 chat/completions 请求体
        base_url: 显式覆盖大模型 Base URL (可选)
        api_key: 显式覆盖大模型 API Key (可选)
        http_timeout: requests read timeout秒数
        hard_timeout: 进程级硬超时秒数，默认 http_timeout + 5

    Returns:
        JSON 响应 dict；全部网关均不可用或超时返回 None
    """
    api_key = api_key or XIAOMI_API_KEY
    base_url = base_url or MIMO_BASE_URL
    
    # 检查主 API 连通可用性，如无则直接尝试备用降级
    if not api_key:
        if FALLBACK_API_KEY:
            logger.warning("XIAOMI_API_KEY 未设置，自动降级切换至备用大模型端点...")
            api_key = FALLBACK_API_KEY
            base_url = FALLBACK_BASE_URL
            if "model" in payload:
                payload["model"] = FALLBACK_MODEL
        else:
            logger.warning("未配置任何有效大模型 API_KEY (XIAOMI_API_KEY / FALLBACK_API_KEY)，跳过调用")
            return None

    hard_timeout = hard_timeout or (int(http_timeout) + 5)
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = (DEFAULT_CONNECT_TIMEOUT, int(http_timeout))

    # 执行主大模型请求
    res_data = _execute_with_process_guard(url, headers, payload, timeout, hard_timeout)
    
    # 如果主调用超时或报错，且存在备用大模型 API，则触发 Fallback
    if res_data is None and api_key != FALLBACK_API_KEY and FALLBACK_API_KEY:
        logger.warning(f"主大模型 API 调用失败或硬超时，自动触发备用 Fallback 网关 -> {FALLBACK_BASE_URL}")
        fallback_url = f"{FALLBACK_BASE_URL.rstrip('/')}/chat/completions"
        fallback_headers = {
            "Authorization": f"Bearer {FALLBACK_API_KEY}",
            "Content-Type": "application/json",
        }
        fallback_payload = payload.copy()
        fallback_payload["model"] = FALLBACK_MODEL
        res_data = _execute_with_process_guard(fallback_url, fallback_headers, fallback_payload, timeout, hard_timeout)
        
    return res_data


def _execute_with_process_guard(url: str, headers: dict, payload: dict, timeout: tuple, hard_timeout: int) -> Optional[dict]:
    """带有独立子进程硬超时的网络请求保护"""
    if os.name == "nt":
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"大模型 HTTP 请求失败: {e}")
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
        logger.error(f"大模型调用进程硬超时 ({hard_timeout}s) 强制终止")
        return None

    try:
        result = result_queue.get_nowait()
    except queue.Empty:
        logger.error(f"大模型调用无返回数据, exitcode={proc.exitcode}")
        return None

    if result.get("ok"):
        return result.get("data") or {}

    logger.error(f"大模型服务错误: {result.get('error')}")
    return None
