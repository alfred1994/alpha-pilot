#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.llm_trader import _parse_decision_response

def run_tests():
    # 1. 干净 JSON 格式
    raw1 = '{"action": "BUY", "confidence": 0.85, "reasoning": "技术面突破阻力位"}'
    action1, conf1, reason1 = _parse_decision_response(raw1)
    assert action1 == "BUY"
    assert conf1 == 0.85
    assert reason1 == "技术面突破阻力位"
    print("Test 1 Passed: Clean JSON")

    # 2. Markdown 代码块 JSON
    raw2 = '这里是分析...\n```json\n{"action": "SELL", "confidence": 0.70, "reasoning": "资金流出且破位"}\n```\n其余文字'
    action2, conf2, reason2 = _parse_decision_response(raw2)
    assert action2 == "SELL"
    assert conf2 == 0.70
    assert reason2 == "资金流出且破位"
    print("Test 2 Passed: Markdown JSON Block")

    # 3. 包含杂言的 JSON (首尾有杂字，但用 {} 范围可提取)
    raw3 = '首先，根据数据：\n{"action": "BUY", "confidence": 0.6, "reasoning": "底部缩量整理完成"}\n以上是决策结果。'
    action3, conf3, reason3 = _parse_decision_response(raw3)
    assert action3 == "BUY"
    assert conf3 == 0.6
    assert reason3 == "底部缩量整理完成"
    print("Test 3 Passed: Wrapped JSON with preamble/postamble")

    # 4. 纯文本自然语言分类 (JSON 损坏，使用关键词和前缀清除)
    raw4 = '<think>我们应当分析巨化股份...</think>\n推理推断: 巨化股份技术形态转强，建议买入，置信度约八成。'
    action4, conf4, reason4 = _parse_decision_response(raw4)
    assert action4 == "BUY"
    assert conf4 == 0.3
    # 应该被清除了 "推理推断" 前缀，且保留了核心内容
    assert "巨化股份技术形态转强" in reason4
    assert "推理推断" not in reason4
    print("Test 4 Passed: Natural language fallback and preamble removal")

    print("\nAll LLM parse tests passed successfully!")

if __name__ == "__main__":
    run_tests()
