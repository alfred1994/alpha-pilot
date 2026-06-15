"""
Telegram通知模块
====================================================================
功能:
  1. 发送文本消息到Telegram
  2. 发送格式化的交易信号报告
  3. 发送每日汇总报告
  4. 支持Markdown格式化

配置:
  环境变量 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID
====================================================================
"""
import os
import requests
import logging
import html
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import Optional

logger = logging.getLogger("scheduler.notifier")

# Telegram配置
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def is_configured() -> bool:
    """Telegram是否已配置"""
    return bool(BOT_TOKEN and CHAT_ID)


def send_message(text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
    """
    发送Telegram消息

    Args:
        text: 消息内容
        parse_mode: 解析模式 "HTML" / "Markdown"
        silent: 是否静默发送(不通知)

    Returns:
        bool: 是否发送成功
    """
    if not is_configured():
        logger.warning("Telegram未配置, 跳过发送")
        return False

    # Telegram消息长度限制4096字符
    if len(text) > 4000:
        text = text[:4000] + "\n...(已截断)"

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_notification": silent,
    }

    try:
        resp = requests.post(API_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram消息发送成功")
            return True
        else:
            logger.error(f"Telegram发送失败: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Telegram发送异常: {e}")
        return False


def send_signal_report(signals: list, title: str = "量化信号报告") -> bool:
    """
    发送信号报告

    Args:
        signals: CompositeSignal对象列表
        title: 报告标题

    Returns:
        bool: 是否发送成功
    """
    now = _now_bj().strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>{title}</b> ({now})", ""]

    if not signals:
        lines.append("无符合条件的信号")
    else:
        for s in signals:
            action_emoji = {"买入": "\U0001f7e2", "卖出": "\U0001f534", "持有": "⚪"}.get(s.direction.value, "❓")
            lines.append(f"{action_emoji} <b>{s.code} {s.name}</b>")
            lines.append(f"  分数: {s.final_score} | 方向: {s.direction.value} | 置信度: {s.confidence:.0%}")

            # 5维详情
            if hasattr(s, "signals") and s.signals:
                dims = s.signals
                lines.append(f"  技术:{dims.get('technical', 50):.0f} "
                             f"资金:{dims.get('capital', 50):.0f} "
                             f"舆情:{dims.get('sentiment', 50):.0f} "
                             f"情绪:{dims.get('emotion', 50):.0f} "
                             f"基本面:{dims.get('fundamental', 50):.0f}")

            lines.append(f"  理由: {s.reason}")
            lines.append("")

    lines.append(f"共 {len(signals)} 只股票")
    text = "\n".join(lines)
    return send_message(text, parse_mode="HTML")


def send_decision_report(decisions: list, title: str = "决策报告") -> bool:
    """
    发送决策报告

    Args:
        decisions: TradeDecision对象列表
        title: 报告标题

    Returns:
        bool: 是否发送成功
    """
    now = _now_bj().strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>{title}</b> ({now})", ""]

    buy_count = 0
    sell_count = 0

    for d in decisions:
        action_emoji = {"BUY": "\U0001f7e2", "SELL": "\U0001f534", "HOLD": "⚪"}.get(d.action, "❓")
        lines.append(f"{action_emoji} <b>{d.code} {d.name}</b>")
        lines.append(f"  决策: {d.action} | 分数: {d.composite_score:.1f} | 置信度: {d.confidence:.0%}")

        if hasattr(d, "dimensions") and d.dimensions:
            for dim_name in ["technical", "capital", "sentiment", "emotion", "fundamental"]:
                dim = d.dimensions.get(dim_name)
                if dim:
                    label = {"technical": "技术", "capital": "资金", "sentiment": "舆情",
                             "emotion": "情绪", "fundamental": "基本面"}.get(dim_name, dim_name)
                    lines.append(f"    {label}: {dim.score:.1f}")

        lines.append(f"  理由: {d.reason}")
        lines.append("")

        if d.action == "BUY":
            buy_count += 1
        elif d.action == "SELL":
            sell_count += 1

    lines.append(f"汇总: 买入{buy_count} 卖出{sell_count} 持有{len(decisions)-buy_count-sell_count}")
    text = "\n".join(lines)
    return send_message(text, parse_mode="HTML")


def send_daily_summary(
    market_status: str = "",
    signal_count: int = 0,
    buy_count: int = 0,
    sell_count: int = 0,
    top_picks: list = None,
    error: str = None,
) -> bool:
    """
    发送每日汇总

    Args:
        market_status: 市场状态
        signal_count: 信号总数
        buy_count: 买入信号数
        sell_count: 卖出信号数
        top_picks: Top推荐列表 [(code, name, score), ...]
        error: 错误信息

    Returns:
        bool: 是否发送成功
    """
    now = _now_bj().strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>每日汇总</b> ({now})", ""]

    if error:
        lines.append(f"⚠️ 执行异常: {error}")
    else:
        lines.append(f"\U0001f4ca 市场状态: {market_status}")
        lines.append(f"\U0001f4cb 扫描信号: {signal_count}只")
        lines.append(f"\U0001f7e2 买入信号: {buy_count}只")
        lines.append(f"\U0001f534 卖出信号: {sell_count}只")

        if top_picks:
            lines.append("")
            lines.append("<b>Top推荐:</b>")
            for i, (code, name, score) in enumerate(top_picks[:5], 1):
                lines.append(f"  {i}. {code} {name} (分数:{score})")

    text = "\n".join(lines)
    return send_message(text, parse_mode="HTML")


def send_error_alert(error_msg: str) -> bool:
    """发送错误告警"""
    now = _now_bj().strftime("%Y-%m-%d %H:%M")
    text = f"⚠️ <b>系统告警</b> ({now})\n\n{error_msg}"
    return send_message(text, parse_mode="HTML")


def should_notify_auto_cycle(actions: list, error: str = "") -> bool:
    """
    判断自动盯盘本轮是否值得通知

    只对关键动作通知，避免循环空转时刷屏。
    """
    if error:
        return True
    for action in actions or []:
        text = str(action)
        if "异常" in text:
            return True
        if "盘前数据预热完成" in text or "市场环境识别" in text:
            return True
        if "盘后复盘进化" in text:
            return True
        if "卖出" in text and "卖出0笔" not in text:
            return True
        if "模拟执行" in text and "成交0笔" not in text:
            return True
    return False


def format_auto_cycle_message(date: str, status: str, actions: list,
                              loop_count: int = 0, error: str = "") -> str:
    """格式化自动盯盘通知"""
    now = _now_bj().strftime("%Y-%m-%d %H:%M")
    title = "自动盯盘"
    if error or any("异常" in str(a) for a in actions or []):
        title = "自动盯盘告警"
    elif any("模拟执行" in str(a) and "成交0笔" not in str(a) for a in actions or []):
        title = "模拟交易成交"
    elif any("盘后复盘进化" in str(a) for a in actions or []):
        title = "盘后复盘进化"

    lines = [
        f"<b>{html.escape(title)}</b> ({now})",
        f"日期: {html.escape(date or '')}",
        f"市场状态: {html.escape(status or '')}",
        f"循环次数: {loop_count}",
        "",
        "<b>动作</b>:",
    ]
    if actions:
        for action in actions:
            lines.append(f"- {html.escape(str(action))}")
    else:
        lines.append("- 本轮无需操作")

    if error:
        lines.extend(["", f"<b>错误</b>: {html.escape(error)}"])
    return "\n".join(lines)


def send_auto_cycle_report(date: str, status: str, actions: list,
                           loop_count: int = 0, error: str = "",
                           force: bool = False) -> bool:
    """
    发送自动盯盘关键动作通知

    Telegram未配置时静默跳过，避免影响自动交易主循环。
    """
    if not force and not should_notify_auto_cycle(actions, error):
        return False
    if not is_configured():
        logger.debug("Telegram未配置，自动盯盘通知静默跳过")
        return False
    text = format_auto_cycle_message(date, status, actions, loop_count, error)
    return send_message(text, parse_mode="HTML")


# 测试
if __name__ == "__main__":
    print("Telegram通知测试")
    print(f"BOT_TOKEN: {'已配置' if BOT_TOKEN else '未配置'}")
    print(f"CHAT_ID: {'已配置' if CHAT_ID else '未配置'}")

    if BOT_TOKEN and CHAT_ID:
        ok = send_message("测试消息 - A股量化系统")
        print(f"发送结果: {'成功' if ok else '失败'}")
    else:
        print("请设置环境变量 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
