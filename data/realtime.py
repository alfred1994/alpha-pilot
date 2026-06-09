"""
实时行情模块 - 腾讯财经API
完全免费，无需注册，无需Token
接口: http://qt.gtimg.cn/q=sh600519,sz000001,...
"""
import requests
import re
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Optional

TENCENT_URL = "http://qt.gtimg.cn/q="

@dataclass
class RealtimeQuote:
    """实时行情数据"""
    code: str           # 股票代码
    name: str           # 股票名称
    price: float        # 现价
    open: float         # 开盘价
    close_prev: float   # 昨收
    high: float         # 最高
    low: float          # 最低
    volume: int         # 成交量(手)
    amount: float       # 成交额(万元)
    change: float       # 涨跌额
    change_pct: float   # 涨跌幅(%)
    turnover: float     # 换手率(%)
    pe: float           # 市盈率
    market_cap: float   # 总市值(亿)
    timestamp: str      # 数据时间


def _parse_tencent_line(line: str) -> Optional[RealtimeQuote]:
    """解析腾讯行情数据行"""
    # 格式: v_sh600519="1~贵州茅台~600519~1315.00~1321.00~1321.00~47487~..."
    match = re.search(r'v_\w+="(.+)"', line)
    if not match:
        return None
    
    parts = match.group(1).split("~")
    if len(parts) < 50 or parts[2] == "":
        return None
    
    try:
        return RealtimeQuote(
            code=parts[2],
            name=parts[1],
            price=float(parts[3]) if parts[3] else 0,
            open=float(parts[5]) if parts[5] else 0,
            close_prev=float(parts[4]) if parts[4] else 0,
            high=float(parts[33]) if parts[33] else 0,
            low=float(parts[34]) if parts[34] else 0,
            volume=int(parts[36]) if parts[36] else 0,
            amount=float(parts[37]) if parts[37] else 0,
            change=float(parts[31]) if parts[31] else 0,
            change_pct=float(parts[32]) if parts[32] else 0,
            turnover=float(parts[38]) if parts[38] else 0,
            pe=float(parts[39]) if parts[39] else 0,
            market_cap=float(parts[45]) / 10000 if parts[45] else 0,  # 万→亿
            timestamp=parts[30] if parts[30] else datetime.now().strftime("%Y%m%d%H%M%S"),
        )
    except (ValueError, IndexError):
        return None


def get_realtime(codes) -> List[RealtimeQuote]:
    """
    获取实时行情
    
    Args:
        codes: 股票代码或代码列表
               - 单个代码: "600519"
               - 代码列表: ["600519", "000001"]
               - 带前缀: ["sh600519", "sz000001"]
    
    Returns:
        RealtimeQuote 列表
    """
    # 处理单个代码
    if isinstance(codes, str):
        codes = [codes]
    
    # 自动加市场前缀
    symbols = []
    for c in codes:
        c = c.strip()
        if c.startswith(("sh", "sz")):
            symbols.append(c)
        elif c.startswith(("6", "5")):
            symbols.append(f"sh{c}")
        else:
            symbols.append(f"sz{c}")
    
    url = TENCENT_URL + ",".join(symbols)
    try:
        resp = requests.get(url, timeout=5)
        resp.encoding = "gbk"
        results = []
        for line in resp.text.strip().split("\n"):
            q = _parse_tencent_line(line)
            if q:
                results.append(q)
        return results
    except Exception as e:
        return []


def get_realtime_batch(codes: List[str], batch_size: int = 50) -> List[RealtimeQuote]:
    """批量获取（分批请求，避免URL过长）"""
    results = []
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        results.extend(get_realtime(batch))
    return results


# 测试
if __name__ == "__main__":
    # 测试几只热门股
    quotes = get_realtime(["600519", "000001", "300750", "601398"])
    print(f"\n{'='*70}")
    print(f"{'代码':<8} {'名称':<8} {'现价':>8} {'涨跌幅':>8} {'成交额(亿)':>10} {'换手率':>6}")
    print(f"{'='*70}")
    for q in quotes:
        print(f"{q.code:<8} {q.name:<8} {q.price:>8.2f} {q.change_pct:>7.2f}% {q.amount/10000:>10.2f} {q.turnover:>5.2f}%")
