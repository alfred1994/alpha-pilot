"""
外围市场数据获取模块 - 腾讯行情API
功能: 美股三大指数、港股恒生、A50期货
用于收盘后收集外围市场数据，评估隔夜风险

腾讯全球指数格式（~分隔，71字段）:
  [1]名称 [3]最新价 [4]昨收 [5]今开 [31]涨跌额 [32]涨跌幅 [33]最高 [34]最低

A50期货使用新浪API（数据准确）
"""
import re
import time
import requests
from datetime import datetime
from data import logger

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# 腾讯全球指数代码
TENCENT_INDICES = {
    "usDJI": {"key": "djia", "name": "道琼斯", "region": "us"},
    "usNDX": {"key": "nasdaq", "name": "纳斯达克100", "region": "us"},
    "usINX": {"key": "sp500", "name": "标普500", "region": "us"},
    "hkHSI": {"key": "hsi", "name": "恒生指数", "region": "hk"},
}

# A50期货代码（新浪，数据准确）
A50_CODE = "hf_CHA50CFD"
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _request_with_retry(url, headers=None, max_retries=3, timeout=10):
    """带重试的HTTP请求"""
    h = headers or HEADERS
    for i in range(max_retries):
        try:
            resp = requests.get(url, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            logger.warning(f"请求失败(第{i+1}次): {url[:80]} - {e}")
            if i < max_retries - 1:
                time.sleep(1)
    return None


def get_global_indices():
    """
    获取全球主要指数数据（腾讯API）

    Returns:
        dict: {"us_indices": {...}, "hk_indices": {...}}
    """
    codes = ",".join(TENCENT_INDICES.keys())
    url = f"http://qt.gtimg.cn/q={codes}"

    resp = _request_with_retry(url)
    if not resp:
        logger.error("获取全球指数失败: 所有重试均失败")
        return {}

    try:
        content = resp.content.decode("gbk")
    except UnicodeDecodeError:
        content = resp.content.decode("gbk", errors="ignore")

    result = {"us_indices": {}, "hk_indices": {}}

    for line in content.strip().split("\n"):
        if "~" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 35:
            continue

        # 从变量名提取代码: v_xxYY="...";
        code_match = re.search(r'v_(\w+)=', line)
        if not code_match:
            continue
        code = code_match.group(1).upper()

        # 匹配映射表（代码可能大小写不一致）
        meta = None
        for tcode, tmeta in TENCENT_INDICES.items():
            if tcode.upper() == code or tcode.lower() == code.lower():
                meta = tmeta
                break
        if not meta:
            # 尝试模糊匹配
            for tcode, tmeta in TENCENT_INDICES.items():
                if tcode[2:].upper() in code.upper():
                    meta = tmeta
                    break
        if not meta:
            continue

        try:
            name = parts[1].strip() if len(parts) > 1 else meta["name"]
            price = float(parts[3]) if parts[3].strip() else 0.0
            prev_close = float(parts[4]) if parts[4].strip() else 0.0
            change = float(parts[31]) if parts[31].strip() else 0.0
            change_pct = float(parts[32]) if parts[32].strip() else 0.0
        except (ValueError, IndexError) as e:
            logger.warning(f"解析指数数据失败 {code}: {e}")
            continue

        data = {
            "name": meta["name"],
            "price": price,
            "prev_close": prev_close,
            "change_pct": round(change_pct, 2),
            "change": round(change, 2),
        }

        region = meta["region"]
        key = meta["key"]
        if region == "us":
            result["us_indices"][key] = data
        elif region == "hk":
            result["hk_indices"][key] = data

    count = sum(len(v) for v in result.values())
    logger.info(f"全球指数获取成功: {count}个 (腾讯)")
    return result


def get_a50_futures():
    """
    获取A50期货实时数据（新浪API，数据准确）

    Returns:
        dict: {"name": "A50期货", "price": ..., "change_pct": ..., ...}
    """
    url = f"http://hq.sinajs.cn/list={A50_CODE}"

    resp = _request_with_retry(url, headers=SINA_HEADERS)
    if not resp:
        logger.error("获取A50期货失败: 所有重试均失败")
        return {}

    try:
        content = resp.content.decode("gbk")
    except UnicodeDecodeError:
        content = resp.content.decode("gbk", errors="ignore")

    # A50格式: 最新(0),(1),买(2),卖(3),最高(4),最低(5),时间(6),昨收(7),今开(8),...
    match = re.match(r'var hq_str_\w+="(.+)";', content.strip())
    if not match:
        logger.warning("A50期货数据解析失败: 格式不匹配")
        return {}

    fields = match.group(1).split(",")
    try:
        price = float(fields[0]) if fields[0] else 0.0
        high = float(fields[4]) if len(fields) > 4 and fields[4] else 0.0
        low = float(fields[5]) if len(fields) > 5 and fields[5] else 0.0
        prev_close = float(fields[7]) if len(fields) > 7 and fields[7] else 0.0
        open_price = float(fields[8]) if len(fields) > 8 and fields[8] else 0.0

        change = price - prev_close if prev_close > 0 else 0.0
        change_pct = (change / prev_close * 100) if prev_close > 0 else 0.0
        change_pct = round(change_pct, 2)
    except (ValueError, IndexError) as e:
        logger.warning(f"A50期货数据解析失败: {e}")
        return {}

    name = "A50期货"
    if len(fields) > 13 and fields[13]:
        name = fields[13]

    result = {
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "change": round(change, 2),
        "prev_close": prev_close,
        "open": open_price,
        "high": high,
        "low": low,
    }
    logger.info(f"A50期货: {price} ({'+' if change_pct >= 0 else ''}{change_pct}%)")
    return result


def _assess_risk_level(us_indices, hk_indices, a50_futures):
    """
    根据外围市场涨跌幅评估风险等级

    规则:
    - 高风险: 任一主要指数跌幅 > 2% 或 A50期货跌幅 > 1.5%
    - 中风险: 任一主要指数跌幅 > 1% 或 A50期货跌幅 > 0.5%
    - 低风险: 其他情况

    Returns:
        str: "low" / "medium" / "high"
    """
    for key in ["djia", "nasdaq", "sp500"]:
        idx = us_indices.get(key, {})
        pct = idx.get("change_pct", 0)
        if pct < -2:
            return "high"
        if pct < -1:
            return "medium"

    hsi = hk_indices.get("hsi", {})
    if hsi.get("change_pct", 0) < -2:
        return "high"
    if hsi.get("change_pct", 0) < -1:
        return "medium"

    a50_pct = a50_futures.get("change_pct", 0)
    if a50_pct < -1.5:
        return "high"
    if a50_pct < -0.5:
        return "medium"

    return "low"


def get_us_market_summary():
    """
    综合获取所有外围市场数据，返回结构化dict

    Returns:
        dict: {
            "timestamp": "...",
            "us_indices": {...},
            "hk_indices": {...},
            "a50_futures": {...},
            "risk_level": "low"
        }
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    indices = get_global_indices()
    us_indices = indices.get("us_indices", {})
    hk_indices = indices.get("hk_indices", {})

    a50_futures = get_a50_futures()

    risk_level = _assess_risk_level(us_indices, hk_indices, a50_futures)

    summary = {
        "timestamp": timestamp,
        "us_indices": us_indices,
        "hk_indices": hk_indices,
        "a50_futures": a50_futures,
        "risk_level": risk_level,
    }

    risk_label = {"low": "低", "medium": "中", "high": "高"}.get(risk_level, "未知")
    logger.info(f"外围市场汇总完成: 风险等级={risk_label}")
    return summary


# 测试入口
if __name__ == "__main__":
    import json
    print("=" * 60)
    print("外围市场数据测试 (腾讯API)")
    print("=" * 60)

    summary = get_us_market_summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
