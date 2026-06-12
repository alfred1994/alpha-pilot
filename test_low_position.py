"""
测试低位选股器
"""
import logging
from strategy.stock_picker import pick_stocks, format_picker_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s"
)

print("=" * 70)
print("测试1: 纯低位模式（关闭涨停板/龙虎榜/异动放量）")
print("=" * 70)
candidates = pick_stocks(
    top_n=15,
    min_score=30,
    low_position_mode=True,  # 纯低位模式
)
print(format_picker_report(candidates))

print("\n" + "=" * 70)
print("测试2: 混合模式（低位+追高，看谁分数高）")
print("=" * 70)
candidates = pick_stocks(
    top_n=15,
    min_score=40,
    use_low_position=True,  # 加入低位维度
    low_position_mode=False,  # 保留其他维度
)
print(format_picker_report(candidates))
