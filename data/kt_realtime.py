"""
KT 实时行情适配器 - 新浪公开快照 + 腾讯公开K线
====================================================================
只读的公开行情快照和轮询客户端；不保证实时、完整或可用于交易。

与 data/realtime.py（腾讯 qt.gtimg.cn 源）的分工:
  - 本模块: 批量快照（一次请求60只）、五档买卖盘、沪深京/ETF/转债/指数
    覆盖、data_valid/amount_status 数据质量标注、1m~周K线。
    但不提供 pe/turnover/market_cap（新浪快照无此字段）。
  - data/realtime.py: 提供 pe/turnover/market_cap（decision.py、
    market_timing.py 的估值逻辑依赖），作为本模块失败时的回退源。

来源: 外部 KTRealtimeClient 脚本移植，相对原版的差异:
  - print() 改为 logging（项目约定）
  - 移除 kt_data_engine 本地缓存（本项目不存在该依赖；
    up_limit/down_limit/limit_count 列保留为 NaN，供后续涨跌停逻辑填充）
====================================================================
"""
import concurrent.futures
import datetime as dt
import json
import logging
import math
import re
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple, Union

import pandas as pd

from data.quote_validation import validate_quote

logger = logging.getLogger("data.kt_realtime")


class KTRealtimeClient:
    """公开源适配器。start_stream 是轮询，不是交易所推送流。"""

    TICK_COLUMNS = [
        "market", "code", "symbol", "sec_type", "name",
        "price", "change", "pct_change", "open", "high", "low", "pre_close",
        "up_limit", "down_limit", "limit_count",
        "volume_raw", "volume_hand", "amount_yuan", "amount_wan",
        "bid1_p", "bid1_v", "bid2_p", "bid2_v", "bid3_p", "bid3_v", "bid4_p", "bid4_v", "bid5_p", "bid5_v",
        "ask1_p", "ask1_v", "ask2_p", "ask2_v", "ask3_p", "ask3_v", "ask4_p", "ask4_v", "ask5_p", "ask5_v",
        "time", "source", "received_at", "data_date", "data_valid", "invalid_reason", "amount_status",
    ]
    KLINE_COLUMNS = [
        "code", "symbol", "sec_type", "datetime", "open", "close", "high", "low",
        "volume_raw", "volume_hand", "amount_yuan",
        "source", "received_at", "data_date", "adjustment", "amount_status", "data_valid",
    ]
    SUPPORTED_PERIODS = {
        "1": "m1", "1m": "m1", "min1": "m1", "5": "m5", "5m": "m5", "min5": "m5",
        "15": "m15", "15m": "m15", "min15": "m15", "30": "m30", "30m": "m30", "min30": "m30",
        "60": "m60", "60m": "m60", "min60": "m60", "d": "day", "day": "day", "1d": "day",
        "w": "week", "week": "week", "1w": "week",
    }
    _INDEX_CODES = {("SH", "000001"), ("SH", "000300"), ("SZ", "399001"), ("SZ", "399006")}
    _AMOUNT_MISSING = "unavailable_provider_payload"

    def __init__(self, max_workers: int = 8):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
            raise ValueError("max_workers 必须是 1 到 32 的整数")
        self.max_workers = max_workers
        self._stream_lock = threading.RLock()
        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop_event: Optional[threading.Event] = None
        self._last_tick_cache: Dict[str, Tuple] = {}

    @staticmethod
    def _finite_float(value) -> Optional[float]:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _checked_int(name: str, value: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} 必须是 1 到 {maximum} 的整数")
        return value

    @staticmethod
    def _notify_error(callback: Optional[Callable[[str], None]], message: str) -> None:
        if callback is not None:
            try:
                callback(message)
            except Exception:
                pass

    @classmethod
    def normalize_security(cls, raw_code: str) -> Tuple[str, str, str, str]:
        """解析严格的六位代码和 SH/SZ/BJ 前后缀。

        兼容 600000、sh600000、sh.600000、600000.SH。裸 000001 是深市股票；
        上证指数请明确写 sh000001。
        """
        text = str(raw_code).strip().upper()
        match = re.fullmatch(r"(?:(SH|SZ|BJ)\.?(\d{6})|(\d{6})\.(SH|SZ|BJ)|(\d{6}))", text)
        if not match:
            raise ValueError(f"非法证券代码: {raw_code!r}；仅支持六位数字及 SH/SZ/BJ 前后缀")
        prefix, prefix_code, suffix_code, suffix, bare_code = match.groups()
        code = prefix_code or suffix_code or bare_code
        supplied_market = prefix or suffix
        if code.startswith(("110", "111", "113", "118")):
            inferred, sec_type = "SH", "convertible"
        elif code.startswith(("123", "127", "128")):
            inferred, sec_type = "SZ", "convertible"
        elif code.startswith(("50", "51", "56", "58")):
            inferred, sec_type = "SH", "etf"
        elif code.startswith(("15", "16", "18")):
            inferred, sec_type = "SZ", "etf"
        elif code.startswith(("4", "8", "920")):
            inferred, sec_type = "BJ", "stock"
        elif code.startswith(("600", "601", "603", "605", "688", "900")):
            inferred, sec_type = "SH", "stock"
        elif code.startswith(("000", "001", "002", "003", "200", "300", "399")):
            inferred, sec_type = "SZ", "stock"
        else:
            raise ValueError(f"无法根据代码识别沪深京市场: {raw_code!r}")
        market = supplied_market or inferred
        if supplied_market and market != inferred:
            # sh000001/sh000300 是故意指定的上证指数例外。
            if (market, code) not in {("SH", "000001"), ("SH", "000300")}:
                raise ValueError(f"证券代码与市场后缀冲突: {raw_code!r}")
        if (market, code) in cls._INDEX_CODES:
            sec_type = "index"
        return market, code, f"{market.lower()}{code}", sec_type

    @staticmethod
    def _data_date(value: str) -> Optional[str]:
        value = (value or "").strip()
        if re.fullmatch(r"\d{8}", value):
            date_format = "%Y%m%d"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date_format = "%Y-%m-%d"
        else:
            return None
        try:
            return dt.datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            return None

    @staticmethod
    def _valid_snapshot_time(date_value: Optional[str], time_value: str) -> bool:
        if date_value is None or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", (time_value or "").strip()):
            return False
        return validate_quote({
            "price": 1.0,
            "timestamp": f"{date_value} {time_value}",
        }).valid

    @staticmethod
    def _snapshot_invalid_reason(date_value: Optional[str], time_value: str) -> str:
        if date_value is None or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", (time_value or "").strip()):
            return "invalid_source_datetime"
        validation = validate_quote({
            "price": 1.0,
            "timestamp": f"{date_value} {time_value}",
        })
        return validation.reason or "invalid_source_datetime"

    @classmethod
    def _empty_ticks(cls, requested=None, errors=None) -> pd.DataFrame:
        frame = pd.DataFrame(columns=cls.TICK_COLUMNS)
        frame.attrs.update({"source": "sina_hq", "requested_count": len(requested or []), "returned_count": 0,
                            "missing_symbols": list(requested or []), "source_errors": list(errors or [])})
        return frame

    @classmethod
    def _empty_klines(cls, errors=None) -> pd.DataFrame:
        frame = pd.DataFrame(columns=cls.KLINE_COLUMNS)
        frame.attrs.update({"source": "tencent_gtimg", "source_errors": list(errors or [])})
        return frame

    def _fetch_single_chunk(self, chunk_map, on_error=None) -> Tuple[List[Dict], List[str]]:
        req = urllib.request.Request(
            f"https://hq.sinajs.cn/list={','.join(chunk_map)}",
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3.5) as response:
                content = response.read().decode("gbk", errors="ignore")
        except Exception as exc:
            message = f"sina_hq 请求失败 ({type(exc).__name__})"
            self._notify_error(on_error, message)
            return [], [message]

        received_at, rows = self._now(), []
        for line in content.splitlines():
            if "=" not in line:
                continue
            left, right = line.strip().split("=", 1)
            vendor_symbol = left.split("_str_")[-1].strip().lower()
            if vendor_symbol not in chunk_map:
                continue
            fields = right.strip().strip('";').split(",")
            if len(fields) < 32:
                continue
            market, code, symbol, sec_type = chunk_map[vendor_symbol]
            price = self._finite_float(fields[3])
            volume_raw = self._finite_float(fields[8])
            if price is None or price <= 0 or volume_raw is None or volume_raw < 0:
                continue
            open_p, pre_close, high_p, low_p = (self._finite_float(fields[i]) for i in (1, 2, 4, 5))
            amount = self._finite_float(fields[9])  # 新浪快照的已知成交额字段
            unit = 10.0 if sec_type == "convertible" else 100.0
            if sec_type == "index":
                # 指数源字段的成交量原始单位没有在此适配器中验证，不臆造股/手换算。
                raw_volume, hand_volume = float("nan"), float("nan")
            else:
                raw_volume, hand_volume = int(volume_raw), volume_raw / unit
            book = {}
            for level, vol_i, price_i, side in (
                (1, 10, 11, "bid"), (2, 12, 13, "bid"), (3, 14, 15, "bid"), (4, 16, 17, "bid"), (5, 18, 19, "bid"),
                (1, 20, 21, "ask"), (2, 22, 23, "ask"), (3, 24, 25, "ask"), (4, 26, 27, "ask"), (5, 28, 29, "ask"),
            ):
                book[f"{side}{level}_v"] = self._finite_float(fields[vol_i])
                book[f"{side}{level}_p"] = self._finite_float(fields[price_i])
            date_text, time_text = fields[30].strip(), fields[31].strip()
            data_date = self._data_date(date_text)
            time_valid = self._valid_snapshot_time(data_date, time_text)
            change = price - pre_close if pre_close is not None and pre_close > 0 else float("nan")
            rows.append({
                "market": market, "code": code, "symbol": symbol, "sec_type": sec_type, "name": fields[0].strip(),
                "price": price, "change": change,
                "pct_change": change / pre_close * 100 if pre_close is not None and pre_close > 0 else float("nan"),
                "open": open_p, "high": high_p, "low": low_p, "pre_close": pre_close,
                "up_limit": float("nan"), "down_limit": float("nan"), "limit_count": float("nan"),
                "volume_raw": raw_volume, "volume_hand": hand_volume,
                "amount_yuan": amount, "amount_wan": amount / 10000 if amount is not None else float("nan"),
                **book, "time": f"{date_text} {time_text}".strip(), "source": "sina_hq", "received_at": received_at,
                "data_date": data_date, "data_valid": time_valid,
                "invalid_reason": None if time_valid else self._snapshot_invalid_reason(data_date, time_text),
                "amount_status": "provider_field_9" if amount is not None else "missing_or_invalid_provider_field_9",
            })
        return rows, []

    def get_realtime_tick(self, codes: Union[str, List[str]], chunk_size: int = 60,
                          on_error: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
        """获取批量快照；attrs 中明确返回缺失标的和源错误。"""
        self._checked_int("chunk_size", chunk_size, 200)
        if isinstance(codes, str):
            raw_codes = [code.strip() for code in codes.split(",") if code.strip()]
        else:
            try:
                raw_codes = list(codes)
            except TypeError as exc:
                raise ValueError("codes 必须是字符串或代码列表") from exc
        if not raw_codes:
            return self._empty_ticks()
        norm_map = {}
        for raw_code in raw_codes:
            market, code, symbol, sec_type = self.normalize_security(raw_code)
            norm_map[symbol] = (market, code, symbol, sec_type)
        requested = list(norm_map)
        chunks = [{key: norm_map[key] for key in requested[start:start + chunk_size]}
                  for start in range(0, len(requested), chunk_size)]
        rows, errors = [], []
        if len(chunks) == 1:
            chunk_rows, chunk_errors = self._fetch_single_chunk(chunks[0], on_error)
            rows.extend(chunk_rows); errors.extend(chunk_errors)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(chunks))) as executor:
                futures = [executor.submit(self._fetch_single_chunk, chunk, on_error) for chunk in chunks]
                for future in futures:
                    try:
                        chunk_rows, chunk_errors = future.result()
                    except Exception as exc:
                        message = f"sina_hq 分片处理失败 ({type(exc).__name__})"
                        self._notify_error(on_error, message)
                        chunk_rows, chunk_errors = [], [message]
                    rows.extend(chunk_rows); errors.extend(chunk_errors)
        if not rows:
            return self._empty_ticks(requested, errors)
        frame = pd.DataFrame(rows, columns=self.TICK_COLUMNS)
        returned = set(frame["symbol"])
        frame.attrs.update({"source": "sina_hq", "requested_count": len(requested), "returned_count": len(frame),
                            "missing_symbols": [symbol for symbol in requested if symbol not in returned],
                            "source_errors": errors})
        return frame

    def get_kline(self, code: str, period: str = "1", count: int = 240,
                  on_error: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
        """获取腾讯 K 线；日/周优先前复权，分钟线绝不回退到日线。"""
        key = str(period).lower().strip()
        if key not in self.SUPPORTED_PERIODS:
            raise ValueError(f"不支持的 K 线周期: {period!r}；有效值: {sorted(self.SUPPORTED_PERIODS)}")
        self._checked_int("count", count, 1000)
        k_type = self.SUPPORTED_PERIODS[key]
        _, code, symbol, sec_type = self.normalize_security(code)
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{k_type},,,{count},qfq"
               if k_type in ("day", "week")
               else f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},{k_type},,{count}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=3.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            message = f"tencent_gtimg K线请求失败 ({type(exc).__name__})"
            self._notify_error(on_error, message)
            return self._empty_klines([message])
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            message = "tencent_gtimg K线响应缺少有效 data 对象"
            self._notify_error(on_error, message)
            return self._empty_klines([message])
        if symbol not in data:
            message = "tencent_gtimg K线响应未包含请求标的"
            self._notify_error(on_error, message)
            return self._empty_klines([message])
        stock = data[symbol]
        adjustment = "none"
        if not isinstance(stock, dict):
            message = "tencent_gtimg K线标的 payload 不是对象"
            self._notify_error(on_error, message)
            return self._empty_klines([message])
        if k_type in ("day", "week"):
            qfq_key = f"qfq{k_type}"
            if qfq_key in stock:
                lines, adjustment = stock[qfq_key], "qfq"
            elif k_type in stock:
                lines, adjustment = stock[k_type], "unadjusted"
            else:
                lines = []
        else:
            lines = stock.get(k_type, [])  # 不允许 m1/m5 回退到 day
        if not isinstance(lines, list) or not lines:
            return self._empty_klines()

        received_at, parsed = self._now(), []
        for item in lines:
            if not isinstance(item, list) or len(item) < 6:
                continue
            raw_dt = str(item[0]).strip()
            if re.fullmatch(r"\d{12}", raw_dt):
                try:
                    parsed_dt = dt.datetime.strptime(raw_dt, "%Y%m%d%H%M")
                except ValueError:
                    continue
                date_value = parsed_dt.date().isoformat()
                datetime_value = parsed_dt.strftime("%Y-%m-%d %H:%M")
            elif re.fullmatch(r"\d{8}", raw_dt):
                date_value = self._data_date(raw_dt)
                datetime_value = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]}"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_dt):
                date_value = self._data_date(raw_dt)
                datetime_value = raw_dt
            else:
                continue
            open_p, close_p, high_p, low_p = (self._finite_float(item[i]) for i in range(1, 5))
            volume_hand = self._finite_float(item[5])
            if (date_value is None or volume_hand is None or volume_hand < 0
                    or any(x is None or x <= 0 for x in (open_p, close_p, high_p, low_p))
                    or low_p > min(open_p, close_p) or high_p < max(open_p, close_p) or low_p > high_p):
                continue
            # 腾讯 item[5] 是“手”。指数的原始单位口径未核实，绝不乘以 100 假装为股。
            unit = 10.0 if sec_type == "convertible" else 100.0
            raw_volume = float("nan") if sec_type == "index" else volume_hand * unit
            parsed.append({
                "code": code, "symbol": symbol, "sec_type": sec_type, "datetime": datetime_value,
                "open": open_p, "close": close_p, "high": high_p, "low": low_p,
                "volume_raw": raw_volume, "volume_hand": volume_hand,
                # item[8] 未有已验证成交额契约；不能以 close*volume 伪造成交额。
                "amount_yuan": float("nan"), "source": "tencent_gtimg", "received_at": received_at,
                "data_date": date_value, "adjustment": adjustment, "amount_status": self._AMOUNT_MISSING, "data_valid": True,
            })
        if not parsed:
            return self._empty_klines()
        frame = pd.DataFrame(parsed, columns=self.KLINE_COLUMNS)
        frame.attrs.update({"source": "tencent_gtimg", "adjustment": adjustment, "returned_count": len(frame), "source_errors": []})
        return frame

    @classmethod
    def _delta_signature(cls, row: Dict) -> Tuple:
        """价格、成交量/额及五档任一价格或数量变化都会触发 delta。"""
        keys = ["price", "volume_raw", "amount_yuan"]
        for side in ("bid", "ask"):
            for level in range(1, 6):
                keys.extend((f"{side}{level}_p", f"{side}{level}_v"))
        return tuple(cls._finite_float(row.get(key)) for key in keys)

    def start_stream(self, codes: List[str], on_tick: Optional[Callable[[dict], None]] = None,
                     on_batch: Optional[Callable[[pd.DataFrame], None]] = None, interval: float = 0.5,
                     only_delta: bool = True, on_error: Optional[Callable[[str], None]] = None) -> bool:
        """启动后台轮询，返回是否真正启动；on_batch 是每轮快照而非新行情计数。"""
        interval_value = self._finite_float(interval)
        if interval_value is None or not 0 < interval_value <= 3600:
            raise ValueError("interval 必须是大于 0 且不超过 3600 秒的有限数")
        try:
            symbols = [self.normalize_security(raw_code)[2] for raw_code in list(codes)]
        except TypeError as exc:
            raise ValueError("codes 必须是代码列表") from exc
        if not symbols:
            raise ValueError("流监听标的不能为空")
        with self._stream_lock:
            if self._stream_thread is not None and self._stream_thread.is_alive():
                logger.warning("行情轮询仍在运行或停止中，旧线程退出前不能重启。")
                return False
            self._last_tick_cache.clear()
            stop_event = threading.Event()  # 每次生命周期独立 Event，杜绝旧线程复活。
            self._stream_stop_event, self._streaming = stop_event, True

            def worker():
                try:
                    while not stop_event.is_set():
                        frame = self.get_realtime_tick(symbols, on_error=on_error)
                        if stop_event.is_set():
                            break
                        if not frame.empty and on_batch is not None:
                            try:
                                on_batch(frame)
                            except Exception as exc:
                                self._notify_error(on_error, f"on_batch 回调异常 ({type(exc).__name__})")
                        if not frame.empty and on_tick is not None:
                            for row in frame.to_dict(orient="records"):
                                if stop_event.is_set():
                                    break
                                signature, symbol = self._delta_signature(row), row["symbol"]
                                if only_delta and self._last_tick_cache.get(symbol) == signature:
                                    continue
                                if only_delta:
                                    self._last_tick_cache[symbol] = signature
                                try:
                                    on_tick(row)
                                except Exception as exc:
                                    self._notify_error(on_error, f"on_tick 回调异常 ({type(exc).__name__})")
                        stop_event.wait(interval_value)
                except Exception as exc:
                    self._notify_error(on_error, f"行情轮询异常 ({type(exc).__name__})")
                finally:
                    with self._stream_lock:
                        if self._stream_stop_event is stop_event:
                            self._streaming = False
                            self._stream_thread = None
                            self._stream_stop_event = None
                            self._last_tick_cache.clear()

            thread = threading.Thread(target=worker, daemon=True, name="KTRealtimeStream")
            self._stream_thread = thread
            thread.start()
        logger.info("行情轮询已启动 (标的数: %d, 间隔: %gs)。", len(symbols), interval_value)
        return True

    def stop_stream(self, timeout: float = 5.0) -> bool:
        """请求停止；仅线程已退出时返回 True。支持在回调中安全调用。"""
        timeout_value = self._finite_float(timeout)
        if timeout_value is None or not 0 <= timeout_value <= 60:
            raise ValueError("timeout 必须是 0 到 60 秒的有限数")
        with self._stream_lock:
            thread, event = self._stream_thread, self._stream_stop_event
            if thread is None:
                self._streaming = False
                self._last_tick_cache.clear()
                logger.info("行情轮询未运行。")
                return True
            if event is not None:
                event.set()
            self._streaming = False
        if thread is threading.current_thread():
            logger.info("已在回调线程内请求停止；当前回调返回后退出。")
            return False
        thread.join(timeout=timeout_value)
        stopped = not thread.is_alive()
        if stopped:
            with self._stream_lock:
                # 旧 worker 的 finally 已清理自己的缓存。仅当仍是同一生命周期时
                # 才允许这里清理，避免与刚启动的新轮询竞争。
                if self._stream_stop_event is event:
                    self._last_tick_cache.clear()
            logger.info("行情轮询已停止。")
        else:
            logger.warning("停止请求已发出，但线程尚未退出；退出前禁止重启。")
        return stopped


# ═══════════════════════════════════════════════════════════════════
# 项目适配层: KT 快照 → data.realtime.RealtimeQuote
# ═══════════════════════════════════════════════════════════════════

_default_client: Optional[KTRealtimeClient] = None
_client_lock = threading.Lock()


def get_kt_client() -> KTRealtimeClient:
    """进程级共享客户端（快照请求无共享可变状态，线程安全）。"""
    global _default_client
    with _client_lock:
        if _default_client is None:
            _default_client = KTRealtimeClient()
        return _default_client


def _nz(value, default: float = 0.0) -> float:
    """NaN/None → default，其余转 float。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def get_realtime_quotes(codes) -> List["RealtimeQuote"]:
    """批量获取实时快照，返回项目通用 RealtimeQuote 列表。

    - 时间戳校验失败 (data_valid=False) 的快照不进入执行链
    - 新浪源无 pe/turnover/market_cap，填 0；依赖 pe 的估值逻辑
      请继续使用 data.realtime.get_realtime（腾讯源）
    - 成交额统一为万元口径（与腾讯源 RealtimeQuote.amount 一致）
    - 全部代码解析或请求失败时返回空列表，由调用方决定回退
    """
    from data.realtime import RealtimeQuote  # 延迟导入避免循环依赖

    try:
        frame = get_kt_client().get_realtime_tick(codes)
    except Exception as exc:
        logger.warning("KT 快照获取失败: %s", exc)
        return []
    missing = frame.attrs.get("missing_symbols") or []
    errors = frame.attrs.get("source_errors") or []
    if missing:
        logger.debug("KT 快照缺失标的: %s", missing)
    if errors:
        logger.debug("KT 快照源错误: %s", errors)
    if frame.empty:
        return []

    quotes = []
    for row in frame.to_dict(orient="records"):
        if not row.get("data_valid"):
            logger.debug("KT 快照时间戳无效，跳过: %s (%s)", row.get("symbol"), row.get("invalid_reason"))
            continue
        quotes.append(RealtimeQuote(
            code=str(row.get("code") or ""),
            name=str(row.get("name") or ""),
            price=_nz(row.get("price")),
            open=_nz(row.get("open")),
            close_prev=_nz(row.get("pre_close")),
            high=_nz(row.get("high")),
            low=_nz(row.get("low")),
            volume=int(_nz(row.get("volume_hand"))),
            amount=_nz(row.get("amount_yuan")) / 10000.0,  # 元 → 万元
            change=_nz(row.get("change")),
            change_pct=_nz(row.get("pct_change")),
            turnover=0.0,
            pe=0.0,
            market_cap=0.0,
            timestamp=str(row.get("time") or ""),
        ))
    return quotes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = get_kt_client()
    pool = ["600000", "113042", "sh000001", "sz399001", "920002"]
    started = time.perf_counter()
    ticks = client.get_realtime_tick(pool)
    print(f"请求 {len(pool)} 个，返回 {len(ticks)} 个，耗时 {(time.perf_counter()-started)*1000:.1f} ms")
    if not ticks.empty:
        print(ticks[["symbol", "name", "sec_type", "price", "data_valid"]].to_string(index=False))
    for quote in get_realtime_quotes(["600000"]):
        print(f"适配层: {quote.code} {quote.name} price={quote.price} amount(万)={quote.amount:.1f}")
