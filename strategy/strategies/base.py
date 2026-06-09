"""
策略基类
所有策略都继承自 BaseStrategy，实现 generate_signals() 方法
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import pandas as pd


@dataclass
class Signal:
    """交易信号"""
    date: str
    code: str
    action: str          # "BUY" / "SELL" / "HOLD"
    score: float         # 信号强度 0-100
    reason: str          # 信号原因
    metadata: dict = field(default_factory=dict)  # 额外信息（用于LLM分析）


class BaseStrategy(ABC):
    """
    策略基类

    所有策略必须:
    1. 设置 name 和 version
    2. 定义 params（可调参数，LLM会修改）
    3. 实现 generate_signals() 方法

    使用方法:
        strategy = MyStrategy()
        signal = strategy.generate_signals("600519", df)
        if signal.action == "BUY":
            print(f"买入信号: {signal.reason}")
    """

    name: str = "unnamed"
    version: str = "1.0"
    params: dict = {}

    @abstractmethod
    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        """
        生成交易信号

        Args:
            code: 股票代码
            df: K线DataFrame，包含 date,open,high,low,close,volume
            **kwargs: 其他参数

        Returns:
            Signal 对象
        """
        pass

    def get_params(self) -> dict:
        """获取当前参数（供LLM读取）"""
        return self.params.copy()

    def set_params(self, params: dict):
        """设置参数（供LLM修改）"""
        self.params.update(params)

    def describe(self) -> str:
        """描述策略逻辑（供LLM理解）"""
        return f"策略: {self.name} v{self.version}\n参数: {self.params}"

    def get_param_schema(self) -> dict:
        """获取参数类型和范围（供LLM参考）"""
        schema = {}
        for key, value in self.params.items():
            if isinstance(value, float):
                schema[key] = {"type": "float", "current": value}
            elif isinstance(value, int):
                schema[key] = {"type": "int", "current": value}
            else:
                schema[key] = {"type": type(value).__name__, "current": value}
        return schema
