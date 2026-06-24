"""
LLM策略优化器

流程:
1. 跑回测，收集结果
2. 把回测结果+策略代码发给MiMo
3. MiMo分析问题，建议参数调整
4. 应用调整，再跑回测
5. 对比改进效果
"""
import json
import re
import logging
import os
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

from .strategies.base import BaseStrategy, Signal
from .mimo_client import DEFAULT_HTTP_TIMEOUT, post_chat_completion

logger = logging.getLogger("strategy.optimizer")


@dataclass
class OptimizationRound:
    """单轮优化记录"""
    round_num: int
    params: dict
    result: dict  # 回测指标
    suggestion: dict = field(default_factory=dict)  # LLM建议
    improvement: float = 0.0  # 相对于上一轮的改进


@dataclass
class OptimizationResult:
    """优化最终结果"""
    strategy_name: str
    initial_params: dict
    final_params: dict
    initial_return: float
    final_return: float
    improvement: float
    rounds: List[OptimizationRound]
    best_round: int


class StrategyOptimizer:
    """
    LLM策略优化器

    流程:
    1. 跑回测，收集结果
    2. 把回测结果+策略代码发给MiMo
    3. MiMo分析问题，建议参数调整
    4. 应用调整，再跑回测
    5. 对比改进效果

    使用方法:
        from strategy.strategies import get_strategy
        from portfolio.backtest import SimpleBacktestEngine

        strategy = get_strategy("zt_reversal")
        engine = SimpleBacktestEngine()

        optimizer = StrategyOptimizer(strategy, engine)
        result = optimizer.optimize(
            stock_codes=["600519", "000001"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            max_rounds=3,
        )
        print(f"优化改进: {result.improvement:+.2%}")
    """

    def __init__(self, strategy: BaseStrategy, engine):
        """
        初始化优化器

        Args:
            strategy: 策略实例
            engine: 回测引擎（SimpleBacktestEngine）
        """
        self.strategy = strategy
        self.engine = engine
        self.history: List[OptimizationRound] = []

    def optimize(
        self,
        stock_codes: List[str],
        start_date: str,
        end_date: str,
        max_rounds: int = 3,
        improvement_threshold: float = 0.01,  # 改进阈值1%
    ) -> OptimizationResult:
        """
        运行优化循环

        Args:
            stock_codes: 股票列表
            start_date/end_date: 回测区间
            max_rounds: 最大优化轮数
            improvement_threshold: 改进阈值，低于此值停止优化

        Returns:
            OptimizationResult 优化结果
        """
        initial_params = self.strategy.get_params()
        initial_return = 0.0
        best_return = -999.0
        best_round = 0
        best_params = initial_params.copy()

        for round_num in range(max_rounds):
            logger.info(f"=== 优化第 {round_num + 1}/{max_rounds} 轮 ===")
            logger.info(f"当前参数: {self.strategy.get_params()}")

            # 1. 跑回测
            try:
                result = self.engine.run(
                    stock_codes=stock_codes,
                    start_date=start_date,
                    end_date=end_date,
                    decision_mode="strategy",
                )
            except Exception as e:
                logger.error(f"回测失败: {e}")
                break

            # 2. 记录结果
            result_dict = {
                "total_return": result.total_return,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "win_rate": result.win_rate,
                "total_trades": result.total_trades,
                "avg_hold_days": result.avg_hold_days,
            }

            if round_num == 0:
                initial_return = result.total_return

            # 更新最佳
            if result.total_return > best_return:
                best_return = result.total_return
                best_round = round_num
                best_params = self.strategy.get_params().copy()

            # 计算改进
            improvement = 0.0
            if self.history:
                improvement = result.total_return - self.history[-1].result["total_return"]

            round_record = OptimizationRound(
                round_num=round_num,
                params=self.strategy.get_params().copy(),
                result=result_dict,
                improvement=improvement,
            )

            # 3. LLM分析（最后一轮不分析）
            if round_num < max_rounds - 1:
                suggestion = self._ask_llm(result, round_num)
                round_record.suggestion = suggestion

                # 4. 应用建议
                if suggestion.get("stop"):
                    logger.info("LLM建议停止优化")
                    self.history.append(round_record)
                    break

                if suggestion.get("params"):
                    old_params = self.strategy.get_params().copy()
                    self.strategy.set_params(suggestion["params"])
                    new_params = self.strategy.get_params()
                    logger.info(f"参数调整: {old_params} → {new_params}")
                    logger.info(f"调整理由: {suggestion.get('reasoning', '无')}")

            self.history.append(round_record)

            # 检查改进是否足够
            if round_num > 0 and improvement < improvement_threshold:
                logger.info(f"改进不足 ({improvement:+.2%} < {improvement_threshold:+.2%})，停止优化")
                break

        # 恢复最佳参数
        self.strategy.set_params(best_params)

        return OptimizationResult(
            strategy_name=self.strategy.name,
            initial_params=initial_params,
            final_params=best_params,
            initial_return=initial_return,
            final_return=best_return,
            improvement=best_return - initial_return,
            rounds=self.history,
            best_round=best_round,
        )

    def _ask_llm(self, result, round_num: int) -> dict:
        """
        调用MiMo分析回测结果，建议参数调整

        Args:
            result: BacktestResult 回测结果
            round_num: 当前轮次

        Returns:
            dict: {"analysis": "...", "params": {...}, "reasoning": "...", "stop": false}
        """
        # 构建提示词
        prompt = self._build_prompt(result, round_num)

        # 调用MiMo API
        api_key = os.environ.get("XIAOMI_API_KEY", "")
        base_url = "https://token-plan-cn.xiaomimimo.com/v1"

        if not api_key:
            logger.warning("XIAOMI_API_KEY 未设置，跳过LLM优化")
            return {"analysis": "API Key未配置", "params": {}, "reasoning": "跳过", "stop": True}

        try:
            data = post_chat_completion(
                base_url=base_url,
                api_key=api_key,
                payload={
                    "model": "mimo-v2.5-pro",
                    "messages": [
                        {"role": "system", "content": "你是A股量化策略优化专家，用中文回答。"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 2000,
                    "temperature": 0.3,
                },
                http_timeout=DEFAULT_HTTP_TIMEOUT,
                hard_timeout=DEFAULT_HTTP_TIMEOUT + 5,
            )
            if not data:
                return {"analysis": "LLM无响应", "params": {}, "reasoning": "超时或失败", "stop": False}

            msg = data["choices"][0]["message"]
            content = (msg.get("content", "") or "").strip()
            if not content:
                content = (msg.get("reasoning_content", "") or "").strip()

            # 解析JSON
            return self._parse_llm_response(content)

        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return {"analysis": f"LLM调用失败: {e}", "params": {}, "reasoning": "错误", "stop": False}

    def _build_prompt(self, result, round_num: int) -> str:
        """构建LLM提示词"""
        # 获取参数 schema
        param_schema = self.strategy.get_param_schema()

        # 格式化最近交易
        recent_trades = self._format_recent_trades(result.trades[-10:])

        prompt = f"""你是A股量化策略优化专家。分析以下回测结果，给出参数调整建议。

## 当前策略
{self.strategy.describe()}

## 参数说明
{json.dumps(param_schema, indent=2, ensure_ascii=False)}

## 回测结果（第{round_num+1}轮）
- 总收益率: {result.total_return:+.2%}
- 夏普比率: {result.sharpe_ratio:.2f}
- 最大回撤: {result.max_drawdown:.2%}
- 胜率: {result.win_rate:.0%}
- 交易次数: {result.total_trades}
- 平均持仓: {result.avg_hold_days:.1f}天

## 最近10笔交易
{recent_trades}

## 历史优化记录
{self._format_history()}

## 要求
1. 分析问题在哪（是胜率低？回撤大？交易太少？）
2. 建议调整哪些参数，为什么
3. 返回JSON格式

返回:
{{"analysis": "问题分析", "params": {{"参数名": 新值}}, "reasoning": "调整理由", "stop": false}}

只返回JSON。"""
        return prompt

    def _format_recent_trades(self, trades: list) -> str:
        """格式化最近交易"""
        if not trades:
            return "无交易记录"

        lines = []
        for t in trades:
            lines.append(f"- {t.date} {t.code} {t.action} @ {t.price:.2f} ({t.reason})")
        return "\n".join(lines)

    def _format_history(self) -> str:
        """格式化历史优化记录"""
        if not self.history:
            return "无历史记录"

        lines = []
        for r in self.history:
            lines.append(
                f"第{r.round_num+1}轮: "
                f"收益={r.result['total_return']:+.2%} "
                f"夏普={r.result['sharpe_ratio']:.2f} "
                f"回撤={r.result['max_drawdown']:.2%} "
                f"胜率={r.result['win_rate']:.0%} "
                f"交易={r.result['total_trades']}笔"
            )
        return "\n".join(lines)

    def _parse_llm_response(self, content: str) -> dict:
        """解析LLM返回的JSON"""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 尝试从内容中提取JSON
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            # 解析失败
            return {
                "analysis": content,
                "params": {},
                "reasoning": "JSON解析失败",
                "stop": False,
            }

    def format_optimization_report(self, result: OptimizationResult) -> str:
        """格式化优化报告"""
        lines = [
            "=" * 60,
            f"策略优化报告: {result.strategy_name}",
            "=" * 60,
            "",
            f"初始收益率: {result.initial_return:+.2%}",
            f"最终收益率: {result.final_return:+.2%}",
            f"改进幅度: {result.improvement:+.2%}",
            f"最佳轮次: 第{result.best_round + 1}轮",
            "",
            "参数变化:",
        ]

        # 对比参数变化
        for key in result.initial_params:
            old = result.initial_params[key]
            new = result.final_params.get(key, old)
            if old != new:
                lines.append(f"  {key}: {old} → {new}")

        lines.append("")
        lines.append("优化历史:")
        for r in result.rounds:
            lines.append(
                f"  第{r.round_num+1}轮: "
                f"收益={r.result['total_return']:+.2%} "
                f"夏普={r.result['sharpe_ratio']:.2f} "
                f"回撤={r.result['max_drawdown']:.2%} "
                f"胜率={r.result['win_rate']:.0%} "
                f"交易={r.result['total_trades']}笔"
            )
            if r.suggestion.get("analysis"):
                lines.append(f"    LLM分析: {r.suggestion['analysis'][:100]}...")
            if r.suggestion.get("reasoning"):
                lines.append(f"    调整理由: {r.suggestion['reasoning'][:100]}...")

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)
