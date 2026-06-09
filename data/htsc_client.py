"""
华泰证券 Skill 客户端
====================================================================
封装3个华泰skill的调用，供量化系统各模块使用：

1. select_stock()   - 条件选股（替代东方财富活跃股扫描）
2. query_indicator() - 行情/指标查询（补充实时数据）
3. financial_analysis() - 个股诊断/市场洞察（增强舆情分析）

所有调用走 subprocess 执行华泰 Python 脚本，返回 JSON。
====================================================================
"""
import json
import os
import subprocess
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger("data.htsc")

# Skill 脚本路径
SKILLS_ROOT = os.path.expanduser("~/.hermes/skills")
SELECT_STOCK_SCRIPT = os.path.join(SKILLS_ROOT, "select-stock", "select_stock.py")
QUERY_INDICATOR_SCRIPT = os.path.join(SKILLS_ROOT, "query-indicator", "query_indicator.py")
FINANCIAL_ANALYSIS_SCRIPT = os.path.join(SKILLS_ROOT, "financial-analysis", "financial_analysis.py")

# 环境变量（优先环境变量，其次配置文件）
def _get_api_key() -> str:
    """获取HT_APIKEY，优先环境变量，其次 ~/.htsc-skills/config"""
    key = os.environ.get("HT_APIKEY", "").strip()
    # 确保是有效的key格式（以ht_开头）
    if key and key.startswith("ht_"):
        return key
    # 从配置文件读取
    config_path = os.path.expanduser("~/.htsc-skills/config")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HT_APIKEY="):
                    val = line.split("=", 1)[1].strip()
                    if val and val.startswith("ht_"):
                        return val
    return ""

HT_APIKEY = _get_api_key()


def _run_skill(script: str, tool: str, args: dict, timeout: int = 30) -> Optional[dict]:
    """
    通用skill执行器

    Args:
        script: Python脚本路径
        tool: 工具名
        args: 参数字典，key=value 格式
        timeout: 超时秒数

    Returns:
        解析后的JSON dict，失败返回None
    """
    if not os.path.exists(script):
        logger.warning(f"Skill脚本不存在: {script}")
        return None

    if not HT_APIKEY:
        logger.warning("HT_APIKEY 未设置，华泰skill不可用")
        return None

    # 构建命令
    cmd = ["python3", script, tool]
    for k, v in args.items():
        cmd.extend([f"--{k}", str(v)])

    env = os.environ.copy()
    env["HT_APIKEY"] = HT_APIKEY

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        if result.returncode != 0:
            logger.warning(f"Skill执行失败 ({tool}): {result.stderr[:200]}")
            return None

        # 解析JSON输出
        output = result.stdout.strip()
        if not output:
            return None

        data = json.loads(output)
        if data.get("ok"):
            return data.get("data")
        else:
            error = data.get("error", {})
            logger.warning(f"Skill业务错误 ({tool}): {error.get('message', 'unknown')}")
            return None

    except subprocess.TimeoutExpired:
        logger.warning(f"Skill超时 ({tool}): {timeout}s")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Skill返回非JSON ({tool}): {e}")
        return None
    except Exception as e:
        logger.warning(f"Skill异常 ({tool}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# 1. 条件选股
# ═══════════════════════════════════════════════════════════════════

def select_stock(query: str) -> Optional[str]:
    """
    条件选股

    Args:
        query: 自然语言选股条件，如 "主力净流入超过5000万且均线金叉的股票"

    Returns:
        选股结果文本（Markdown表格），失败返回None
    """
    data = _run_skill(SELECT_STOCK_SCRIPT, "selectStock", {"query": query}, timeout=60)
    if data:
        return data.get("result")
    return None


def select_stock_parsed(query: str) -> List[Dict[str, str]]:
    """
    条件选股并解析为结构化数据

    Returns:
        [{"code": "600519", "name": "贵州茅台", ...}, ...]
    """
    result_text = select_stock(query)
    if not result_text:
        return []

    stocks = []
    lines = result_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line.startswith("|") or "序号" in line or "---" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3:
            try:
                # 解析: 序号 | 股票名称(代码) | 最新价 | ...
                name_code = parts[1]
                # 提取代码和名称
                import re
                match = re.search(r'(\d{6})', name_code)
                if match:
                    code = match.group(1)
                    name = re.sub(r'\(\d+\)', '', name_code).strip()
                    stocks.append({
                        "code": code,
                        "name": name,
                        "price": parts[2] if len(parts) > 2 else "",
                    })
            except Exception:
                continue

    return stocks


# ═══════════════════════════════════════════════════════════════════
# 2. 行情/指标查询
# ═══════════════════════════════════════════════════════════════════

def query_indicator(query: str) -> Optional[str]:
    """
    金融指标查询

    Args:
        query: 自然语言查询，如 "茅台最新价和PE"

    Returns:
        查询结果文本，失败返回None
    """
    data = _run_skill(QUERY_INDICATOR_SCRIPT, "queryIndicator", {"query": query}, timeout=30)
    if data:
        return data.get("answer")
    return None


def get_stock_quote(stock_name: str) -> Optional[Dict[str, Any]]:
    """
    获取个股行情（解析为结构化数据）

    Args:
        stock_name: 股票名称或代码

    Returns:
        {"price": float, "change_pct": float, "volume": float, ...} 或 None
    """
    answer = query_indicator(f"{stock_name}最新价、涨跌幅、成交量、成交额")
    if not answer:
        return None

    import re
    result = {}

    # 尝试解析数值
    price_match = re.search(r'最新价[为是：:]\s*([\d.]+)', answer)
    if price_match:
        result["price"] = float(price_match.group(1))

    pct_match = re.search(r'涨跌幅[为是：:]\s*([+-]?[\d.]+)%?', answer)
    if pct_match:
        result["change_pct"] = float(pct_match.group(1))

    vol_match = re.search(r'成交量[为是：:]\s*([\d.]+)\s*万', answer)
    if vol_match:
        result["volume_wan"] = float(vol_match.group(1))

    result["raw"] = answer
    return result if result else None


# ═══════════════════════════════════════════════════════════════════
# 3. 金融分析（个股诊断/市场洞察）
# ═══════════════════════════════════════════════════════════════════

def financial_analysis(tool: str, query: str) -> Optional[str]:
    """
    金融分析查询

    Args:
        tool: 工具名 (investCalendar / diagnosisStock / marketInsight)
        query: 查询内容

    Returns:
        分析结果文本，失败返回None
    """
    data = _run_skill(FINANCIAL_ANALYSIS_SCRIPT, tool, {"query": query}, timeout=120)
    if data:
        return data.get("answer")
    return None


def diagnose_stock(stock_name: str) -> Optional[str]:
    """
    个股诊断

    Args:
        stock_name: 股票名称或代码

    Returns:
        诊断结果文本
    """
    return financial_analysis("diagnosisStock", f"{stock_name}分析诊断")


def market_insight(query: str = "今天大盘怎么样") -> Optional[str]:
    """
    市场洞察

    Returns:
        市场分析文本
    """
    return financial_analysis("marketInsight", query)


# ═══════════════════════════════════════════════════════════════════
# 测试入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 htsc_client.py <command> [args]")
        print("  select <query>      - 条件选股")
        print("  quote <stock>       - 个股行情")
        print("  diagnose <stock>    - 个股诊断")
        print("  insight [query]     - 市场洞察")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "select":
        query = sys.argv[2] if len(sys.argv) > 2 else "主力净流入超过5000万的股票"
        result = select_stock(query)
        print(result or "无结果")
    elif cmd == "quote":
        stock = sys.argv[2] if len(sys.argv) > 2 else "华泰证券"
        result = get_stock_quote(stock)
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "无结果")
    elif cmd == "diagnose":
        stock = sys.argv[2] if len(sys.argv) > 2 else "华泰证券"
        result = diagnose_stock(stock)
        print(result or "无结果")
    elif cmd == "insight":
        query = sys.argv[2] if len(sys.argv) > 2 else "今天大盘怎么样"
        result = market_insight(query)
        print(result or "无结果")
    else:
        print(f"未知命令: {cmd}")
