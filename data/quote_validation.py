"""交易与实时传感器共用的行情输入校验。"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_MAX_QUOTE_AGE_SECONDS = int(os.environ.get("MAX_EXECUTION_QUOTE_AGE_SECONDS", "300"))


@dataclass(frozen=True)
class QuoteValidation:
    valid: bool
    reason: str = ""
    price: float = 0.0
    timestamp: Optional[datetime] = None


def normalize_quote_code(value: Any) -> str:
    """提取行情代码中的六位证券代码，无法识别时返回空串。"""
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def parse_quote_timestamp(value: Any) -> Optional[datetime]:
    """解析项目两条行情源及长桥常见的时间格式，统一为北京时间。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%Y%m%d%H%M%S", "%Y%m%d%H%M",
        ):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def validate_quote(
    quote: Any,
    *,
    expected_code: str = "",
    now: datetime = None,
    max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    allow_historical: bool = False,
) -> QuoteValidation:
    """验证标的、价格和时间；历史回放必须由调用方显式开启。"""
    if isinstance(quote, dict):
        raw_price = quote.get("price")
        raw_code = quote.get("code")
        raw_timestamp = quote.get("timestamp")
    else:
        raw_price = getattr(quote, "price", None)
        raw_code = getattr(quote, "code", None)
        raw_timestamp = getattr(quote, "timestamp", None)
    if isinstance(raw_price, bool):
        return QuoteValidation(False, "行情价格必须为正的有限数")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return QuoteValidation(False, "行情价格缺失或非法")
    if not math.isfinite(price) or price <= 0:
        return QuoteValidation(False, "行情价格必须为正的有限数")

    actual_code = normalize_quote_code(raw_code)
    expected = normalize_quote_code(expected_code)
    if expected and actual_code and actual_code != expected:
        return QuoteValidation(False, f"行情标的不匹配: 期望{expected}，收到{actual_code}")
    if expected and not actual_code and not allow_historical:
        return QuoteValidation(False, "行情缺少可校验标的代码")

    timestamp = parse_quote_timestamp(raw_timestamp)
    if timestamp is None:
        if allow_historical:
            return QuoteValidation(True, price=price)
        return QuoteValidation(False, "行情时间戳缺失或非法")
    if allow_historical:
        return QuoteValidation(True, price=price, timestamp=timestamp)
    if (isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0):
        return QuoteValidation(False, "行情新鲜度阈值非法")
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    else:
        current = current.astimezone(BEIJING_TZ)
    age_seconds = (current - timestamp).total_seconds()
    if age_seconds < -30:
        return QuoteValidation(False, "行情时间戳超过当前时间")
    if age_seconds > float(max_age_seconds):
        return QuoteValidation(False, f"行情已过期({age_seconds:.0f}秒)")
    return QuoteValidation(True, price=price, timestamp=timestamp)
