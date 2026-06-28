"""
自适应策略调整引擎
根据近期交易表现自动调整系统参数

核心逻辑:
1. 信号准确率追踪 — 哪个维度预测准，权重就调高
2. 来源有效性分析 — 哪个选股池出牛股，优先级调高
3. 阈值动态调整 — 胜率低时收紧，胜率高时放松
4. 仓位自适应 — 回撤大时自动降仓
"""
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

from config import (
    DATA_DIR, SIGNAL_WEIGHTS, TRADE_ADAPTIVE_MIN_SCORE_BASE,
    DECISION_BUY_THRESHOLD, STOP_LOSS, TAKE_PROFIT,
    MAX_SINGLE_PCT, MAX_POSITIONS,
)

logger = logging.getLogger("strategy.adaptive")

ADAPTIVE_FILE = os.path.join(DATA_DIR, "adaptive_state.json")
REVIEW_DIR = os.path.join(DATA_DIR, "reviews")

# ── 参数调整边界（防止过度调整）──
WEIGHT_MIN = 0.05
WEIGHT_MAX = 0.50
SCORE_MIN = 45
SCORE_MAX = 75
BUY_THRESHOLD_MIN = 50
BUY_THRESHOLD_MAX = 70
TOP_K_DELTA_MIN = -2
TOP_K_DELTA_MAX = 2
POSITION_SCALE_MIN = 0.50
POSITION_SCALE_MAX = 1.20


class AdaptiveEngine:
    """
    自适应策略调整引擎

    每次复盘后调用，根据近期表现自动微调参数。
    所有调整都有边界限制，防止过度拟合。
    """

    def __init__(
        self,
        adaptive_file: str = None,
        review_dir: str = None,
        data_dir: str = None,
        db_path: str = None,
        enable_versioning: bool = True,
        enable_ab_testing: bool = True,
    ):
        self.adaptive_file = adaptive_file or ADAPTIVE_FILE
        self.review_dir = review_dir or REVIEW_DIR
        self.data_dir = data_dir or DATA_DIR
        self.db_path = db_path
        self.enable_versioning = enable_versioning
        self.enable_ab_testing = enable_ab_testing
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """加载自适应状态"""
        default_state = {
            "version": 1,
            "last_update": None,
            "lookback_days": 10,          # 回看天数
            "adjustments": [],            # 调整历史
            "signal_accuracy": {},        # 各维度准确率
            "source_effectiveness": {},   # 各来源有效性
            "current_weights": dict(SIGNAL_WEIGHTS),
            "current_buy_threshold": DECISION_BUY_THRESHOLD,
            "current_min_score": TRADE_ADAPTIVE_MIN_SCORE_BASE,
            "current_top_k_delta": 0,     # 候选数量偏移，低胜率时减少买入标的
            "current_position_scale": 1.0, # 仓位缩放，低胜率时降低单笔仓位
            "weekly_stats": [],           # 周统计
        }
        if os.path.exists(self.adaptive_file):
            try:
                with open(self.adaptive_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for key, value in default_state.items():
                    state.setdefault(key, value)
                return state
            except Exception:
                pass
        return default_state

    def _save_state(self):
        """保存自适应状态"""
        os.makedirs(os.path.dirname(self.adaptive_file), exist_ok=True)
        with open(self.adaptive_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

        # 双写：自适应状态同步到 SQLite
        try:
            from data.database import Database
            with Database(db_path=self.db_path) as db:
                db.save_adaptive_state(self.state)
        except Exception as e:
            logger.warning(f"SQLite双写失败(不影响JSON): {e}")

    def analyze_and_adjust(self, days: int = 10) -> Dict:
        """
        主入口: 分析近期表现并调整参数

        Returns:
            调整报告 dict
        """
        logger.info(f"自适应分析开始 (回看{days}天)")
        self.state["lookback_days"] = days

        # 1. 加载近期交易记录
        trades = self._load_recent_trades(days)
        if len(trades) < 3:
            logger.info("交易记录不足3笔，跳过自适应调整")
            return {"status": "insufficient_data", "trade_count": len(trades)}

        # 2. 计算整体表现
        overall = self._calc_overall_stats(trades)

        # 3. 分析各信号维度准确率
        signal_stats = self._analyze_signal_accuracy(trades)

        # 4. 分析各来源有效性
        source_stats = self._analyze_source_effectiveness(trades)

        # 5. 调整参数

        # 记录调整前的参数（用于AB测试）
        old_params = self.get_adjusted_params()

        # 5-pre. 保存当前参数版本快照（P2-6 策略版本管理）
        if self.enable_versioning:
            version_db = None
            try:
                from strategy.versioning import StrategyVersionManager
                from data.database import Database
                version_db = Database(db_path=self.db_path).__enter__()
                vm = StrategyVersionManager(db=version_db)
                current_params = dict(old_params)
                perf_data = {
                    "win_rate": overall.get("win_rate", 0),
                    "pnl_pct": overall.get("total_pnl_pct", 0),
                    "trade_count": overall.get("sell_trades", 0),
                    "profit_factor": overall.get("profit_factor", 0),
                }
                # 获取当前市场环境
                regime = self.state.get("current_regime", "")
                vm.save_version(
                    params=current_params,
                    regime=regime,
                    description=f"自适应调整前快照",
                    performance=perf_data,
                )
            except Exception as e:
                logger.warning(f"策略版本快照保存失败(不影响调整): {e}")
            finally:
                if version_db is not None:
                    version_db.__exit__(None, None, None)

        adjustments = []

        # 5a. 调整信号权重
        weight_adj = self._adjust_signal_weights(signal_stats, overall)
        if weight_adj:
            adjustments.extend(weight_adj)

        # 5b. 调整买入阈值
        threshold_adj = self._adjust_thresholds(overall)
        if threshold_adj:
            adjustments.extend(threshold_adj)

        # 6. 生成建议
        suggestions = self._generate_suggestions(overall, signal_stats, source_stats)

        # 7. 保存状态
        self.state["last_update"] = datetime.now().isoformat()
        self.state["signal_accuracy"] = signal_stats
        self.state["source_effectiveness"] = source_stats
        self.state["adjustments"].append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "overall": overall,
            "adjustments": adjustments,
        })
        # 只保留最近30条调整记录
        self.state["adjustments"] = self.state["adjustments"][-30:]
        self._save_state()

        # 8. AB测试集成：如果有显著调整，创建AB测试
        if adjustments and self.enable_ab_testing:
            new_params = self.get_adjusted_params()
            # 检查是否有显著变化（至少一个参数有实际调整）
            has_significant_change = any(
                adj.get("old") != adj.get("new") for adj in adjustments
                if "old" in adj and "new" in adj
            )
            if has_significant_change:
                ab_db = None
                try:
                    from strategy.ab_test import ABTestManager
                    from data.database import Database
                    ab_db = Database(db_path=self.db_path).__enter__()
                    abm = ABTestManager(db=ab_db)
                    regime = self.state.get("current_regime", "")
                    abm.create_test(
                        control_params=old_params,
                        treatment_params=new_params,
                        control_regime=regime,
                        treatment_regime=regime,
                    )
                    logger.info("AB测试已创建: 对照组(旧参数) vs 实验组(新参数)")
                except Exception as e:
                    logger.debug(f"AB测试创建跳过: {e}")
                finally:
                    if ab_db is not None:
                        ab_db.__exit__(None, None, None)

        # 9. 评估运行中的AB测试，自动采用胜出参数
        if self.enable_ab_testing:
            ab_db = None
            try:
                from strategy.ab_test import ABTestManager
                from data.database import Database
                ab_db = Database(db_path=self.db_path).__enter__()
                abm = ABTestManager(db=ab_db)
                concluded = abm.evaluate_all_running()
                for test_result in concluded:
                    if test_result.get("winner") == "treatment":
                        best = abm.get_test_params(test_result["test_id"], "treatment")
                        if best:
                            logger.info(
                                f"AB测试 {test_result['test_id']} 实验组胜出，"
                                f"自动采用新参数"
                            )
                            self.state["current_weights"] = best.get(
                                "signal_weights", self.state.get("current_weights")
                            )
                            self.state["current_buy_threshold"] = best.get(
                                "buy_threshold", self.state.get("current_buy_threshold")
                            )
                            self.state["current_min_score"] = best.get(
                                "min_score", self.state.get("current_min_score")
                            )
                            self.state["current_top_k_delta"] = best.get(
                                "top_k_delta",
                                best.get("current_top_k_delta", self.state.get("current_top_k_delta", 0)),
                            )
                            self.state["current_position_scale"] = best.get(
                                "position_scale",
                                best.get("current_position_scale", self.state.get("current_position_scale", 1.0)),
                            )
                    elif test_result.get("winner") == "control":
                        logger.info(
                            f"AB测试 {test_result['test_id']} 对照组胜出，"
                            f"保持当前参数"
                        )
                    # tie 情况不做任何调整
            except Exception as e:
                logger.debug(f"AB测试评估跳过: {e}")
            finally:
                if ab_db is not None:
                    ab_db.__exit__(None, None, None)

        report = {
            "status": "ok",
            "overall": overall,
            "signal_stats": signal_stats,
            "source_stats": source_stats,
            "adjustments": adjustments,
            "suggestions": suggestions,
        }

        logger.info(f"自适应分析完成: 胜率={overall['win_rate']:.1%} 调整{len(adjustments)}项")
        return report

    def _load_recent_trades(self, days: int) -> List[dict]:
        """加载近期交易记录"""
        trades = []
        today = datetime.now()

        for i in range(days):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            # 从复盘文件加载 (两种命名格式)
            for pattern in [f"{date}.json", f"review_{date}.json"]:
                review_file = os.path.join(self.review_dir, pattern)
                if os.path.exists(review_file):
                    try:
                        with open(review_file, "r", encoding="utf-8") as f:
                            review = json.load(f)
                        for t in review.get("trade_reviews", []):
                            trades.append({**t, "date": date})
                    except Exception:
                        pass

            # 从交易记录加载
            trade_file = os.path.join(self.data_dir, "trades", f"{date}.json")
            if os.path.exists(trade_file):
                try:
                    with open(trade_file, "r", encoding="utf-8") as f:
                        day_trades = json.load(f)
                    for t in (day_trades if isinstance(day_trades, list) else []):
                        trades.append({**t, "date": date})
                except Exception:
                    pass

        return trades

    def _calc_overall_stats(self, trades: List[dict]) -> dict:
        """计算整体统计"""
        sells = [t for t in trades if t.get("action") == "SELL"]
        wins = [t for t in sells if t.get("result_pct", 0) > 0]
        losses = [t for t in sells if t.get("result_pct", 0) <= 0]

        total_pnl = sum(t.get("result_pct", 0) for t in sells)
        avg_win = sum(t.get("result_pct", 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.get("result_pct", 0) for t in losses) / len(losses) if losses else 0

        # 盈亏比
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 999

        return {
            "total_trades": len(trades),
            "sell_trades": len(sells),
            "win_count": len(wins),
            "lose_count": len(losses),
            "win_rate": len(wins) / len(sells) if sells else 0,
            "total_pnl_pct": total_pnl / 100,  # 转为小数
            "avg_win_pct": avg_win / 100,
            "avg_loss_pct": avg_loss / 100,
            "profit_factor": profit_factor,
            "expectancy": ((len(wins)/len(sells) * avg_win + len(losses)/len(sells) * avg_loss) / 100) if sells else 0,
        }

    def _analyze_signal_accuracy(self, trades: List[dict]) -> dict:
        """
        分析各信号维度的准确率

        对每笔卖出交易，看买入时各维度的分数，
        哪个维度分数高且最终盈利，说明该维度预测准。
        """
        dim_stats = defaultdict(lambda: {"total": 0, "correct": 0, "scores": [], "results": []})

        for t in trades:
            if t.get("action") != "SELL":
                continue
            result_pct = t.get("result_pct", 0)
            hit = result_pct > 0

            # 从信号缓存或决策记录获取维度分数
            dims = t.get("dimensions", {})
            if not dims:
                # 尝试从signal_score推算
                score = t.get("signal_score", 50)
                dims = {"overall": score}

            for dim_name, dim_score in dims.items():
                stats = dim_stats[dim_name]
                stats["total"] += 1
                stats["scores"].append(dim_score)
                stats["results"].append(result_pct)
                # 高分买入+盈利 或 低分卖出+亏损 = 准确
                if (dim_score > 60 and hit) or (dim_score < 40 and not hit):
                    stats["correct"] += 1

        # 计算准确率
        result = {}
        for dim_name, stats in dim_stats.items():
            if stats["total"] > 0:
                accuracy = stats["correct"] / stats["total"]
                avg_score = sum(stats["scores"]) / len(stats["scores"])
                avg_result = sum(stats["results"]) / len(stats["results"])
                result[dim_name] = {
                    "total": stats["total"],
                    "accuracy": accuracy,
                    "avg_score": avg_score,
                    "avg_result_pct": avg_result,
                }

        return result

    def _analyze_source_effectiveness(self, trades: List[dict]) -> dict:
        """
        分析各选股来源的有效性

        哪个来源选出的股票最终盈利多，权重就调高
        """
        source_stats = defaultdict(lambda: {"total": 0, "wins": 0, "total_pnl": 0})

        for t in trades:
            if t.get("action") != "SELL":
                continue
            source = t.get("source", "unknown")
            result_pct = t.get("result_pct", 0)

            stats = source_stats[source]
            stats["total"] += 1
            stats["total_pnl"] += result_pct
            if result_pct > 0:
                stats["wins"] += 1

        result = {}
        for source, stats in source_stats.items():
            if stats["total"] > 0:
                result[source] = {
                    "total": stats["total"],
                    "win_rate": stats["wins"] / stats["total"],
                    "avg_pnl": (stats["total_pnl"] / stats["total"]) / 100,  # 转为小数
                    "total_pnl": stats["total_pnl"] / 100,
                }

        return result

    def _adjust_signal_weights(self, signal_stats: dict, overall: dict) -> List[dict]:
        """
        根据信号准确率调整5维权重

        规则:
        - 准确率 > 60% 的维度，权重 +2%
        - 准确率 < 40% 的维度，权重 -2%
        - 每次调整幅度 ±2%，防止震荡
        """
        adjustments = []
        current_weights = self.state.get("current_weights", dict(SIGNAL_WEIGHTS))

        for dim_name, stats in signal_stats.items():
            if dim_name == "overall" or stats["total"] < 3:
                continue

            accuracy = stats["accuracy"]
            current = current_weights.get(dim_name, 0.1)

            if accuracy > 0.60:
                new_weight = min(WEIGHT_MAX, current + 0.02)
                if new_weight != current:
                    adjustments.append({
                        "type": "weight",
                        "dimension": dim_name,
                        "old": current,
                        "new": new_weight,
                        "reason": f"准确率{accuracy:.0%}偏高，权重+2%",
                    })
                    current_weights[dim_name] = new_weight

            elif accuracy < 0.40:
                new_weight = max(WEIGHT_MIN, current - 0.02)
                if new_weight != current:
                    adjustments.append({
                        "type": "weight",
                        "dimension": dim_name,
                        "old": current,
                        "new": new_weight,
                        "reason": f"准确率{accuracy:.0%}偏低，权重-2%",
                    })
                    current_weights[dim_name] = new_weight

        # 归一化权重（确保总和=1）
        total = sum(current_weights.values())
        if total > 0 and abs(total - 1.0) > 0.01:
            for k in current_weights:
                current_weights[k] /= total

        self.state["current_weights"] = current_weights
        return adjustments

    def _adjust_thresholds(self, overall: dict) -> List[dict]:
        """
        根据整体表现调整阈值

        规则:
        - 胜率 < 35%: 买入阈值+2, 最低入选分+2, TopK-1, 仓位*0.8
        - 胜率 > 55%: 买入阈值-1, 最低入选分-1, TopK+1, 仓位+10%
        - 盈亏比 < 1.0: 止损收紧1%
        - 盈亏比 > 2.0: 止损放松1%
        """
        adjustments = []
        win_rate = overall["win_rate"]
        pf = overall["profit_factor"]

        # 买入阈值调整
        current_bt = self.state.get("current_buy_threshold", DECISION_BUY_THRESHOLD)
        if win_rate < 0.35:
            new_bt = min(BUY_THRESHOLD_MAX, current_bt + 2)
            if new_bt != current_bt:
                adjustments.append({
                    "type": "buy_threshold",
                    "old": current_bt,
                    "new": new_bt,
                    "reason": f"胜率{win_rate:.0%}偏低，提高买入门槛",
                })
                self.state["current_buy_threshold"] = new_bt
        elif win_rate > 0.55:
            new_bt = max(BUY_THRESHOLD_MIN, current_bt - 1)
            if new_bt != current_bt:
                adjustments.append({
                    "type": "buy_threshold",
                    "old": current_bt,
                    "new": new_bt,
                    "reason": f"胜率{win_rate:.0%}良好，适当放宽",
                })
                self.state["current_buy_threshold"] = new_bt

        # 最低入选分调整
        current_ms = self.state.get("current_min_score", TRADE_ADAPTIVE_MIN_SCORE_BASE)
        if win_rate < 0.35:
            new_ms = min(SCORE_MAX, current_ms + 2)
            if new_ms != current_ms:
                adjustments.append({
                    "type": "min_score",
                    "old": current_ms,
                    "new": new_ms,
                    "reason": f"胜率低，提高选股门槛",
                })
                self.state["current_min_score"] = new_ms
        elif win_rate > 0.55:
            new_ms = max(SCORE_MIN, current_ms - 1)
            if new_ms != current_ms:
                adjustments.append({
                    "type": "min_score",
                    "old": current_ms,
                    "new": new_ms,
                    "reason": f"胜率良好，适当放宽选股",
                })
                self.state["current_min_score"] = new_ms

        # 候选数量调整：亏损期减少买入标的，表现恢复后逐步放开
        current_top_k_delta = self.state.get("current_top_k_delta", 0)
        if win_rate < 0.35:
            new_top_k_delta = max(TOP_K_DELTA_MIN, current_top_k_delta - 1)
            if new_top_k_delta != current_top_k_delta:
                adjustments.append({
                    "type": "top_k_delta",
                    "old": current_top_k_delta,
                    "new": new_top_k_delta,
                    "reason": f"胜率低，减少下一轮候选买入数量",
                })
                self.state["current_top_k_delta"] = new_top_k_delta
        elif win_rate > 0.55:
            new_top_k_delta = min(TOP_K_DELTA_MAX, current_top_k_delta + 1)
            if new_top_k_delta != current_top_k_delta:
                adjustments.append({
                    "type": "top_k_delta",
                    "old": current_top_k_delta,
                    "new": new_top_k_delta,
                    "reason": f"胜率良好，适当增加候选买入数量",
                })
                self.state["current_top_k_delta"] = new_top_k_delta

        # 仓位缩放调整：复盘结果直接影响下一轮单笔仓位上限
        current_position_scale = self.state.get("current_position_scale", 1.0)
        if win_rate < 0.35:
            new_position_scale = max(POSITION_SCALE_MIN, round(current_position_scale * 0.8, 4))
            if new_position_scale != current_position_scale:
                adjustments.append({
                    "type": "position_scale",
                    "old": current_position_scale,
                    "new": new_position_scale,
                    "reason": f"胜率低，降低下一轮单笔仓位",
                })
                self.state["current_position_scale"] = new_position_scale
        elif win_rate > 0.55 and pf > 1.2:
            new_position_scale = min(POSITION_SCALE_MAX, round(current_position_scale + 0.1, 4))
            if new_position_scale != current_position_scale:
                adjustments.append({
                    "type": "position_scale",
                    "old": current_position_scale,
                    "new": new_position_scale,
                    "reason": f"胜率和盈亏比较好，逐步恢复单笔仓位",
                })
                self.state["current_position_scale"] = new_position_scale

        return adjustments

    def _generate_suggestions(self, overall: dict, signal_stats: dict, source_stats: dict) -> List[str]:
        """生成人工可读的优化建议"""
        suggestions = []
        win_rate = overall["win_rate"]
        pf = overall["profit_factor"]

        # 胜率建议
        if win_rate < 0.30:
            suggestions.append("⚠️ 胜率极低(<30%)，建议暂停交易1-2天，检查信号逻辑")
        elif win_rate < 0.40:
            suggestions.append("⚠️ 胜率偏低，系统已自动收紧参数，建议观察2-3天")
        elif win_rate > 0.60:
            suggestions.append("✅ 胜率良好(>60%)，系统已适当放宽参数")

        # 盈亏比建议
        if pf < 1.0:
            suggestions.append("⚠️ 盈亏比<1，亏损大于盈利，建议检查止损逻辑")
        elif pf > 2.5:
            suggestions.append("✅ 盈亏比优秀(>2.5)，策略运行良好")

        # 信号维度建议
        for dim, stats in signal_stats.items():
            if dim == "overall":
                continue
            if stats["total"] >= 3:
                if stats["accuracy"] < 0.35:
                    suggestions.append(f"📉 {dim}维度准确率仅{stats['accuracy']:.0%}，已降权")
                elif stats["accuracy"] > 0.65:
                    suggestions.append(f"📈 {dim}维度准确率{stats['accuracy']:.0%}，已加权")

        # 来源建议
        best_source = None
        worst_source = None
        for src, stats in source_stats.items():
            if stats["total"] >= 2:
                if best_source is None or stats["win_rate"] > best_source[1]["win_rate"]:
                    best_source = (src, stats)
                if worst_source is None or stats["win_rate"] < worst_source[1]["win_rate"]:
                    worst_source = (src, stats)

        if best_source and worst_source and best_source[0] != worst_source[0]:
            suggestions.append(
                f"🏆 来源对比: {best_source[0]}胜率{best_source[1]['win_rate']:.0%} "
                f"vs {worst_source[0]}胜率{worst_source[1]['win_rate']:.0%}"
            )

        return suggestions

    def get_adjusted_params(self) -> dict:
        """获取调整后的参数（供其他模块读取）"""
        return {
            "signal_weights": self.state.get("current_weights", SIGNAL_WEIGHTS),
            "buy_threshold": self.state.get("current_buy_threshold", DECISION_BUY_THRESHOLD),
            "min_score": self.state.get("current_min_score", TRADE_ADAPTIVE_MIN_SCORE_BASE),
            "top_k_delta": self.state.get("current_top_k_delta", 0),
            "position_scale": self.state.get("current_position_scale", 1.0),
        }

    def format_report(self, report: Dict) -> str:
        """格式化自适应报告"""
        if report.get("status") == "insufficient_data":
            return f"自适应分析: 交易记录不足({report.get('trade_count', 0)}笔)，需要至少3笔"

        lines = [
            "=" * 50,
            "自适应策略分析报告",
            "=" * 50,
        ]

        overall = report.get("overall", {})
        lines.append(f"分析周期: 近{report.get('overall', {}).get('sell_trades', 0)}笔卖出交易")
        lines.append(f"胜率: {overall.get('win_rate', 0):.1%}")
        lines.append(f"盈亏比: {overall.get('profit_factor', 0):.2f}")
        lines.append(f"平均盈利: {overall.get('avg_win_pct', 0):+.2%}")
        lines.append(f"平均亏损: {overall.get('avg_loss_pct', 0):+.2%}")
        lines.append(f"期望收益: {overall.get('expectancy', 0):+.2%}")
        lines.append("-" * 50)

        # 信号维度准确率
        signal_stats = report.get("signal_stats", {})
        if signal_stats:
            lines.append("信号维度准确率:")
            for dim, stats in signal_stats.items():
                if dim == "overall":
                    continue
                emoji = "📈" if stats["accuracy"] > 0.55 else "📉" if stats["accuracy"] < 0.45 else "➡️"
                lines.append(f"  {emoji} {dim}: {stats['accuracy']:.0%} ({stats['total']}笔)")
            lines.append("-" * 50)

        # 来源有效性
        source_stats = report.get("source_stats", {})
        if source_stats:
            lines.append("选股来源有效性:")
            for src, stats in sorted(source_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True):
                emoji = "🏆" if stats["win_rate"] > 0.5 else "⚠️" if stats["win_rate"] < 0.3 else "➡️"
                lines.append(f"  {emoji} {src}: 胜率{stats['win_rate']:.0%} 均盈{stats['avg_pnl']:+.2%} ({stats['total']}笔)")
            lines.append("-" * 50)

        # 参数调整
        adjustments = report.get("adjustments", [])
        if adjustments:
            lines.append("参数调整:")
            for adj in adjustments:
                lines.append(f"  • {adj['reason']}")
                if "old" in adj and "new" in adj:
                    lines.append(f"    {adj.get('dimension', adj.get('type', ''))}: {adj['old']:.3f} → {adj['new']:.3f}")
        else:
            lines.append("参数调整: 无需调整")

        # 建议
        suggestions = report.get("suggestions", [])
        if suggestions:
            lines.append("-" * 50)
            lines.append("优化建议:")
            for s in suggestions:
                lines.append(f"  {s}")

        lines.append("=" * 50)
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = AdaptiveEngine()
    report = engine.analyze_and_adjust(days=10)
    print(engine.format_report(report))
