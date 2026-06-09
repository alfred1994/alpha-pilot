"""
调度层公共工具函数
====================================================================
提取自 closure_repair.py 和 paper_observer.py 的共享逻辑。
====================================================================
"""
import os


def force_paper_mode():
    """强制当前进程使用模拟盘（BROKER_MODE=paper）"""
    os.environ["BROKER_MODE"] = "paper"
    try:
        import config
        config.BROKER_MODE = "paper"
    except Exception:
        pass
    try:
        import execution.broker as broker
        broker.BROKER_MODE = "paper"
    except Exception:
        pass
