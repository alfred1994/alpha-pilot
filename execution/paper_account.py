"""
模拟盘账户模块
JSON持久化存储，支持买入/卖出/查持仓/查净值
数据保存到 data/paper_account.json，并同步账户状态到 SQLite
"""
import json
import os
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional
from config import (
    INITIAL_CAPITAL,
    MAX_POSITIONS,
    MAX_SINGLE_PCT,
    STOP_LOSS,
    TAKE_PROFIT,
    TRAILING_STOP,
    PAPER_ACCOUNT_FILE,
    COMMISSION_RATE,
    STAMP_TAX_RATE,
    MIN_TRADE_UNIT,
    USE_ATR_STOP,
    ATR_MULTIPLIER,
)

logger = logging.getLogger("execution.account")


class PaperAccount:
    """
    模拟盘账户

    JSON结构:
    {
        "initial_capital": 1000000,
        "cash": 900000,
        "positions": {
            "600519": {
                "code": "600519", "name": "贵州茅台",
                "shares": 100, "buy_price": 1800.0,
                "buy_date": "2024-01-15", "cost": 180000.0,
                "highest_price": 1850.0
            }
        },
        "trades": [...],
        "created_at": "...",
        "updated_at": "..."
    }
    """

    # 【Phase1-Task5】添加线程锁，防止并发读写JSON文件导致数据损坏
    _lock = threading.RLock()  # 可重入锁：sell()内调用_save()需要嵌套加锁

    def __init__(self, filepath: str = None, db_path: str = None):
        self.filepath = filepath or PAPER_ACCOUNT_FILE
        self.db_path = db_path
        self._ensure_data_dir()
        self._load()
        self._sync_state_to_sqlite()

    def _ensure_data_dir(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)

    def _load(self):
        """从JSON加载账户状态（加锁防止并发读取损坏数据）"""
        with self._lock:
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.initial_capital = data.get("initial_capital", INITIAL_CAPITAL)
                    self.cash = data.get("cash", self.initial_capital)
                    self.positions = data.get("positions", {})
                    self.trades = data.get("trades", [])
                    self.created_at = data.get("created_at", datetime.now().isoformat())
                    self.updated_at = data.get("updated_at", datetime.now().isoformat())
                    logger.info(f"加载账户: 现金={self.cash:.0f} 持仓={len(self.positions)}只")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"账户文件损坏, 重新初始化: {e}")
                    self._init_new()
            else:
                self._init_new()

    def _init_new(self):
        """初始化新账户"""
        self.initial_capital = INITIAL_CAPITAL
        self.cash = INITIAL_CAPITAL
        self.positions = {}
        self.trades = []
        self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()
        self._save()
        logger.info(f"新建模拟盘账户: 初始资金={INITIAL_CAPITAL:,.0f}")

    def _save(self):
        """保存到JSON（加锁防止并发写入损坏文件）"""
        self.updated_at = datetime.now().isoformat()
        data = {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": self.positions,
            "trades": self.trades,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        with self._lock:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        self._sync_state_to_sqlite(data)

    def _sync_state_to_sqlite(self, data: dict = None):
        """同步账户状态到SQLite（失败不影响JSON主流程）"""
        data = data or {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": self.positions,
            "trades": self.trades,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        try:
            from data.database import Database
            with Database(db_path=self.db_path) as db:
                db.save_account_state({
                    **data,
                    "total_assets": self.total_assets(),
                })
        except Exception as e:
            logger.warning(f"SQLite账户状态同步失败(不影响JSON): {e}")

    # ── 查询 ──────────────────────────────────────────────────────

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def can_buy(self) -> bool:
        return self.position_count < MAX_POSITIONS

    def has_position(self, code: str) -> bool:
        return code in self.positions

    def get_position(self, code: str) -> Optional[dict]:
        return self.positions.get(code)

    def market_value(self, prices: Dict[str, float] = None) -> float:
        """持仓市值（按买入价或最新价）"""
        value = 0.0
        for code, pos in self.positions.items():
            if prices and code in prices:
                value += prices[code] * pos["shares"]
            else:
                value += pos["buy_price"] * pos["shares"]
        return value

    def total_assets(self, prices: Dict[str, float] = None) -> float:
        return self.cash + self.market_value(prices)

    def max_buy_amount(self) -> float:
        """单只最大买入金额"""
        return self.total_assets() * MAX_SINGLE_PCT

    def pnl_summary(self, prices: Dict[str, float] = None) -> dict:
        """盈亏汇总"""
        total_assets = self.total_assets(prices)
        pnl = total_assets - self.initial_capital
        pnl_pct = pnl / self.initial_capital if self.initial_capital > 0 else 0
        return {
            "initial_capital": self.initial_capital,
            "total_assets": total_assets,
            "cash": self.cash,
            "market_value": self.market_value(prices),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "positions": self.position_count,
        }

    # ── 交易 ──────────────────────────────────────────────────────

    def buy(
        self,
        code: str,
        name: str,
        price: float,
        shares: int = None,
        amount: float = None,
        reason: str = "信号买入",
        atr: float = None,
    ) -> Optional[dict]:
        """
        买入股票（加锁保证读-改-写的原子性）

        Args:
            code: 股票代码
            name: 股票名称
            price: 买入价格
            shares: 买入股数（与amount二选一）
            amount: 买入金额（自动计算整百股数）
            reason: 买入原因
            atr: 买入时的ATR值（用于动态止损）

        Returns:
            交易记录 dict 或 None
        """
        with self._lock:
            if self.has_position(code):
                logger.warning(f"已持有 {name}({code}), 不重复买入")
                return None

            if not self.can_buy():
                logger.warning(f"持仓已满({MAX_POSITIONS}只), 无法买入")
                return None

            if shares is None and amount is None:
                amount = self.max_buy_amount()

            if shares is None:
                shares = int(amount / price / MIN_TRADE_UNIT) * MIN_TRADE_UNIT

            if shares <= 0:
                logger.warning(f"资金不足以买入1手 {name}")
                return None

            # 计算实际成本（含佣金）
            cost = shares * price
            commission = max(cost * COMMISSION_RATE, 5)  # 最低5元
            total_cost = cost + commission

            if total_cost > self.cash:
                # 缩减排数
                shares = int(self.cash / price / (1 + COMMISSION_RATE) / MIN_TRADE_UNIT) * MIN_TRADE_UNIT
                if shares <= 0:
                    logger.warning("可用资金不足")
                    return None
                cost = shares * price
                commission = max(cost * COMMISSION_RATE, 5)
                total_cost = cost + commission

            self.cash -= total_cost
            self.positions[code] = {
                "code": code,
                "name": name,
                "shares": shares,
                "buy_price": price,
                "buy_date": datetime.now().strftime("%Y-%m-%d"),
                "cost": cost,
                "highest_price": price,
                "atr_at_buy": atr if atr and atr > 0 else 0.0,
            }

            trade = {
                "action": "BUY",
                "code": code,
                "name": name,
                "price": price,
                "shares": shares,
                "amount": cost,
                "commission": commission,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
                "cash_after": self.cash,
            }
            self.trades.append(trade)

            self._save()

            # 【双写】JSON保存成功后，同步写入SQLite trades表
            # SQLite写入失败不影响已保存的JSON数据
            try:
                from data.database import Database
                with Database(db_path=self.db_path) as db:
                    db.save_trade_record(trade)
                    # 同步持仓到positions表
                    db.upsert_position({
                        "code": code,
                        "name": name,
                        "shares": shares,
                        "buy_price": price,
                        "buy_date": datetime.now().strftime("%Y-%m-%d"),
                        "cost": cost,
                        "highest_price": price,
                        "current_price": price,
                    })
            except Exception as e:
                logger.warning(f"SQLite双写失败(不影响JSON): {e}")

            logger.info(f"买入 {name}({code}) {shares}股 @ {price} 金额={cost:.0f} 佣金={commission:.1f}")
            return trade

    def sell(
        self,
        code: str,
        price: float,
        shares: int = None,
        reason: str = "信号卖出",
    ) -> Optional[dict]:
        """
        卖出股票（加锁保证读-改-写的原子性）

        Args:
            code: 股票代码
            price: 卖出价格
            shares: 卖出股数（None=全部卖出）
            reason: 卖出原因

        Returns:
            交易记录 dict 或 None
        """
        with self._lock:
            if code not in self.positions:
                logger.warning(f"未持有 {code}, 无法卖出")
                return None

            pos = self.positions[code]
            if shares is None:
                shares = pos["shares"]

            shares = min(shares, pos["shares"])

            # 【TaskB】卖出前保存持仓快照，供SQLite同步使用
            pos_snapshot = dict(pos)
            is_full_sell = shares >= pos["shares"]

            # 计算到手金额（扣佣金和印花税）
            amount = shares * price
            commission = max(amount * COMMISSION_RATE, 5)
            stamp_tax = amount * STAMP_TAX_RATE
            net_amount = amount - commission - stamp_tax

            self.cash += net_amount

            # 更新持仓
            if is_full_sell:
                del self.positions[code]
            else:
                pos["shares"] -= shares

            profit_pct = (price - pos_snapshot["buy_price"]) / pos_snapshot["buy_price"] * 100

            trade = {
                "action": "SELL",
                "code": code,
                "name": pos_snapshot["name"],
                "price": price,
                "shares": shares,
                "amount": amount,
                "commission": commission,
                "stamp_tax": stamp_tax,
                "profit_pct": profit_pct,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
                "cash_after": self.cash,
            }
            self.trades.append(trade)

            self._save()

            # 【双写】JSON保存成功后，同步写入SQLite trades表
            # SQLite写入失败不影响已保存的JSON数据
            try:
                from data.database import Database
                with Database(db_path=self.db_path) as db:
                    db.save_trade_record(trade)
                    # 同步持仓变更到positions表
                    if is_full_sell:
                        # 全仓卖出，删除持仓记录
                        db.delete_position(code)
                    else:
                        # 部分卖出，更新持仓股数
                        remaining = pos_snapshot["shares"] - shares
                        db.upsert_position({
                            "code": code,
                            "name": pos_snapshot["name"],
                            "shares": remaining,
                            "buy_price": pos_snapshot["buy_price"],
                            "buy_date": pos_snapshot.get("buy_date", ""),
                            "cost": pos_snapshot["buy_price"] * remaining,
                            "highest_price": pos_snapshot.get("highest_price", pos_snapshot["buy_price"]),
                            "current_price": price,
                        })
            except Exception as e:
                logger.warning(f"SQLite双写失败(不影响JSON): {e}")

            logger.info(
                f"卖出 {pos_snapshot['name']}({code}) {shares}股 @ {price} "
                f"盈亏={profit_pct:+.1f}% 原因={reason}"
            )
            return trade

    def update_highest_price(self, code: str, price: float):
        """更新持仓最高价（用于移动止损）"""
        if code in self.positions:
            if price > self.positions[code]["highest_price"]:
                self.positions[code]["highest_price"] = price
                self._save()

    def check_stop_conditions(
        self,
        prices: Dict[str, float],
        atr_map: Dict[str, float] = None,
    ) -> List[dict]:
        """
        检查止损止盈条件（使用StopLossManager，支持ATR动态止损）

        Args:
            prices: {code: current_price}
            atr_map: {code: atr_value} ATR值（可选，优先使用买入时ATR）

        Returns:
            触发的卖出交易列表
        """
        from risk.stop_loss import StopLossManager

        slm = StopLossManager(
            atr_multiplier=ATR_MULTIPLIER,
            use_atr=USE_ATR_STOP,
        )

        triggered = []
        for code in list(self.positions.keys()):
            if code not in prices:
                continue

            pos = self.positions[code]
            current = prices[code]

            # 更新最高价
            self.update_highest_price(code, current)

            # 获取ATR：优先用买入时记录的，其次用外部传入的
            atr = pos.get("atr_at_buy", 0)
            if not atr and atr_map:
                atr = atr_map.get(code, 0)

            signal = slm.check_stop(
                code=code,
                name=pos.get("name", code),
                buy_price=pos["buy_price"],
                current_price=current,
                highest_price=pos.get("highest_price", pos["buy_price"]),
                atr=atr if atr > 0 else None,
            )

            if signal:
                trade = self.sell(code, current, reason=signal.reason)
                if trade:
                    triggered.append(trade)

        return triggered

    def reset(self):
        """重置账户（慎用）"""
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
        self._init_new()
        logger.info("账户已重置")

    # ── 展示 ──────────────────────────────────────────────────────

    def format_status(self, prices: Dict[str, float] = None) -> str:
        """格式化账户状态"""
        pnl = self.pnl_summary(prices)
        lines = [
            "=" * 55,
            "模拟盘账户",
            "=" * 55,
            f"初始资金:   {pnl['initial_capital']:>14,.0f}",
            f"可用现金:   {pnl['cash']:>14,.0f}",
            f"持仓市值:   {pnl['market_value']:>14,.0f}",
            f"总资产:     {pnl['total_assets']:>14,.0f}",
            f"总盈亏:     {pnl['pnl']:>+14,.0f} ({pnl['pnl_pct']:+.2%})",
            f"持仓数:     {pnl['positions']}/{MAX_POSITIONS}",
            "-" * 55,
        ]

        if self.positions:
            lines.append(f"{'代码':<8} {'名称':<8} {'股数':>6} {'成本价':>8} {'最高价':>8}")
            lines.append("-" * 55)
            for code, pos in self.positions.items():
                lines.append(
                    f"{pos['code']:<8} {pos['name']:<8} {pos['shares']:>6} "
                    f"{pos['buy_price']:>8.2f} {pos['highest_price']:>8.2f}"
                )
        else:
            lines.append("空仓")

        lines.append("=" * 55)
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    account = PaperAccount()
    print(account.format_status())

    # 测试买入
    trade = account.buy("600519", "贵州茅台", 1800.0, amount=200000)
    if trade:
        print(f"\n买入成功: {trade}")

    print(account.format_status())

    # 测试卖出
    trade = account.sell("600519", 1850.0, reason="信号卖出")
    if trade:
        print(f"\n卖出成功: {trade}")

    print(account.format_status())
