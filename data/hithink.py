"""同花顺 Fuyao 研究数据客户端；不用于证明逐股交易行情的新鲜度。

合同来源 https://fuyao.aicubes.cn/llms-full.txt 。金额元、成交量股；
财务披露时间原样保留，调用方必须按披露日做 point-in-time 筛选。
"""
import math
import os
import re
import threading
import time
from datetime import date, datetime, time as day_time, timedelta, timezone

import pandas as pd
import requests


class HiThinkError(RuntimeError):
    """安全错误消息不包含请求头、服务端自由文本或原始异常。"""


class HiThinkUnavailable(HiThinkError):
    pass


def normalize_code(code):
    value = str(code).strip().upper()
    prefix = re.fullmatch(r"(SH|SZ|BJ)\.?([0-9]{6})", value)
    if prefix:
        return f"{prefix[2]}.{prefix[1]}"
    if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value):
        return value
    if re.fullmatch(r"[0-9]{6}", value):
        if value.startswith(("4", "8", "92")):
            return value + ".BJ"
        if value.startswith("6"):
            return value + ".SH"
        if value.startswith(("0", "3")):
            return value + ".SZ"
    raise ValueError("unsupported A-share code; use explicit six-digit exchange suffix")


_TZ = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
_DAILY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _date_ms(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, day_time())
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        parsed = datetime.strptime(value, "%Y-%m-%d")
    else:
        raise ValueError("date must be YYYY-MM-DD or a date/datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ)
    return int(parsed.timestamp() * 1000)


def _window(start, end):
    start_ms, end_ms = _date_ms(start), _date_ms(end)
    # Conservative bound: the server defines a maximum ten-year window.
    if not 0 <= end_ms - start_ms <= 3650 * 86400000:
        raise ValueError("date window must be ordered and at most 3650 days")
    return {"start": start_ms, "end": end_ms}


class HiThinkClient:
    BASE_URL = "https://fuyao.aicubes.cn"

    def __init__(self, api_key=None, *, timeout=15, min_interval=1.0, max_retries=2):
        self._api_key = (api_key if api_key is not None else
                         os.getenv("HITHINK_FINANCE_API_KEY") or os.getenv("FUYAO_API_KEY", "")).strip()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("min_interval must be nonnegative")
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between zero and three")
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries

    @property
    def available(self):
        return bool(self._api_key)

    def _request(self, path, params=None):
        global _LAST_REQUEST
        if not self.available:
            raise HiThinkUnavailable("HiThink API key is not configured")
        # Serialize requests across instances, including network time and backoff.
        with _LOCK:
            for attempt in range(self.max_retries + 1):
                time.sleep(max(0, self.min_interval - (time.monotonic() - _LAST_REQUEST)))
                try:
                    response = requests.get(
                        self.BASE_URL + path, params=params or {},
                        headers={"X-api-key": self._api_key}, timeout=self.timeout,
                        allow_redirects=False,
                    )
                except requests.RequestException:
                    raise HiThinkError("HiThink transport failure") from None
                finally:
                    _LAST_REQUEST = time.monotonic()
                limited = response.status_code == 429
                retry_after = response.headers.get("Retry-After")
                try:
                    if not limited and response.status_code != 200:
                        raise HiThinkError(f"HiThink HTTP status {response.status_code}")
                    body = None if limited else response.json()
                except (ValueError, TypeError):
                    raise HiThinkError("HiThink invalid JSON response") from None
                finally:
                    response.close()
                if not limited:
                    if not isinstance(body, dict) or type(body.get("code")) is not int:
                        raise HiThinkError("HiThink invalid response envelope")
                    limited = body["code"] == 4001
                if limited:
                    if attempt == self.max_retries:
                        raise HiThinkError("HiThink rate limit exhausted")
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = 0
                    # Never retry sooner than Retry-After; fail instead of an unbounded wait.
                    if not math.isfinite(wait) or wait > 60:
                        raise HiThinkError("HiThink rate limit requires later retry")
                    time.sleep(max(2 ** (attempt + 1), wait))
                    continue
                if body["code"] != 0:
                    raise HiThinkError(f"HiThink business code {body['code']}")
                if not isinstance(body.get("data"), dict):
                    raise HiThinkError("HiThink invalid data container")
                return body["data"]

    @staticmethod
    def _items(data):
        items = data.get("item")
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            raise HiThinkError("HiThink invalid item list")
        return items

    def _batch(self, path, codes):
        if isinstance(codes, str):
            codes = [codes]
        codes = list(dict.fromkeys(normalize_code(code) for code in codes))
        items, batches = [], []
        for offset in range(0, len(codes), 100):
            batch = codes[offset:offset + 100]
            data = self._request(path, {"thscodes": ",".join(batch)})
            rows = self._items(data)
            if any(row.get("thscode") not in batch for row in rows):
                raise HiThinkError("HiThink unexpected symbol in response")
            items.extend(rows)
            batches.append({"codes": batch, "latest_upstream_timestamp_ms": data.get("timestamp")})
        return {"source": "hithink", "item": items, "batches": batches,
                "missing_codes": [code for code in codes if code not in {r.get('thscode') for r in items}],
                "per_symbol_timestamp_available": False}

    def get_snapshot(self, codes):
        """原始快照，整批时间只留在 batches，不能作为单股执行时效证明。"""
        return self._batch("/api/a-share/prices/snapshot", codes)

    def get_snapshot_page(self, *, limit=100, offset=0):
        """全市场单页观察数据；调用方显式控制页数预算。"""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("snapshot page size must be 1..100 and offset nonnegative")
        data = self._request("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        return {"source": "hithink", "item": self._items(data), "total": data.get("total"),
                "batches": [{"offset": offset, "latest_upstream_timestamp_ms": data.get("timestamp")}],
                "per_symbol_timestamp_available": False}

    def get_valuations(self, codes):
        return self._batch("/api/a-share/valuations/snapshot", codes)

    def get_daily(self, code, start_date, end_date, adjust="qfq"):
        adjustments = {"qfq": "forward", "hfq": "backward", "none": "none",
                       "forward": "forward", "backward": "backward"}
        if adjust not in adjustments:
            raise ValueError("unsupported adjustment")
        params = dict(thscode=normalize_code(code), interval="1d", adjust=adjustments[adjust],
                      **_window(start_date, end_date))
        data = self._request("/api/a-share/prices/historical", params)
        result, rejected = [], 0
        for row in self._items(data):
            try:
                stamp = row["date_ms"]
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                    raise ValueError()
                if not params["start"] <= stamp <= params["end"]:
                    raise ValueError()
                raw_prices = [row[key + "_price"] for key in ("open", "high", "low", "close")]
                if any(isinstance(value, bool) for value in raw_prices):
                    raise ValueError()
                values = [float(value) for value in raw_prices]
                opening, high, low, close = values
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError()
                if high < max(opening, low, close) or low > min(opening, high, close):
                    raise ValueError()
                optional = []
                for key in ("volume", "turnover"):
                    value = row.get(key)
                    try:
                        value = float(value) if value is not None and not isinstance(value, bool) else float("nan")
                    except (TypeError, ValueError, OverflowError):
                        value = float("nan")
                    if not math.isfinite(value) or value < 0:
                        value = float("nan")
                    optional.append(value)
                day = datetime.fromtimestamp(stamp / 1000, _TZ).strftime("%Y-%m-%d")
                result.append([day, *values, *optional])
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                rejected += 1
        frame = pd.DataFrame(result, columns=_DAILY_COLUMNS)
        if frame["date"].duplicated().any():
            raise HiThinkError("HiThink duplicate daily bars")
        frame = frame.sort_values("date").reset_index(drop=True)
        frame.attrs.update(source="hithink", adjust=params["adjust"], volume_unit="shares",
                           amount_unit="CNY", rejected_rows=rejected,
                           upstream_timestamp_ms=data.get("timestamp"))
        return frame

    def get_financials(self, code, statement="income-statements", period="annual", *,
                       limit=None, start_date=None, end_date=None):
        if statement not in ("income-statements", "balance-sheets", "cash-flow-statements"):
            raise ValueError("unsupported financial statement")
        if period not in ("annual", "quarterly"):
            raise ValueError("unsupported report period")
        params = {"thscode": normalize_code(code), "period": period}
        if start_date is not None or end_date is not None:
            if start_date is None or end_date is None or limit is not None:
                raise ValueError("financial date range requires both dates and no limit")
            params.update(_window(start_date, end_date))
        else:
            limit = 4 if limit is None else limit
            if type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("financial limit must be between one and twenty")
            params["limit"] = limit
        data = self._request("/api/a-share/financials/" + statement, params)
        self._items(data)
        return data

    def get_indicators(self, code, report):
        if not isinstance(report, str) or not re.fullmatch(r"[0-9]{4}-[1-4]", report):
            raise ValueError("report must be YYYY-quarter")
        return self._request("/api/a-share/financials/indicators",
                             {"thscode": normalize_code(code), "report": report})

    def get_tickers(self, *, asset_type="a-share", limit=1000, offset=0):
        if asset_type not in ("a-share", "a-share-index", "fund-otc", "fund-etf", "fund-lof"):
            raise ValueError("unsupported asset type")
        if type(limit) is not int or not 1 <= limit <= 10000 or type(offset) is not int or offset < 0:
            raise ValueError("invalid pagination")
        data = self._request("/api/meta/tickers/list", dict(asset_type=asset_type, limit=limit, offset=offset))
        self._items(data)
        return data

    def get_market_dump(self, kind="daily-k-10d"):
        """仅获取短期有效下载链接元信息，不下载或执行任何内容。"""
        if kind not in ("daily-k", "daily-k-10d", "adjustment-factors"):
            raise ValueError("unsupported market dump")
        return self._request(f"/api/dump/market-dumps/{kind}/download-url")


def get_client():
    """显式开关且已配置凭据时可用；不触发网络访问。"""
    if os.getenv("HITHINK_ENABLED", "0").strip() != "1":
        return None
    client = HiThinkClient()
    return client if client.available else None
