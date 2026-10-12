"""多因子研究组合的内存账本；不连接 Broker 或正式模拟账户。"""
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExecutionConfig:
    capital: float = 100_000
    max_positions: int = 5
    max_weight: float = 0.2
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    slippage: float = 0.001
    mode: str = "strict"
    price_tick: float = 0.01

    def validate(self):
        if not np.isfinite(self.capital) or self.capital <= 0:
            raise ValueError("初始资金必须有限且为正")
        if not isinstance(self.max_positions, int) or isinstance(self.max_positions, bool) or self.max_positions < 1:
            raise ValueError("最大持仓数必须为正整数")
        if not np.isfinite(self.max_weight) or not 0 < self.max_weight <= 1:
            raise ValueError("单股目标权重必须在 (0, 1] 内")
        if any(not np.isfinite(v) or not 0 <= v < 1 for v in (self.commission, self.stamp_tax, self.slippage)):
            raise ValueError("费用与滑点率必须在 [0, 1) 内")
        if self.commission + self.stamp_tax >= 1 or not np.isfinite(self.min_commission) or self.min_commission < 0:
            raise ValueError("佣金/印花税参数不合法")
        if self.mode not in ("strict", "research"):
            raise ValueError("成交模式必须是 strict 或 research")
        if not np.isfinite(self.price_tick) or self.price_tick <= 0:
            raise ValueError("价格最小单位必须有限且为正")


def _positive(value):
    return value is not None and pd.notna(value) and np.isfinite(float(value)) and float(value) > 0


def _price(row, field, strict):
    if row is None:
        return None
    raw = row.get("raw_" + field)
    if _positive(raw):
        return float(raw)
    value = row.get(field)
    return float(value) if not strict and _positive(value) else None


def _cent(value, tick=0.01):
    unit = Decimal(str(tick))
    return float((Decimal(str(value)) / unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * unit)


def summarize_curve(curve, trades, capital):
    equity = np.array([capital] + [item["equity"] for item in curve], dtype=float)
    returns = equity[1:] / equity[:-1] - 1
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    return {
        "total_return": float(equity[-1] / capital - 1),
        "max_drawdown": float((equity / np.maximum.accumulate(equity) - 1).min()),
        "sharpe_252_observations": float(returns.mean() / std * np.sqrt(252)) if std > 1e-12 else None,
        "trade_count": len(trades),
        "commission": float(sum(t["commission"] for t in trades)),
        "stamp_tax": float(sum(t["stamp_tax"] for t in trades)),
        "slippage_cost": float(sum(t["slippage_cost"] for t in trades)),
        "mean_daily_turnover": float(np.mean([d["turnover"] for d in curve])) if curve else 0.0,
        "ending_equity": float(equity[-1]),
    }


def run_portfolio(panel, targets, config=None):
    """targets: {信号日: {code:目标权重}}。信号日收盘定数量，下一开盘成交。

    不足一手留现金；先卖后买；失败订单逐日重试直到下个调仓目标覆盖。
    缺失行情的持仓按最后已知收盘价估值并明确记录，不虚构成交或期末清仓。
    """
    config = config or ExecutionConfig()
    config.validate()
    strict = config.mode == "strict"
    rows = {(pd.Timestamp(r["date"]), str(r["code"])): r for r in panel.to_dict("records")}
    dates = sorted(pd.to_datetime(panel["date"].unique()))
    target_map = {pd.Timestamp(date): weights for date, weights in targets.items()}
    if not target_map or not set(target_map).issubset(dates):
        raise ValueError("组合目标必须包含面板中的信号日期")
    for weights in target_map.values():
        if (len(weights) > config.max_positions
                or any(not np.isfinite(w) or not 0 < w <= config.max_weight + 1e-12 for w in weights.values())
                or sum(weights.values()) > 1 + 1e-12):
            raise ValueError("目标超过持仓数、单股权重或总资金限制")
    cash = float(config.capital)
    lots, last_close, pending = {}, {}, {}
    trades, blocked, curve = [], [], []
    first_signal = min(target_map)
    active_signal = None

    def shares(code):
        return sum(lot[1] for lot in lots.get(code, []))

    def reject(date, code, side, reason):
        blocked.append({"date": date.strftime("%Y-%m-%d"), "code": code, "side": side, "reason": reason})

    for index, date in enumerate(dates):
        if date < first_signal:
            continue
        previous_date = dates[index - 1] if index else None
        previous_equity = cash + sum(shares(code) * last_close[code] for code in lots)
        if previous_date in target_map:
            active_signal = previous_date
            pending = {}
            for code, weight in target_map[previous_date].items():
                signal_row = rows.get((previous_date, code))
                price = _price(signal_row, "close", strict)
                if price is None:
                    reject(date, code, "BUY", "missing_signal_price")
                    pending[code] = shares(code)
                    continue
                pending[code] = int(previous_equity * weight / price / 100) * 100
                if pending[code] == 0 and shares(code) == 0:
                    reject(date, code, "BUY", "target_budget_below_one_lot")
            for code in lots:
                pending.setdefault(code, 0)

        day_notional = 0.0
        # 目标数量固定在信号时点，不能用当日收盘/全天成交量决定开盘订单大小。
        for side in ("SELL", "BUY"):
            ordered = sorted(pending, key=lambda c: (-target_map.get(active_signal, {}).get(c, 0), c))
            for code in ordered:
                current = shares(code)
                delta = pending[code] - current
                quantity = -delta if side == "SELL" else delta
                if quantity <= 0:
                    continue
                row = rows.get((date, code))
                if row is None:
                    reject(date, code, side, "missing_bar")
                    continue
                if float(row.get("volume", 0)) <= 0 or row.get("tradable") in (False, 0):
                    reject(date, code, side, "suspended")
                    continue
                opening = _price(row, "open", strict)
                upper, lower = row.get("limit_up"), row.get("limit_down")
                if strict and (opening is None or not _positive(row.get("raw_close"))
                               or row.get("tradable") not in (True, 1)
                               or not _positive(upper) or not _positive(lower)):
                    reject(date, code, side, "missing_execution_metadata")
                    continue
                if opening is None:
                    reject(date, code, side, "missing_open")
                    continue
                if not _positive(upper) or not _positive(lower):
                    reference = last_close.get(code)
                    if reference is None:
                        reject(date, code, side, "missing_limit_reference")
                        continue
                    upper, lower = _cent(reference * 1.1, config.price_tick), _cent(reference * 0.9, config.price_tick)
                upper, lower = float(upper), float(lower)
                tolerance = config.price_tick / 2
                if lower >= upper or not lower - tolerance <= opening <= upper + tolerance:
                    reject(date, code, side, "invalid_price_limits")
                    continue
                price = opening * (1 + config.slippage if side == "BUY" else 1 - config.slippage)
                if side == "BUY" and (opening >= upper - tolerance or price > upper):
                    reject(date, code, side, "limit_up")
                    continue
                if side == "SELL" and (opening <= lower + tolerance or price < lower):
                    reject(date, code, side, "limit_down")
                    continue
                if side == "SELL":
                    available = sum(q for bought, q in lots[code] if bought < date)
                    if available < quantity:
                        reject(date, code, side, "t_plus_one")
                    quantity = min(quantity, available)
                else:
                    if current == 0 and len(lots) >= config.max_positions:
                        reject(date, code, side, "position_slots_locked")
                        continue
                    affordable = max(0, int(cash / (price * (1 + config.commission)) / 100) * 100)
                    quantity = min(quantity, affordable)
                    while quantity > 0 and quantity * price + max(config.min_commission, quantity * price * config.commission) > cash + 1e-9:
                        quantity -= 100
                    if quantity <= 0:
                        reject(date, code, side, "insufficient_cash_or_lot")
                if quantity <= 0:
                    continue
                notional = quantity * price
                commission = max(config.min_commission, notional * config.commission)
                tax = notional * config.stamp_tax if side == "SELL" else 0.0
                cash_change = notional - commission - tax if side == "SELL" else -notional - commission
                if side == "SELL":
                    remaining = quantity
                    retained = []
                    for bought, count in lots[code]:
                        sold = min(count, remaining) if bought < date else 0
                        remaining -= sold
                        if count > sold:
                            retained.append((bought, count - sold))
                    if retained:
                        lots[code] = retained
                    else:
                        del lots[code]
                else:
                    lots.setdefault(code, []).append((date, quantity))
                cash += cash_change
                day_notional += notional
                trades.append({
                    "date": date.strftime("%Y-%m-%d"), "signal_date": active_signal.strftime("%Y-%m-%d"),
                    "code": code, "side": side, "shares": quantity, "price": price,
                    "notional": notional, "commission": commission, "stamp_tax": tax,
                    "slippage_cost": abs(price - opening) * quantity, "cash_change": cash_change,
                })
        stale = []
        for code in panel["code"].unique():
            closing = _price(rows.get((date, code)), "close", strict)
            if closing is not None:
                last_close[code] = closing
            elif code in lots:
                stale.append(code)
        if date == first_signal:
            continue
        holdings = {code: {"shares": shares(code), "mark": last_close[code],
                           "value": shares(code) * last_close[code]} for code in sorted(lots)}
        equity = cash + sum(p["value"] for p in holdings.values())
        if cash < -1e-7 or equity <= 0:
            raise AssertionError("研究账本现金或净值不合法")
        curve.append({"date": date.strftime("%Y-%m-%d"), "cash": cash, "equity": equity,
                      "turnover": day_notional / previous_equity,
                      "holdings": holdings, "stale_marks": stale})
    return {"config": asdict(config), "metrics": summarize_curve(curve, trades, config.capital),
            "curve": curve, "trades": trades, "blocked_orders": blocked,
            "blocked_counts": dict(Counter(item["reason"] for item in blocked)),
            "ending_positions": curve[-1]["holdings"] if curve else {},
            "end_valuation": "mark_to_market_no_forced_liquidation"}
