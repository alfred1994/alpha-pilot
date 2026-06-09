"""
涨停洗盘策略

逻辑：前一日涨停 + 次日中子形态 + 量能均线条件
源自通达信公式，核心思路是捕捉涨停后的洗盘结束信号。

信号条件:
1. 前一日涨停（涨幅≥9.8% 且 收盘=最高）
2. 当日中子形态（实体振幅≤3%，带上下影线）
3. 量不超涨停日3倍
4. 均线上穿或价格在趋势均线上方
5. 短期均量上穿长期均量，且当日量不超长期均量2倍
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy, Signal


class ZTReversalStrategy(BaseStrategy):
    """
    涨停洗盘策略

    逻辑：前一日涨停 + 次日中子形态 + 量能均线条件
    源自通达信公式，核心思路是捕捉涨停后的洗盘结束信号。
    """

    name = "涨停洗盘"
    version = "1.0"
    params = {
        "zt_pct": 0.098,           # 涨停阈值（9.8%）
        "body_pct": 0.03,          # 中子实体最大振幅（3%）
        "vol_ratio_max": 3.0,      # 次日量/涨停日量 最大倍数
        "ma_short": 5,             # 短期均线
        "ma_long": 25,             # 长期均线
        "ma_trend": 28,            # 趋势均线
        "vol_ma_short": 5,         # 短期均量
        "vol_ma_long": 60,         # 长期均量
        "vol_ma_ratio": 2.0,       # 量比上限
    }

    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        """
        生成交易信号

        Args:
            code: 股票代码
            df: K线DataFrame，包含 date,open,high,low,close,volume

        Returns:
            Signal 对象
        """
        # 数据校验
        if df is None or len(df) < 60:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        # 复制并排序
        df = df.copy().sort_values("date").reset_index(drop=True)

        # 提取价格和成交量
        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        p = self.params

        # ── 计算条件 ──

        # 1. 涨停：涨幅≥9.8% 且 收盘=最高
        pct_chg = close / close.shift(1) - 1
        zt = (pct_chg >= p["zt_pct"]) & (close >= high * 0.999)

        # 2. 中子形态：实体振幅≤3%，带上下影线
        body = (open_ - close).abs() / close
        has_shadow_up = high > pd.concat([open_, close], axis=1).max(axis=1)
        has_shadow_low = low < pd.concat([open_, close], axis=1).min(axis=1)
        spinning_top = (body <= p["body_pct"]) & has_shadow_up & has_shadow_low

        # 3. 量不超3倍
        vol_ok = volume <= volume.shift(1) * p["vol_ratio_max"]

        # 4. 均线上穿
        ma_short = close.rolling(p["ma_short"]).mean()
        ma_long = close.rolling(p["ma_long"]).mean()
        ma_trend = close.rolling(p["ma_trend"]).mean()
        ma_cross = (ma_short > ma_long) | (close >= ma_trend)

        # 5. 量上穿无巨量
        vol_ma_short = volume.rolling(p["vol_ma_short"]).mean()
        vol_ma_long = volume.rolling(p["vol_ma_long"]).mean()
        vol_cross = (vol_ma_short > vol_ma_long) & (volume <= vol_ma_long * p["vol_ma_ratio"])

        # ── 综合信号 ──
        buy_signal = zt.shift(1).fillna(False) & spinning_top & vol_ok & ma_cross & vol_cross

        # 取最后一个交易日的信号
        last_idx = len(df) - 1
        last_date = df["date"].iloc[last_idx]

        if buy_signal.iloc[last_idx]:
            # 计算信号强度
            score = 80

            # 信号强度加成
            if close.iloc[last_idx] >= ma_trend.iloc[last_idx]:
                score += 10
            if vol_cross.iloc[last_idx]:
                score += 10

            # 计算附加信息
            zt_date = df["date"].iloc[last_idx - 1] if last_idx > 0 else ""
            body_pct = float(body.iloc[last_idx]) if not pd.isna(body.iloc[last_idx]) else 0
            vol_ratio = float(volume.iloc[last_idx] / volume.iloc[last_idx - 1]) if volume.iloc[last_idx - 1] > 0 else 0

            return Signal(
                date=last_date,
                code=code,
                action="BUY",
                score=min(score, 100),
                reason=f"涨停洗盘: 昨涨停+今十字星+缩量+均线多头",
                metadata={
                    "zt_date": zt_date,
                    "body_pct": body_pct,
                    "vol_ratio": vol_ratio,
                    "ma_short": float(ma_short.iloc[last_idx]) if not pd.isna(ma_short.iloc[last_idx]) else 0,
                    "ma_long": float(ma_long.iloc[last_idx]) if not pd.isna(ma_long.iloc[last_idx]) else 0,
                    "ma_trend": float(ma_trend.iloc[last_idx]) if not pd.isna(ma_trend.iloc[last_idx]) else 0,
                }
            )

        # 无信号
        return Signal(
            date=last_date,
            code=code,
            action="HOLD",
            score=0,
            reason="无信号",
            metadata={
                "conditions": {
                    "zt_yesterday": bool(zt.shift(1).fillna(False).iloc[last_idx]),
                    "spinning_top": bool(spinning_top.iloc[last_idx]),
                    "vol_ok": bool(vol_ok.iloc[last_idx]),
                    "ma_cross": bool(ma_cross.iloc[last_idx]),
                    "vol_cross": bool(vol_cross.iloc[last_idx]),
                }
            }
        )
