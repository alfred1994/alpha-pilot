# 事件驱动架构 - 快速开始

## 一键测试
```bash
python tests/test_realtime.py
```

## 启动命令
```bash
# 前台测试
python main.py --realtime

# 后台守护（Hermes/Ubuntu）
systemctl --user start quant-realtime
```

## 核心优势
- 止损延迟：60秒 → <1秒
- CPU占用：持续轮询 → 事件唤醒
- 支持扩展：新闻/指数/外盘监控

详见 docs/REALTIME.md
