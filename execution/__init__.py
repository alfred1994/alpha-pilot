"""
执行层模块
交易通道适配器 → 模拟盘账户 → 订单管理 → 实时监控
"""

from execution.broker import BrokerAdapter, PaperBrokerAdapter, get_broker_adapter
