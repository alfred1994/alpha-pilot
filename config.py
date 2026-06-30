"""
全局配置模块
资金、风控参数、5维打分权重、文件路径
"""
import os

# ══════════════════════════════════════════════════════════════════
# 路径配置
# ══════════════════════════════════════════════════════════════════
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PAPER_ACCOUNT_FILE = os.path.join(DATA_DIR, "paper_account.json")

# ══════════════════════════════════════════════════════════════════
# 资金配置
# ══════════════════════════════════════════════════════════════════
INITIAL_CAPITAL = 1_000_000      # 初始资金 100万
MAX_POSITIONS = 5                # 最大持仓数
MAX_SINGLE_PCT = 0.20            # 单只股票持仓上限 20%
COMMISSION_RATE = 0.0003         # 佣金费率 万三
STAMP_TAX_RATE = 0.001           # 印花税 千一（卖出）
MIN_TRADE_UNIT = 100             # A股最小交易单位 100股

# ══════════════════════════════════════════════════════════════════
# 风控参数
# ══════════════════════════════════════════════════════════════════
STOP_LOSS = -0.08                # 止损 -8%（A股波动大，-5%太紧）
TAKE_PROFIT = 0.10               # 止盈 +10%
TRAILING_STOP = -0.03            # 移动止损：最高点回撤 -3%
MAX_DRAWDOWN = -0.15             # 最大回撤 -15%（触发熔断）
CIRCUIT_BREAKER_DAYS = 1         # 熔断暂停天数

# ATR动态止损配置
USE_ATR_STOP = True              # 是否启用ATR动态止损（False则用固定止损）
ATR_PERIOD = 14                  # ATR计算周期（14日标准）
ATR_MULTIPLIER = 2.0             # ATR倍数（止损价=买入价-N倍ATR）

# ══════════════════════════════════════════════════════════════════
# 5维打分权重
# ══════════════════════════════════════════════════════════════════
SIGNAL_WEIGHTS = {
    "technical": 0.40,           # 技术面 40%（主要信号）
    "capital": 0.10,             # 资金面 10%（数据不稳定）
    "sentiment": 0.25,           # 舆情面 25%（LLM分析）
    "emotion": 0.15,             # 情绪面 15%（市场整体）
    "fundamental": 0.10,         # 基本面 10%（数据不稳定）
}

# ══════════════════════════════════════════════════════════════════
# 选股器参数
# ══════════════════════════════════════════════════════════════════
PICKER_TOP_N = 10                # 选股器输出候选数量
PICKER_MIN_SCORE = 20            # 最低入选分数（原55，现20，适应低位股）
PICKER_MAX_PE = 200              # 最大市盈率过滤
PICKER_MIN_AMOUNT = 5000         # 最小成交额（万元）

# 交易执行层的自适应最低分基准，独立于低位候选池的宽松入选线。
TRADE_ADAPTIVE_MIN_SCORE_BASE = 55

# ══════════════════════════════════════════════════════════════════
# 决策引擎参数
# ══════════════════════════════════════════════════════════════════
DECISION_BUY_THRESHOLD = 60      # 综合分 >= 60 触发买入（原65太高）
DECISION_SELL_THRESHOLD = 35     # 综合分 <= 35 触发卖出
DECISION_MIN_CONFIDENCE = 0.4    # 最低置信度

# ══════════════════════════════════════════════════════════════════
# 调度参数
# ══════════════════════════════════════════════════════════════════
REBALANCE_HOUR = 14              # 调仓时间：14:50
REBALANCE_MINUTE = 50

# ══════════════════════════════════════════════════════════════════
# AI交易员配置
# ══════════════════════════════════════════════════════════════════
USE_LLM_TRADER = True             # True=用LLM决策, False=用加权打分
USE_LLM_REGIME = True             # True=用LLM识别市场环境, False=用规则
BROKER_MODE = os.environ.get("BROKER_MODE", "paper").lower()  # paper=模拟盘, real=预留实盘适配器

# ══════════════════════════════════════════════════════════════════
# 选股混合模式配置
# ══════════════════════════════════════════════════════════════════
USE_LOW_POSITION = True           # 启用低位潜力股
LOW_POSITION_MODE = True          # 低位模式（关闭追高维度）
LIMIT_UP_SCORE_REDUCE = True      # 降低涨停板权重
LIMIT_UP_BASE_SCORE = 10          # 涨停板基础分（原40，现10）
LIMIT_UP_DAY_SCORE = 5            # 每连板加分（原15，现5）
LIMIT_UP_MAX_SCORE = 30           # 涨停板最高分（原80，现30）

# ══════════════════════════════════════════════════════════════════
# 自动盯盘配置
# ══════════════════════════════════════════════════════════════════
AUTO_LOOP_INTERVAL = int(os.environ.get("AUTO_LOOP_INTERVAL", "60"))       # 自动盯盘主循环间隔(秒)
AUTO_SCAN_INTERVAL = int(os.environ.get("AUTO_SCAN_INTERVAL", "1800"))     # 盘中扫描间隔(秒)
AUTO_STOP_INTERVAL = int(os.environ.get("AUTO_STOP_INTERVAL", "60"))       # 止损巡检间隔(秒)
AUTO_REVIEW_AFTER = os.environ.get("AUTO_REVIEW_AFTER", "15:05")           # 收盘复盘最早时间
AUTO_NOTIFY_ENABLED = os.environ.get("AUTO_NOTIFY_ENABLED", "1") == "1"    # 自动盯盘关键动作通知
AUTO_WATCH_INTERVAL = int(os.environ.get("AUTO_WATCH_INTERVAL", "300"))    # 盘中轻量看盘间隔(秒)
AUTO_RESCUE_SCAN_INTERVAL = int(os.environ.get("AUTO_RESCUE_SCAN_INTERVAL", "900"))  # 救援扫描间隔(秒)
AUTO_WATCHLIST_TTL = int(os.environ.get("AUTO_WATCHLIST_TTL", "3600"))     # 观察池有效期(秒)
AUTO_RESCUE_MAX_TOPK = int(os.environ.get("AUTO_RESCUE_MAX_TOPK", "5"))    # 救援扫描最多关注TopK
AUTO_RESCUE_POSITION_SCALE = float(os.environ.get("AUTO_RESCUE_POSITION_SCALE", "0.4"))  # 救援小仓缩放

# ══════════════════════════════════════════════════════════════════
# 系统级风控参数
# ══════════════════════════════════════════════════════════════════
DAILY_LOSS_LIMIT = -0.05         # 单日亏损阈值 -5%（次日禁止开新仓）
CONSECUTIVE_LOSS_DAYS = 3        # 连续亏损天数阈值（触发降仓）
POSITION_REDUCE_RATIO = 0.5      # 降仓缩放系数（连亏后仓位降至50%）

# ══════════════════════════════════════════════════════════════════
# 可转债T+0策略配置
# ══════════════════════════════════════════════════════════════════
CB_MAX_PREMIUM = 0.30        # 最大溢价率30%
CB_STOP_LOSS = -0.03         # 可转债止损-3%
CB_SINGLE_POSITION = 0.20    # 单只可转债仓位上限20%
CB_MIN_SCORE = 70            # 最低入选分数70
CB_T0_ENABLED = True         # 是否启用可转债T+0
