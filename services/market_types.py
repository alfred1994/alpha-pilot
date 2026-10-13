"""多市场模拟盘的金额、报价与交易时段契约。

所有金额使用 Decimal，时间使用带时区的 UTC。此模块只计算模拟成交，
不会修改账户，也不会向任何券商或交易所发送订单。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from functools import lru_cache
from typing import Any, Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
MAX_QUOTE_CLOCK_SKEW = timedelta(seconds=1)


class MarketError(ValueError):
    """可向客户端公开的稳定错误，不包含 provider 响应、凭据或内部路径。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Market(str, Enum):
    CN = "CN"
    US = "US"
    CRYPTO = "CRYPTO"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class QuoteStatus(str, Enum):
    LIVE = "live"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


def parse_market(value: Any) -> Market:
    try:
        return value if isinstance(value, Market) else Market(str(value).strip().upper())
    except ValueError:
        raise MarketError("unsupported_market", "暂不支持该市场") from None


def parse_side(value: Any) -> Side:
    try:
        return value if isinstance(value, Side) else Side(str(value).strip().upper())
    except ValueError:
        raise MarketError("invalid_side", "交易方向必须为 BUY 或 SELL") from None


def utc_datetime(value: Optional[datetime] = None) -> datetime:
    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise MarketError("invalid_timestamp", "时间必须包含明确时区")
    return current.astimezone(UTC)


def decimal_value(value: Any, *, field: str = "数值", positive: bool = True) -> Decimal:
    """限制输入长度和数量级，防止 NaN、Infinity 及巨大指数进入金额运算。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MarketError("invalid_number", f"{field}必须为有限数值")
    text = str(value)
    if len(text) > 64:
        raise MarketError("invalid_number", f"{field}超出允许范围")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise MarketError("invalid_number", f"{field}必须为有限数值") from None
    if (not number.is_finite() or number.adjusted() > 12
            or number.as_tuple().exponent < -18 or (positive and number <= 0)):
        raise MarketError("invalid_number", f"{field}必须为允许范围内的有限正数")
    return number


def _iso(value: Optional[datetime]) -> Optional[str]:
    return utc_datetime(value).isoformat().replace("+00:00", "Z") if value is not None else None


@dataclass(frozen=True)
class Instrument:
    market: Market
    symbol: str
    name: str
    currency: str
    quantity_step: Decimal
    price_tick: Decimal
    allow_t0: bool
    min_notional: Decimal = Decimal("1")

    @property
    def instrument_id(self) -> str:
        return f"{self.market.value}:{self.symbol}"

    def to_dict(self) -> dict:
        return {
            "id": self.instrument_id, "instrument_id": self.instrument_id,
            "market": self.market.value, "symbol": self.symbol, "name": self.name,
            "currency": self.currency, "quantity_step": str(self.quantity_step),
            "price_tick": str(self.price_tick), "allow_t0": self.allow_t0,
            "min_notional": str(self.min_notional),
        }


@dataclass(frozen=True)
class MarketSpec:
    market: Market
    name: str
    currency: str
    timezone: str
    calendar: str
    max_quote_age_seconds: int
    commission_rate: Decimal
    minimum_commission: Decimal
    slippage_rate: Decimal
    cash_settlement_days: int

    def to_dict(self) -> dict:
        return {
            "market": self.market.value, "name": self.name, "currency": self.currency,
            "timezone": self.timezone, "calendar": self.calendar,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "commission_rate": str(self.commission_rate),
            "minimum_commission": str(self.minimum_commission),
            "slippage_rate": str(self.slippage_rate),
            "cash_settlement_days": self.cash_settlement_days,
            "short_selling": False, "leverage": False, "mode": "paper",
            "fee_note": "平台模拟费率，仅用于研究，不代表券商或交易所实际收费",
        }


_SPECS = {
    Market.CN: MarketSpec(Market.CN, "A股 ETF", "CNY", "Asia/Shanghai", "existing_cn",
                          300, Decimal("0.0003"), Decimal("5"), Decimal("0.0005"), 1),
    Market.US: MarketSpec(Market.US, "美股 ETF", "USD", "America/New_York", "XNYS",
                          60, Decimal("0.0005"), Decimal("0.01"), Decimal("0.0005"), 1),
    Market.CRYPTO: MarketSpec(Market.CRYPTO, "加密现货", "USD", "UTC", "24/7",
                              30, Decimal("0.006"), Decimal("0.01"), Decimal("0.001"), 0),
}

_INSTRUMENTS = (
    Instrument(Market.CN, "510300", "沪深300 ETF", "CNY", Decimal("100"), Decimal("0.001"), False),
    Instrument(Market.CN, "510500", "中证500 ETF", "CNY", Decimal("100"), Decimal("0.001"), False),
    Instrument(Market.CN, "159915", "创业板 ETF", "CNY", Decimal("100"), Decimal("0.001"), False),
    Instrument(Market.CN, "588000", "科创50 ETF", "CNY", Decimal("100"), Decimal("0.001"), False),
    Instrument(Market.US, "SPY", "SPDR S&P 500 ETF", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.US, "QQQ", "Invesco QQQ ETF", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.US, "IWM", "iShares Russell 2000 ETF", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.US, "DIA", "SPDR Dow Jones ETF", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.US, "TLT", "iShares 20+ Year Treasury ETF", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.US, "GLD", "SPDR Gold Shares", "USD", Decimal("1"), Decimal("0.01"), True),
    Instrument(Market.CRYPTO, "BTC-USD", "Bitcoin / USD", "USD", Decimal("0.00000001"), Decimal("0.01"), True),
    Instrument(Market.CRYPTO, "ETH-USD", "Ethereum / USD", "USD", Decimal("0.00000001"), Decimal("0.01"), True),
)
_INSTRUMENT_LOOKUP = {(item.market, item.symbol): item for item in _INSTRUMENTS}


def get_market_spec(market: Any) -> MarketSpec:
    return _SPECS[parse_market(market)]


def list_instruments(market: Any = None) -> tuple[Instrument, ...]:
    selected = parse_market(market) if market is not None else None
    return tuple(item for item in _INSTRUMENTS if selected is None or item.market == selected)


def get_instrument(market: Any, symbol: str) -> Instrument:
    selected = parse_market(market)
    if not isinstance(symbol, str) or len(symbol) > 20:
        raise MarketError("unsupported_instrument", "标的不在当前模拟盘支持列表中")
    canonical = symbol.strip().upper()
    if selected == Market.CRYPTO:
        canonical = canonical.replace("/", "-")
    instrument = _INSTRUMENT_LOOKUP.get((selected, canonical))
    if instrument is None:
        raise MarketError("unsupported_instrument", "标的不在当前模拟盘支持列表中")
    return instrument


def validate_quantity(instrument: Instrument, value: Any) -> Decimal:
    quantity = decimal_value(value, field="交易数量")
    if quantity > Decimal("1000000000") or quantity % instrument.quantity_step:
        raise MarketError("invalid_quantity", f"交易数量须为 {instrument.quantity_step} 的正整数倍")
    return quantity.quantize(instrument.quantity_step)


@dataclass(frozen=True)
class MarketSession:
    market: Market
    is_open: bool
    reason: str
    timezone: str
    opens_at: Optional[datetime] = None
    closes_at: Optional[datetime] = None
    status: str = "closed"

    def to_dict(self) -> dict:
        return {
            "market": self.market.value, "is_open": self.is_open, "reason": self.reason,
            "timezone": self.timezone, "opens_at": _iso(self.opens_at),
            "closes_at": _iso(self.closes_at), "status": self.status,
        }


@lru_cache(maxsize=6)
def _us_calendar(year: int):
    try:
        import exchange_calendars
        return exchange_calendars.get_calendar(
            "XNYS", start=f"{year - 1}-01-01", end=f"{year + 1}-12-31"
        )
    except Exception:
        raise MarketError("calendar_unavailable", "美股交易日历暂不可用，暂停模拟成交") from None


def us_session_bounds(day: date) -> Optional[tuple[datetime, datetime]]:
    calendar = _us_calendar(day.year)
    try:
        label = day.isoformat()
        if not calendar.is_session(label):
            return None
        return (calendar.session_open(label).to_pydatetime().astimezone(UTC),
                calendar.session_close(label).to_pydatetime().astimezone(UTC))
    except Exception:
        raise MarketError("calendar_unavailable", "美股交易日历暂不可用，暂停模拟成交") from None


def _cn_trading_day(day: date) -> bool:
    # 复用原调度器已加载的权威日历或本地节假表，不在网页请求中启动慢速日历抓取。
    from scheduler import market_calendar as calendar
    cache = calendar._trading_dates_cache
    if cache is not None and calendar._cache_date == str(day.year):
        return day.strftime("%Y%m%d") in cache
    holidays = calendar._HOLIDAY_WEEKDAYS.get(day.year)
    if holidays is None:
        raise MarketError("calendar_unavailable", "A股当前年份交易日历不可用，暂停模拟成交")
    return day.weekday() < 5 and day.strftime("%m%d") not in holidays


def market_session(market: Any, now: Optional[datetime] = None) -> MarketSession:
    selected = parse_market(market)
    current = utc_datetime(now)
    spec = get_market_spec(selected)
    if selected == Market.CRYPTO:
        return MarketSession(selected, True, "现货市场全天开放", spec.timezone, status="open")
    try:
        local = current.astimezone(ZoneInfo(spec.timezone))
        if selected == Market.US:
            bounds = us_session_bounds(local.date())
            if bounds is None:
                return MarketSession(selected, False, "周末或交易所节假日", spec.timezone)
            opens_at, closes_at = bounds
            opened = opens_at <= current < closes_at
            return MarketSession(selected, opened, "常规交易时段" if opened else "常规交易时段之外",
                                 spec.timezone, opens_at, closes_at, "open" if opened else "closed")
        if not _cn_trading_day(local.date()):
            return MarketSession(selected, False, "周末或交易所节假日", spec.timezone)
        opens_at = datetime.combine(local.date(), time(9, 30), local.tzinfo).astimezone(UTC)
        closes_at = datetime.combine(local.date(), time(15), local.tzinfo).astimezone(UTC)
        clock_time = local.time().replace(tzinfo=None)
        opened = time(9, 30) <= clock_time < time(11, 30) or time(13) <= clock_time < time(15)
        reason = "常规交易时段" if opened else "午间休市" if time(11, 30) <= clock_time < time(13) else "常规交易时段之外"
        return MarketSession(selected, opened, reason, spec.timezone, opens_at, closes_at,
                             "open" if opened else "closed")
    except (MarketError, KeyError):
        return MarketSession(selected, False, "交易日历不可用，暂停模拟成交", spec.timezone, status="unavailable")


def settlement_available_at(instrument: Instrument, now: Optional[datetime] = None) -> datetime:
    """美股卖出款保守冻结至下一交易日开盘；A股和现货交易现金即时复用。"""
    current = utc_datetime(now)
    if instrument.market != Market.US:
        return current
    local_date = current.astimezone(ZoneInfo("America/New_York")).date()
    for offset in range(1, 15):
        bounds = us_session_bounds(local_date + timedelta(days=offset))
        if bounds is not None:
            return bounds[0]
    raise MarketError("calendar_unavailable", "无法确认美股下一结算交易日")


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    price: Decimal
    as_of: datetime
    received_at: datetime
    source: str
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    status: QuoteStatus = QuoteStatus.LIVE
    reason: str = ""
    minimum_notional: Optional[Decimal] = None
    quantity_increment: Optional[Decimal] = None
    price_increment: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    limit_up: Optional[Decimal] = None
    limit_down: Optional[Decimal] = None

    @property
    def clock_skew_seconds(self) -> float:
        """行情源领先本地接收时钟的秒数，不修改来源时间或新鲜度基准。"""
        return max(0.0, (utc_datetime(self.as_of) - utc_datetime(self.received_at)).total_seconds())

    def to_dict(self) -> dict:
        return {
            "instrument_id": self.instrument.instrument_id,
            "market": self.instrument.market.value, "symbol": self.instrument.symbol,
            "currency": self.instrument.currency, "price": str(self.price),
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "as_of": _iso(self.as_of), "received_at": _iso(self.received_at),
            "clock_skew_seconds": self.clock_skew_seconds,
            "source": self.source, "status": self.status.value, "reason": self.reason,
            "minimum_notional": str(self.minimum_notional) if self.minimum_notional is not None else None,
            "quantity_increment": str(self.quantity_increment) if self.quantity_increment is not None else None,
            "price_increment": str(self.price_increment) if self.price_increment is not None else None,
            "volume": str(self.volume) if self.volume is not None else None,
        }


@dataclass(frozen=True)
class Bar:
    # as_of 是日线结束时刻，而非开盘/供应商抓取时刻，策略只能消费已结束日线。
    as_of: datetime
    close: Decimal

    def to_dict(self) -> dict:
        return {"as_of": _iso(self.as_of), "close": str(self.close)}


def validate_quote(quote: Quote, *, instrument: Optional[Instrument] = None,
                   now: Optional[datetime] = None) -> Quote:
    """公共源实测存在亚秒级跨主机时钟偏差，三组时间差分别最多容忍 1 秒。

    保留来源和接收时间，绝不以 now 替换；容差不叠加，也不延长各市场的
    30/60/300 秒新鲜度阈值。超过边界的未来报价仍然拒绝。
    """
    current = utc_datetime(now)
    expected = instrument or quote.instrument
    if quote.instrument != expected:
        raise MarketError("quote_mismatch", "行情标的与订单不匹配")
    if quote.status != QuoteStatus.LIVE:
        raise MarketError("quote_unavailable", "当前行情不可用于模拟成交")
    decimal_value(quote.price, field="行情价格")
    for value in (quote.bid, quote.ask):
        if value is not None:
            decimal_value(value, field="买卖盘价格")
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        raise MarketError("invalid_quote", "买卖盘价格倒挂，暂不支持模拟成交")
    if not isinstance(quote.as_of, datetime) or not isinstance(quote.received_at, datetime):
        raise MarketError("invalid_timestamp", "行情必须包含来源时间和接收时间")
    as_of = utc_datetime(quote.as_of)
    received_at = utc_datetime(quote.received_at)
    if max(as_of - current, received_at - current, as_of - received_at) > MAX_QUOTE_CLOCK_SKEW:
        raise MarketError("future_quote", "行情时间晚于当前时间，暂停模拟成交")
    if (current - as_of).total_seconds() > get_market_spec(expected.market).max_quote_age_seconds:
        raise MarketError("stale_quote", "行情已过期，请等待新的实时行情")
    if quote.volume is not None and decimal_value(quote.volume, positive=False) <= 0:
        raise MarketError("no_liquidity", "当前行情没有有效成交量")
    return quote


@dataclass(frozen=True)
class FillEstimate:
    instrument: Instrument
    side: Side
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    total: Decimal
    currency: str

    def to_dict(self) -> dict:
        return {"instrument_id": self.instrument.instrument_id, "side": self.side.value,
                "quantity": str(self.quantity), "price": str(self.price),
                "notional": str(self.notional), "fee": str(self.fee),
                "total": str(self.total), "currency": self.currency, "mode": "paper"}


def estimate_fill(instrument: Instrument, side: Any, quantity: Any, quote: Quote,
                  now: Optional[datetime] = None) -> FillEstimate:
    """模拟单边滑点和费用；余额、持仓、T+1 及幂等性由账户事务层负责。"""
    canonical = get_instrument(instrument.market, instrument.symbol)
    if canonical != instrument:
        raise MarketError("invalid_instrument", "标的规则与平台定义不一致")
    current = utc_datetime(now)
    direction = parse_side(side)
    amount = validate_quantity(instrument, quantity)
    validate_quote(quote, instrument=instrument, now=current)
    session = market_session(instrument.market, current)
    if not session.is_open:
        raise MarketError("calendar_unavailable" if session.status == "unavailable" else "market_closed", session.reason)
    if quote.quantity_increment is not None and amount % decimal_value(quote.quantity_increment):
        raise MarketError("invalid_quantity", "交易数量不符合行情源当前交易精度")
    spec = get_market_spec(instrument.market)
    buying = direction == Side.BUY
    if buying and quote.limit_up is not None and quote.price >= quote.limit_up:
        raise MarketError("price_limit", "标的已到涨停价格，暂停模拟买入")
    if not buying and quote.limit_down is not None and quote.price <= quote.limit_down:
        raise MarketError("price_limit", "标的已到跌停价格，暂停模拟卖出")
    reference = (quote.ask if buying else quote.bid) or quote.price
    tick = max(instrument.price_tick, decimal_value(quote.price_increment)) if quote.price_increment is not None else instrument.price_tick
    slipped = reference * (Decimal("1") + spec.slippage_rate if buying else Decimal("1") - spec.slippage_rate)
    rounding = ROUND_CEILING if buying else ROUND_FLOOR
    price = (slipped / tick).to_integral_value(rounding=rounding) * tick
    decimal_value(price, field="模拟成交价")
    if ((quote.limit_up is not None and price > quote.limit_up)
            or (quote.limit_down is not None and price < quote.limit_down)):
        raise MarketError("price_limit", "模拟滑点后价格超过涨跌停范围，暂缓成交")
    raw_notional = price * amount
    minimum = max(instrument.min_notional, decimal_value(quote.minimum_notional)) if quote.minimum_notional is not None else instrument.min_notional
    if raw_notional < minimum:
        raise MarketError("minimum_notional", f"订单金额须不低于 {minimum} {instrument.currency}")
    # 买入金额向上、卖出金额向下取分，避免小数现货通过舍入获得额外现金。
    notional = raw_notional.quantize(Decimal("0.01"), rounding=rounding)
    fee = max(notional * spec.commission_rate, spec.minimum_commission).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    total = notional + fee if buying else notional - fee
    if total <= 0:
        raise MarketError("minimum_notional", "订单金额不足以覆盖模拟费用")
    return FillEstimate(instrument, direction, amount, price, notional, fee, total, instrument.currency)
