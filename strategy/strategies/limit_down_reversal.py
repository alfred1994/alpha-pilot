"""
跌停反转策略（情绪反转类）
====================================================================
逻辑：连续跌停后出现止跌信号（缩量十字星或阳线）
高风险高回报，抄底反弹行情
====================================================================
"""
import pandas as pd
import numpy as np
from strategy.strategies.base import BaseStrategy, Signal, board_limit_pct


class LimitDownReversalStrategy(BaseStrategy):
    """跌停反转策略"""

    name = "跌停反转"
    version = "1.0"
    params = {
        "limit_down_pct": None,     # 跌停阈值；None=按板块自适应(主板-9.5%/创业科创-19.5%/北交-29.5%)
        "min_down_days": 2,         # 最少连续跌停天数
        "max_down_days": 5,         # 最多连续跌停天数（超过不抄底）
        "vol_shrink": 0.5,          # 止跌日成交量需低于前日的50%（缩量）
        "body_pct": 0.03,           # 止跌日实体振幅≤3%（十字星）
        "recovery_pct": 0.02,       # 止跌日涨幅需≥+2%（确认止跌回升）
    }

    def generate_signals(self, code, df, **kwargs):
        if df is None or len(df) < 30:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        df = df.copy().sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        o = df["open"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        v = df["volume"].astype(float)
        p = self.params

        # 日涨跌幅
        pct = c / c.shift(1) - 1

        # 跌停判断（阈值按板块自适应）
        limit_down_pct = p.get("limit_down_pct")
        if limit_down_pct is None:
            limit_down_pct = -(board_limit_pct(code, kwargs.get("name", "")) - 0.005)
        is_limit_down = pct <= limit_down_pct

        # 连续跌停天数（从今天往前数）
        consecutive = pd.Series(0, index=df.index, dtype=int)
        for i in range(1, len(df)):
            if is_limit_down.iloc[i]:
                consecutive.iloc[i] = consecutive.iloc[i - 1] + 1

        # 前N天有连续跌停
        had_limit_down = consecutive.shift(1) >= p["min_down_days"]
        not_too_long = consecutive.shift(1) <= p["max_down_days"]

        # 止跌信号
        # 1. 缩量
        vol_shrink = v < v.shift(1) * p["vol_shrink"]
        # 2. 小实体（十字星）
        body = (o - c).abs() / c
        small_body = body <= p["body_pct"]
        # 3. 没有继续跌停
        not_down = pct >= p["recovery_pct"]

        buy_signal = had_limit_down & not_too_long & (vol_shrink | small_body) & not_down

        idx = len(df) - 1
        last_date = df["date"].iloc[idx]

        if buy_signal.iloc[idx]:
            down_days = int(consecutive.shift(1).iloc[idx])
            score = 65 + min(down_days * 5, 20)  # 跌停天数越多反弹空间越大
            return Signal(
                date=last_date, code=code, action="BUY",
                score=min(score, 100),
                reason=f"跌停反转: 连续{down_days}个跌停后止跌缩量",
                metadata={"down_days": down_days},
            )

        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="无信号")
