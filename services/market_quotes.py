"""只读公共行情适配：Alpaca IEX 美股、Coinbase Exchange 美元现货和既有 A 股源。

接口白名单固定在代码中，不接受用户提供的域名或 URL。没有交易、转账、
账户余额等请求；缺配置、格式异常、超出时钟容差的未来或过期报价均明确失败。
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

import requests

from services.market_types import (
    UTC, Bar, Instrument, Market, MarketError, Quote, decimal_value,
    get_instrument, us_session_bounds, utc_datetime, validate_quote,
)


ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks"
COINBASE_DATA_URL = "https://api.exchange.coinbase.com/products"


def _provider_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MarketError("invalid_quote", "行情源缺少可信时间戳")
    try:
        return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        raise MarketError("invalid_quote", "行情源时间戳格式不正确") from None


def _utc_string(value: datetime) -> str:
    return utc_datetime(value).isoformat().replace("+00:00", "Z")


class QuoteService:
    """可注入 transport/clock 的行情服务；默认单次请求超时 8 秒且不自动重试。"""

    def __init__(self, *, session=None, environ: Optional[Mapping[str, str]] = None,
                 timeout: float = 8.0, clock: Optional[Callable[[], datetime]] = None):
        if isinstance(timeout, bool) or not 0 < timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        self.session = session if session is not None else requests.Session()
        self.environ = os.environ if environ is None else environ
        self.timeout = timeout
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._products: dict[str, tuple[datetime, dict]] = {}

    def _now(self, now: Optional[datetime]) -> datetime:
        return utc_datetime(now if now is not None else self.clock())

    def _json(self, url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None):
        try:
            response = self.session.get(
                url, headers=headers or {}, params=params or {},
                timeout=(min(3.0, self.timeout), self.timeout), allow_redirects=False,
            )
        except requests.RequestException:
            raise MarketError("provider_unavailable", "行情源暂时无法连接，请稍后重试") from None
        if response.status_code in (401, 403):
            raise MarketError("provider_unauthorized", "行情源权限或配置不可用")
        if response.status_code == 429:
            raise MarketError("provider_rate_limited", "行情源请求过于频繁，请稍后重试")
        if response.status_code != 200:
            raise MarketError("provider_unavailable", "行情源暂未返回可用数据")
        try:
            payload = response.json(parse_float=Decimal)
        except (ValueError, TypeError):
            raise MarketError("invalid_quote", "行情源响应格式不正确") from None
        if not isinstance(payload, (dict, list)):
            raise MarketError("invalid_quote", "行情源响应结构不正确")
        return payload

    def _alpaca_headers(self) -> dict:
        key = self.environ.get("ALPACA_DATA_KEY_ID", "").strip()
        secret = self.environ.get("ALPACA_DATA_SECRET_KEY", "").strip()
        if not key or not secret:
            raise MarketError("provider_not_configured", "美股行情服务尚未配置，模拟交易暂不可用")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _crypto_product(self, instrument: Instrument, current: datetime) -> dict:
        cached = self._products.get(instrument.symbol)
        if cached is not None and 0 <= (current - cached[0]).total_seconds() < 300:
            return cached[1]
        product = self._json(f"{COINBASE_DATA_URL}/{instrument.symbol}")
        if (not isinstance(product, dict) or product.get("id") != instrument.symbol
                or product.get("base_currency") != instrument.symbol.split("-")[0]
                or product.get("quote_currency") != "USD"):
            raise MarketError("quote_mismatch", "现货交易对元数据与请求不匹配")
        if (product.get("status") != "online" or product.get("trading_disabled") is not False
                or product.get("cancel_only") is not False or product.get("post_only") is not False
                or product.get("limit_only") is not False):
            raise MarketError("instrument_unavailable", "该现货交易对当前不支持市价模拟")
        rules = {
            "quantity_increment": decimal_value(product.get("base_increment")),
            "price_increment": decimal_value(product.get("quote_increment")),
            "minimum_notional": decimal_value(product.get("min_market_funds")),
        }
        # 如 provider 使用与固定平台步长不相容的精度，不擅自改变持仓契约。
        if (max(instrument.quantity_step, rules["quantity_increment"])
                % min(instrument.quantity_step, rules["quantity_increment"])):
            raise MarketError("instrument_unavailable", "该现货交易对精度暂不兼容")
        if max(instrument.price_tick, rules["price_increment"]) % min(instrument.price_tick, rules["price_increment"]):
            raise MarketError("instrument_unavailable", "该现货交易对报价精度暂不兼容")
        self._products[instrument.symbol] = (current, rules)
        return rules

    def get_quote(self, market: Any, symbol: str, now: Optional[datetime] = None) -> Quote:
        instrument = get_instrument(market, symbol)
        requested_at = self._now(now)
        if instrument.market == Market.CN:
            quote = self._cn_quote(instrument, now)
        elif instrument.market == Market.US:
            payload = self._json(
                f"{ALPACA_DATA_URL}/{instrument.symbol}/quotes/latest",
                headers=self._alpaca_headers(), params={"feed": "iex"},
            )
            if not isinstance(payload, dict) or payload.get("symbol") != instrument.symbol:
                raise MarketError("quote_mismatch", "美股行情标的与请求不匹配")
            value = payload.get("quote")
            if not isinstance(value, dict):
                raise MarketError("invalid_quote", "美股行情缺少报价数据")
            bid = decimal_value(value.get("bp"), field="买一价")
            ask = decimal_value(value.get("ap"), field="卖一价")
            decimal_value(value.get("bs"), field="买盘数量")
            decimal_value(value.get("as"), field="卖盘数量")
            quote = Quote(
                instrument, (bid + ask) / 2, _provider_time(value.get("t")),
                self._now(now), "alpaca_iex", bid=bid, ask=ask,
            )
        else:
            rules = self._crypto_product(instrument, requested_at)
            value = self._json(f"{COINBASE_DATA_URL}/{instrument.symbol}/ticker")
            if not isinstance(value, dict):
                raise MarketError("invalid_quote", "现货行情缺少报价数据")
            quote = Quote(
                instrument, decimal_value(value.get("price"), field="现货价格"),
                _provider_time(value.get("time")), self._now(now), "coinbase_exchange",
                bid=decimal_value(value.get("bid")), ask=decimal_value(value.get("ask")),
                **rules,
            )
        return validate_quote(quote, now=self._now(now))

    def _cn_quote(self, instrument: Instrument, now: Optional[datetime]) -> Quote:
        from data.quote_validation import parse_quote_timestamp
        from data.realtime import get_realtime
        try:
            records = get_realtime([instrument.symbol])
        except Exception:
            raise MarketError("provider_unavailable", "A股行情源暂不可用") from None
        if not records:
            raise MarketError("provider_unavailable", "A股行情源暂未返回可用数据")
        matching = [item for item in records if getattr(item, "code", None) == instrument.symbol]
        if len(matching) != 1:
            raise MarketError("quote_mismatch", "A股行情标的与请求不匹配")
        value = matching[0]
        stamp = parse_quote_timestamp(getattr(value, "timestamp", None))
        if stamp is None:
            raise MarketError("invalid_quote", "A股行情缺少可信时间戳")
        previous = decimal_value(getattr(value, "close_prev", None), field="昨收价")
        limit_pct = Decimal("0.20") if instrument.symbol in ("159915", "588000") else Decimal("0.10")
        return Quote(
            instrument, decimal_value(value.price, field="行情价格"), stamp.astimezone(UTC),
            self._now(now), "tencent", volume=decimal_value(getattr(value, "volume", None)),
            limit_up=(previous * (1 + limit_pct)).quantize(instrument.price_tick, rounding=ROUND_HALF_UP),
            limit_down=(previous * (1 - limit_pct)).quantize(instrument.price_tick, rounding=ROUND_HALF_UP),
        )

    def get_bars(self, market: Any, symbol: str, limit: int = 80,
                 now: Optional[datetime] = None) -> list[Bar]:
        """返回升序已结束日线；不会用未收盘 K 线或抓取时间冒充日线结束时间。"""
        instrument = get_instrument(market, symbol)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise MarketError("invalid_limit", "日线数量必须为 1 至 200")
        current = self._now(now)
        if instrument.market == Market.CN:
            raise MarketError("history_unavailable", "A股工作区日线策略尚未接入，当前支持实时行情模拟")
        if instrument.market == Market.US:
            return self._us_bars(instrument, limit, current)
        return self._crypto_bars(instrument, limit, current)

    def _us_bars(self, instrument: Instrument, limit: int, current: datetime) -> list[Bar]:
        headers = self._alpaca_headers()
        params = {"timeframe": "1Day", "start": _utc_string(current - timedelta(days=limit * 2 + 20)),
                  "end": _utc_string(current), "limit": min(limit + 10, 1000),
                  "feed": "iex", "adjustment": "split", "sort": "desc", "currency": "USD"}
        bars = []
        seen = set()
        page_tokens = set()
        # 官方允许单页少于 limit 但仍有下一页；读取足够日线后停止，最多 5 页。
        # https://docs.alpaca.markets/us/reference/stockbarsingle-1
        for _ in range(5):
            payload = self._json(f"{ALPACA_DATA_URL}/{instrument.symbol}/bars",
                                 headers=headers, params=dict(params))
            if (not isinstance(payload, dict) or payload.get("symbol") != instrument.symbol
                    or not isinstance(payload.get("bars"), list)
                    or payload.get("currency", "USD") != "USD"):
                raise MarketError("invalid_history", "美股日线数据结构不正确")
            for item in payload["bars"]:
                if not isinstance(item, dict):
                    raise MarketError("invalid_history", "美股日线数据格式不正确")
                stamp = _provider_time(item.get("t"))
                local = stamp.astimezone(ZoneInfo("America/New_York"))
                if local.time().replace(tzinfo=None) != time(0):
                    raise MarketError("invalid_history", "美股日线时间未对齐交易日")
                bounds = us_session_bounds(local.date())
                if bounds is None:
                    raise MarketError("invalid_history", "美股日线落在非交易日")
                ended_at = bounds[1]
                if stamp > current or ended_at > current:
                    continue
                close = decimal_value(item.get("c"), field="日线收盘价")
                if ended_at in seen:
                    raise MarketError("invalid_history", "美股日线存在重复日期")
                seen.add(ended_at)
                bars.append(Bar(ended_at, close))
            token = payload.get("next_page_token")
            if len(bars) >= limit or token is None:
                break
            if not isinstance(token, str) or not 1 <= len(token) <= 2048 or token in page_tokens:
                raise MarketError("invalid_history", "美股日线分页信息不正确")
            page_tokens.add(token)
            params["page_token"] = token
        else:
            raise MarketError("incomplete_history", "美股日线分页未完成，策略暂缓运行")
        return self._completed_bars(bars, limit, instrument, current)

    def _crypto_bars(self, instrument: Instrument, limit: int, current: datetime) -> list[Bar]:
        self._crypto_product(instrument, current)
        payload = self._json(
            f"{COINBASE_DATA_URL}/{instrument.symbol}/candles",
            params={"granularity": 86400, "start": _utc_string(current - timedelta(days=limit + 3)),
                    "end": _utc_string(current)},
        )
        if not isinstance(payload, list):
            raise MarketError("invalid_history", "现货日线数据结构不正确")
        bars = []
        seen = set()
        for item in payload:
            if not isinstance(item, list) or len(item) != 6:
                raise MarketError("invalid_history", "现货日线数据格式不正确")
            epoch = decimal_value(item[0], field="日线时间")
            if epoch != epoch.to_integral_value() or epoch % 86400:
                raise MarketError("invalid_history", "现货日线时间未对齐 UTC 日期")
            try:
                stamp = datetime.fromtimestamp(int(epoch), UTC)
            except (ValueError, OverflowError, OSError):
                raise MarketError("invalid_history", "现货日线时间不正确") from None
            ended_at = stamp + timedelta(days=1)
            if ended_at > current:
                continue
            close = decimal_value(item[4], field="日线收盘价")
            if ended_at in seen:
                raise MarketError("invalid_history", "现货日线存在重复日期")
            seen.add(ended_at)
            bars.append(Bar(ended_at, close))
        return self._completed_bars(bars, limit, instrument, current)

    @staticmethod
    def _completed_bars(bars: list[Bar], limit: int, instrument: Instrument,
                        current: datetime) -> list[Bar]:
        if not bars:
            raise MarketError("history_unavailable", "行情源未返回已结束日线")
        ordered = sorted(bars, key=lambda value: value.as_of)[-limit:]
        if instrument.market == Market.CRYPTO:
            latest = datetime.combine(current.date(), time(0), UTC)
            expected = [latest - timedelta(days=offset) for offset in range(len(ordered))]
        else:
            expected = []
            day = current.astimezone(ZoneInfo("America/New_York")).date()
            # 日历无覆盖或异常停市时有界失败；不把旧数据平移成最新日线。
            for offset in range(len(ordered) * 3 + 15):
                bounds = us_session_bounds(day - timedelta(days=offset))
                if bounds is not None and bounds[1] <= current:
                    expected.append(bounds[1])
                    if len(expected) == len(ordered):
                        break
        if not expected or ordered[-1].as_of != expected[0]:
            raise MarketError("stale_history", "最新已结束日线尚未就绪，策略暂缓运行")
        if len(expected) != len(ordered) or [bar.as_of for bar in reversed(ordered)] != expected:
            raise MarketError("incomplete_history", "日线序列存在缺口，策略暂缓运行")
        return ordered
