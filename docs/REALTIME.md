# 事件驱动架构部署指南

## 架构对比

| 项目 | 旧架构（定时轮询） | 新架构（事件驱动） |
|------|-------------------|-------------------|
| 止损延迟 | 60秒 | <1秒 |
| CPU占用 | 持续轮询 | 事件唤醒 |
| 扩展性 | 单线程阻塞 | 异步并发 |
| 外部事件 | 无法响应 | 实时接入 |

## 快速开始

### 1. 安装依赖
```bash
pip install longport  # 长桥OpenAPI（实时行情）
```

### 2. 测试运行
```bash
# 确保环境变量已设置
export LONGPORT_APP_KEY="your_key"
export LONGPORT_APP_SECRET="your_secret"
export LONGPORT_ACCESS_TOKEN="your_token"

# 启动事件驱动交易员
python3 main.py --realtime
```

### 3. 生产部署（systemd守护）

#### Hermes/Ubuntu环境
```bash
# 复制systemd配置
cp scripts/quant-realtime.service ~/.config/systemd/user/

# 启用服务
systemctl --user enable quant-realtime
systemctl --user start quant-realtime

# 查看状态
systemctl --user status quant-realtime

# 查看日志
tail -f ~/.hermes/logs/quant-realtime.log
```

#### 停止服务
```bash
systemctl --user stop quant-realtime
```

## 核心模块

### 1. `realtime/event_bus.py` - 事件总线
异步发布订阅，解耦事件生产者和消费者

### 2. `realtime/quote_monitor.py` - 行情监控
长桥WebSocket订阅持仓股票实时行情

### 3. `realtime/event_handlers.py` - 事件处理器
- `StopLossHandler` - 实时止损
- `MarketEventHandler` - 市场异动触发扫描

### 4. `realtime/event_driven_trader.py` - 主循环
协调各模块，管理生命周期

## 事件流

```
[长桥WebSocket] → [quote_update事件] → [EventBus] 
                                          ↓
                              [StopLossHandler检查持仓]
                                          ↓
                          触发止损 → [PaperAccount.sell()]
```

## 扩展点

### 添加新事件类型
```python
# 1. 在 event_handlers.py 定义处理器
class NewsHandler:
    async def on_news_flash(self, event: Event):
        # 处理财经快讯
        pass

# 2. 在 event_driven_trader.py 注册
news_handler = NewsHandler()
event_bus.subscribe("news_flash", news_handler.on_news_flash)

# 3. 发布事件
event_bus.publish_sync(Event(
    type="news_flash",
    data={"title": "央行降息", "sentiment": "利好"}
))
```

## 向后兼容

旧的定时任务（cron/--auto）依然可用：
- `python3 main.py --scan` - 扫描
- `python3 main.py --execute` - 执行
- `python3 main.py --review` - 复盘

新架构只是增加了实时能力，不影响原有流程。

## 监控告警

```bash
# 检查服务健康
systemctl --user is-active quant-realtime

# 检查日志错误
grep ERROR ~/.hermes/logs/quant-realtime.log | tail -20

# 检查事件处理延迟
grep "事件处理" ~/.hermes/logs/quant-realtime.log | tail
```

## 故障恢复

- systemd配置了`Restart=always`，进程崩溃自动重启
- 长桥连接断开会自动重连（SDK内置）
- 事件队列满时丢弃新事件（防止内存溢出）

## 常见问题

**Q: 为什么还需要cron任务？**  
A: 复盘、报告等低频任务仍用cron，实时交易用事件驱动

**Q: 如何优雅退出？**  
A: Ctrl+C或`systemctl --user stop`，程序会完成当前事件处理后退出

**Q: 多实例冲突？**  
A: 模拟账户无并发保护，不要同时运行多个实例
