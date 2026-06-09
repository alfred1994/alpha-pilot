"""
双风格选股策略
=================================================================
风格A: 科技成长 — 半导体/CPO/PCB/AI算力产业链
风格B: 价值超跌 — 低估值 + 超跌反弹

同花顺公式 + Python实现，支持回测和实盘
=================================================================
"""
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime

import numpy as np
import pandas as pd

from strategy.ths_indicators import (
    MA, EMA, SMA, MACD, RSI, KDJ, BOLL, ATR, BIAS,
    VOL_MA, VOL_RATIO, OBV,
    CROSS, CROSS_DOWN, REF, HHV, LLV, COUNT, EVERY, EXIST,
    BARSLAST, HHVBARS
)

logger = logging.getLogger("strategy.dual_style")


# ════════════════════════════════════════════════════════════════
# 信号定义
# ════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    """交易信号"""
    code: str
    name: str
    style: str           # "growth" | "value"
    date: str
    score: float         # 信号强度 0-100
    reasons: List[str]   # 触发原因
    indicators: Dict     # 关键指标值


# ════════════════════════════════════════════════════════════════
# 风格A: 科技成长策略
# ════════════════════════════════════════════════════════════════
#
# ══ 同花顺公式 ══
#
# 【科技强势选股公式】
# {MACD金叉+放量+均线多头}
# DIF:=EMA(C,12)-EMA(C,26);
# DEA:=EMA(DIF,9);
# MACD金叉:=CROSS(DIF,DEA);
# MA5:=MA(C,5);
# MA10:=MA(C,10);
# MA20:=MA(C,20);
# 均线多头:=MA5>MA10 AND MA10>MA20;
# 量比:=V/MA(V,5);
# 放量:=量比>1.5;
# RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100;
# K:=SMA(RSV,3,1); D:=SMA(K,3,1); J:=3*K-2*D;
# KDJ金叉:=CROSS(K,D) AND J<80;
# 5日涨幅:=(C/REF(C,5)-1)*100;
# 强势:=5日涨幅>5 AND 5日涨幅<25;
# 选股:=MACD金叉 AND 均线多头 AND 放量 AND 强势;
#
# 【突破放量选股公式】
# {N日新高+量比突破}
# 新高:=C=HHV(C,20);
# 量比:=V/MA(V,10);
# 放量突破:=新高 AND 量比>2;
# RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100;
# K:=SMA(RSV,3,1); D:=SMA(K,3,1);
# KDJ健康:=K>50 AND D>40;
# 布林:=C>MA(C,20);
# 选股:=放量突破 AND KDJ健康 AND 布林;


def calc_growth_signals(df: pd.DataFrame, code: str, name: str = "") -> Optional[Signal]:
    """
    科技成长策略 — 信号计算

    同花顺公式翻译为Python实现

    入口条件（至少满足2条）:
    1. MACD金叉或DIF>0且向上
    2. 均线多头排列(MA5>MA10>MA20)
    3. 放量(量比>1.5)
    4. 5日涨幅5%-25%（强势但不追高）

    加分项:
    + 突破20日新高
    + KDJ金叉且J<80（未超买）
    + OBV创新高（资金持续流入）
    + 布林带中轨之上

    减分项:
    - RSI>80（严重超买）
    - 5日涨幅>25%（追高风险）
    - 换手率>20%（过度投机）
    """
    if len(df) < 30:
        return None

    close = df['close'].astype(float)
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    volume = df['volume'].astype(float)
    turn = df['turn'].astype(float) if 'turn' in df.columns else pd.Series(0, index=df.index)

    # === 指标计算 ===
    dif, dea, macd_hist = MACD(close)
    ma5 = MA(close, 5)
    ma10 = MA(close, 10)
    ma20 = MA(close, 20)
    rsi6 = RSI(close, 6)
    k, d, j = KDJ(high, low, close)
    vol_ratio = VOL_RATIO(volume, 5)
    boll_upper, boll_mid, boll_lower = BOLL(close, 20)
    obv = OBV(close, volume)
    bias20 = BIAS(close, 20)

    # === 最新一天的值 ===
    i = len(df) - 1
    cur_close = close.iloc[i]
    cur_ma5 = ma5.iloc[i]
    cur_ma10 = ma10.iloc[i]
    cur_ma20 = ma20.iloc[i]
    cur_dif = dif.iloc[i]
    cur_dea = dea.iloc[i]
    cur_rsi = rsi6.iloc[i]
    cur_k = k.iloc[i]
    cur_d = d.iloc[i]
    cur_j = j.iloc[i]
    cur_vr = vol_ratio.iloc[i]
    cur_turn = turn.iloc[i]
    cur_boll_mid = boll_mid.iloc[i]

    # 5日涨幅
    if i >= 5:
        pct_5d = (close.iloc[i] / close.iloc[i - 5] - 1) * 100
    else:
        pct_5d = 0

    # 20日新高
    high_20d = high.iloc[max(0, i - 19):i + 1].max()
    is_new_high = cur_close >= high_20d

    # MACD金叉（近3日内）
    macd_cross = False
    for j_idx in range(max(0, i - 2), i + 1):
        if j_idx > 0 and dif.iloc[j_idx] > dea.iloc[j_idx] and dif.iloc[j_idx - 1] <= dea.iloc[j_idx - 1]:
            macd_cross = True
            break

    # KDJ金叉（近3日内）
    kdj_cross = False
    for j_idx in range(max(0, i - 2), i + 1):
        if j_idx > 0 and k.iloc[j_idx] > d.iloc[j_idx] and k.iloc[j_idx - 1] <= d.iloc[j_idx - 1]:
            kdj_cross = True
            break

    # OBV趋势（近5日OBV是否上升）
    obv_rising = obv.iloc[i] > obv.iloc[max(0, i - 5)]

    # === 信号评分 ===
    score = 0
    reasons = []

    # 入口条件（每条15分，至少2条=30分起）
    if macd_cross or (cur_dif > 0 and dif.iloc[i] > dif.iloc[max(0, i - 1)]):
        score += 15
        reasons.append("MACD金叉/向上")

    if cur_ma5 > cur_ma10 > cur_ma20:
        score += 15
        reasons.append("均线多头排列")
    elif cur_ma5 > cur_ma10:
        score += 8
        reasons.append("短期均线多头")

    if cur_vr > 1.5:
        score += 15
        reasons.append(f"放量(量比{cur_vr:.1f})")

    if 5 < pct_5d < 25:
        score += 15
        reasons.append(f"5日涨{pct_5d:.1f}%")
    elif pct_5d > 25:
        score -= 10
        reasons.append(f"5日涨{pct_5d:.1f}%追高风险")

    # 加分项
    if is_new_high:
        score += 10
        reasons.append("突破20日新高")

    if kdj_cross and cur_j < 80:
        score += 10
        reasons.append("KDJ金叉未超买")

    if obv_rising:
        score += 5
        reasons.append("OBV资金流入")

    if cur_close > cur_boll_mid:
        score += 5
        reasons.append("布林中轨之上")

    # 减分项
    if cur_rsi > 80:
        score -= 15
        reasons.append(f"RSI超买({cur_rsi:.0f})")

    if cur_turn > 20:
        score -= 10
        reasons.append(f"换手率过高({cur_turn:.1f}%)")

    # 低于30分不出信号
    if score < 30:
        return None

    return Signal(
        code=code, name=name, style="growth", date=df.index[i] if isinstance(df.index[i], str) else str(df.index[i]),
        score=min(100, max(0, score)), reasons=reasons,
        indicators={
            "dif": round(cur_dif, 2), "dea": round(cur_dea, 2),
            "rsi": round(cur_rsi, 1), "k": round(cur_k, 1), "d": round(cur_d, 1), "j": round(cur_j, 1),
            "vol_ratio": round(cur_vr, 2), "pct_5d": round(pct_5d, 2),
            "ma5": round(cur_ma5, 2), "ma10": round(cur_ma10, 2), "ma20": round(cur_ma20, 2),
            "turnover": round(cur_turn, 2), "bias20": round(bias20.iloc[i], 2),
        }
    )


# ════════════════════════════════════════════════════════════════
# 风格B: 价值超跌策略
# ════════════════════════════════════════════════════════════════
#
# ══ 同花顺公式 ══
#
# 【超跌反弹选股公式】
# {RSI超跌+乖离率+止跌信号}
# RSI6:=SMA(MAX(C-REF(C,1),0),6,1)/SMA(ABS(C-REF(C,1)),6,1)*100;
# RSI超跌:=RSI6<25;
# BIAS20:=(C-MA(C,20))/MA(C,20)*100;
# 大幅偏离:=BIAS20<-15;
# 10日跌幅:=(C/REF(C,10)-1)*100;
# 近期大跌:=10日跌幅<-15;
# {止跌信号: 收阳线+下影线}
# 收阳:=C>O;
# 下影线:=(O-L)/(H-L+0.01);
# 止跌:=收阳 AND 下影线>0.3;
# {RSI底背离: 价格创新低但RSI不创新低}
# 价格新低:=C=LLV(C,20);
# RSI低:=RSI6=LLV(RSI6,20);
# 底背离:=价格新低 AND NOT(RSI低);
# 选股:=RSI超跌 AND (大幅偏离 OR 近期大跌) AND (止跌 OR 底背离);
#
# 【估值修复选股公式】
# {低估值+资金回流}
# RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100;
# K:=SMA(RSV,3,1); D:=SMA(K,3,1); J:=3*K-2*D;
# KDJ超卖:=J<20;
# BIAS20:=(C-MA(C,20))/MA(C,20)*100;
# 大偏离:=BIAS20<-12;
# 缩量企稳:=V<MA(V,5)*0.8 AND C>REF(C,1);
# MACD底背离:=CROSS(EMA(C,12)-EMA(C,26),REF(EMA(C,12)-EMA(C,26),1)) AND C<REF(C,5);
# 选股:=KDJ超卖 AND 大偏离 AND (缩量企稳 OR MACD底背离);


def calc_value_signals(df: pd.DataFrame, code: str, name: str = "") -> Optional[Signal]:
    """
    价值超跌策略 — 信号计算

    入口条件（至少满足2条）:
    1. RSI6 < 30（超卖区）
    2. 20日乖离率 < -12%（大幅偏离均线）
    3. 10日跌幅 > 15%（近期大跌）
    4. KDJ的J值 < 20（超卖）

    加分项:
    + 止跌信号（收阳+长下影线）
    + RSI底背离（价格新低但RSI不新低）
    + 缩量企稳（量缩价稳）
    + MACD底背离
    + 布林下轨附近（有支撑）

    减分项:
    - 连续3日以上阴线（趋势未止）
    - 10日跌幅>30%（可能有基本面问题）
    - 成交额过低（流动性差）
    """
    if len(df) < 30:
        return None

    close = df['close'].astype(float)
    open_price = df['open'].astype(float)
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    volume = df['volume'].astype(float)
    amount = df['amount'].astype(float) if 'amount' in df.columns else pd.Series(0, index=df.index)

    # === 指标计算 ===
    rsi6 = RSI(close, 6)
    rsi14 = RSI(close, 14)
    dif, dea, macd_hist = MACD(close)
    k, d, j = KDJ(high, low, close)
    bias20 = BIAS(close, 20)
    boll_upper, boll_mid, boll_lower = BOLL(close, 20)
    ma20 = MA(close, 20)

    i = len(df) - 1
    cur_close = close.iloc[i]
    cur_open = open_price.iloc[i]
    cur_high = high.iloc[i]
    cur_low = low.iloc[i]
    cur_rsi6 = rsi6.iloc[i]
    cur_rsi14 = rsi14.iloc[i]
    cur_k = k.iloc[i]
    cur_d = d.iloc[i]
    cur_j = j.iloc[i]
    cur_bias = bias20.iloc[i]
    cur_boll_lower = boll_lower.iloc[i]
    cur_boll_mid = boll_mid.iloc[i]

    # 10日跌幅
    if i >= 10:
        pct_10d = (close.iloc[i] / close.iloc[i - 10] - 1) * 100
    else:
        pct_10d = 0

    # 20日跌幅
    if i >= 20:
        pct_20d = (close.iloc[i] / close.iloc[i - 20] - 1) * 100
    else:
        pct_20d = 0

    # 止跌信号: 收阳 + 长下影线
    is_yang = cur_close > cur_open
    candle_range = cur_high - cur_low if cur_high != cur_low else 0.01
    lower_shadow = (min(cur_open, cur_close) - cur_low) / candle_range
    stop_loss_signal = is_yang and lower_shadow > 0.3

    # RSI底背离（20日内价格新低但RSI不新低）
    price_at_low = cur_close <= close.iloc[max(0, i - 19):i + 1].min() * 1.01
    rsi_not_at_low = cur_rsi6 > rsi6.iloc[max(0, i - 19):i + 1].min() + 3
    rsi_divergence = price_at_low and rsi_not_at_low

    # 缩量企稳
    avg_vol_5 = volume.iloc[max(0, i - 4):i + 1].mean()
    shrink_volume = volume.iloc[i] < avg_vol_5 * 0.8
    price_up = close.iloc[i] > close.iloc[i - 1]
    shrink_stable = shrink_volume and price_up

    # 连续阴线天数
    yin_count = 0
    for j_idx in range(i, max(i - 10, -1), -1):
        if close.iloc[j_idx] < open_price.iloc[j_idx]:
            yin_count += 1
        else:
            break

    # === 信号评分 ===
    score = 0
    reasons = []

    # 入口条件
    if cur_rsi6 < 30:
        score += 20
        reasons.append(f"RSI超卖({cur_rsi6:.0f})")
    elif cur_rsi6 < 40:
        score += 10
        reasons.append(f"RSI偏低({cur_rsi6:.0f})")

    if cur_bias < -12:
        score += 20
        reasons.append(f"乖离率偏离({cur_bias:.1f}%)")

    if pct_10d < -15:
        score += 15
        reasons.append(f"10日跌{pct_10d:.1f}%")
    elif pct_10d < -10:
        score += 8
        reasons.append(f"10日跌{pct_10d:.1f}%")

    if cur_j < 20:
        score += 15
        reasons.append(f"KDJ超卖(J={cur_j:.0f})")

    # 加分项
    if stop_loss_signal:
        score += 10
        reasons.append(f"止跌信号(下影线{lower_shadow:.0%})")

    if rsi_divergence:
        score += 15
        reasons.append("RSI底背离")

    if shrink_stable:
        score += 8
        reasons.append("缩量企稳")

    if cur_close < cur_boll_lower * 1.02:
        score += 8
        reasons.append("布林下轨附近")

    # 减分项
    if yin_count >= 3:
        score -= 10
        reasons.append(f"连续{yin_count}日阴线")

    if pct_10d < -30:
        score -= 15
        reasons.append(f"10日暴跌{pct_10d:.1f}%风险")

    if amount.iloc[i] < 5000_0000:  # 成交额<5000万
        score -= 10
        reasons.append("流动性不足")

    if score < 25:
        return None

    return Signal(
        code=code, name=name, style="value", date=df.index[i] if isinstance(df.index[i], str) else str(df.index[i]),
        score=min(100, max(0, score)), reasons=reasons,
        indicators={
            "rsi6": round(cur_rsi6, 1), "rsi14": round(cur_rsi14, 1),
            "k": round(cur_k, 1), "d": round(cur_d, 1), "j": round(cur_j, 1),
            "bias20": round(cur_bias, 2), "pct_10d": round(pct_10d, 2),
            "pct_20d": round(pct_20d, 2),
            "boll_lower": round(cur_boll_lower, 2), "boll_mid": round(cur_boll_mid, 2),
            "lower_shadow": round(lower_shadow, 2), "yin_count": yin_count,
        }
    )


# ════════════════════════════════════════════════════════════════
# 板块股票池
# ════════════════════════════════════════════════════════════════

# 科技成长股票池 — 半导体/CPO/PCB/AI算力
GROWTH_POOL = {
    # 半导体
    "688981": "中芯国际", "002371": "北方华创", "688012": "中微公司",
    "603986": "兆易创新", "688396": "华润微", "300661": "圣邦股份",
    "688008": "澜起科技", "603501": "韦尔股份", "688521": "芯原股份",
    "002049": "紫光国微", "688256": "寒武纪", "300782": "卓胜微",
    # CPO/光模块
    "300502": "新易盛", "300308": "中际旭创", "002281": "光迅科技",
    "300620": "光库科技", "688600": "天孚通信", "300548": "博创科技",
    "300394": "天孚通信", "603083": "剑桥科技",
    # PCB/封装基板
    "002916": "深南电路", "600183": "生益科技", "002463": "沪电股份",
    "300408": "三环集团", "603328": "依顿电子", "002938": "鹏鼎控股",
    "300870": "欧陆通",
    # AI算力/服务器
    "002230": "科大讯飞", "688111": "金山办公", "300496": "中科创达",
    "002415": "海康威视", "000977": "浪潮信息", "603019": "中科曙光",
    "688256": "寒武纪",
}

# 价值超跌股票池 — 可动态扩展
# 这里定义一些典型价值股，实际运行时从全A中筛选
VALUE_POOL = {
    # 白酒/消费
    "600519": "贵州茅台", "000858": "五粮液", "000568": "泸州老窖",
    "002304": "洋河股份", "600809": "山西汾酒",
    # 银行
    "601398": "工商银行", "601939": "建设银行", "600036": "招商银行",
    "601166": "兴业银行", "000001": "平安银行",
    # 医药
    "600276": "恒瑞医药", "000538": "云南白药", "300760": "迈瑞医疗",
    "300122": "智飞生物", "002007": "华兰生物",
    # 地产/基建
    "001979": "招商蛇口", "600048": "保利发展", "000002": "万科A",
    # 新能源
    "300750": "宁德时代", "002594": "比亚迪", "601012": "隆基绿能",
}


def get_growth_pool() -> Dict[str, str]:
    """获取科技成长股票池"""
    return GROWTH_POOL.copy()


def get_value_pool() -> Dict[str, str]:
    """获取价值超跌股票池"""
    return VALUE_POOL.copy()
