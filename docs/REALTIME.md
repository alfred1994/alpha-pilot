# 事件驱动架构部署指南

## 架构对比

| 项目 | 旧架构（定时轮询） | 新架构（事件驱动） |
|------|-------------------|-------------------|
| 风险感知 | 按巡检周期检查 | 推送或快照到达后检查，实际延迟取决于数据源 |
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
# 确保环境变量已设置（可选，见下方轮询回退）
export LONGPORT_APP_KEY="your_key"
export LONGPORT_APP_SECRET="your_secret"
export LONGPORT_ACCESS_TOKEN="your_token"

# 启动事件驱动交易员
python3 main.py --realtime
```

### 3. 无Longport凭证时的轮询回退

没有 `LONGPORT_*` 凭证或SDK不可用时，行情监控自动回退到
新浪公开快照轮询（`data/kt_realtime.py` KT适配器），无需任何凭证：

```bash
# 直接启动，无需Longport环境变量
python3 main.py --realtime

# 可选: 调整轮询间隔（默认3秒，新浪接口有频率限制，不要低于1秒）
export REALTIME_POLL_INTERVAL=5
```

回退模式说明：
- 推送变轮询，默认每3秒请求一次；不承诺成交延迟
- 事件结构与长桥推送同构（quote_update: code/price/volume/turnover），
  下游处理器无感知；事件额外携带 `source: sina_poll`
- 行情覆盖沪深京股票/ETF/可转债/指数，带 `data_valid` 时间戳校验

实时模块是只读风险传感器：有效行情触发纯判断并广播建议，不调用账户卖出。
模拟成交仍由自动循环或显式执行入口负责，因此不要因启用 `--realtime` 停用定时止损巡检。
长桥初始化成功后先注册回调，再订阅已登记的持仓；缺少凭证或初始化失败才回退到快照轮询。
行情标的、正有限价格和源时间戳共用校验，默认最大年龄300秒；过期、未来时间或缺少时间戳的行情不进入传感与自动执行链。

### 4. 生产部署（systemd守护）

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
长桥WebSocket订阅持仓股票实时行情；凭证缺失时自动回退新浪快照轮询

### 3. `realtime/event_handlers.py` - 事件处理器
- `StopLossHandler` - 只读止损评估与建议通知
- `MarketEventHandler` - 市场异动触发扫描

### 4. `realtime/event_driven_trader.py` - 主循环
协调各模块，管理生命周期

## 事件流

```
[长桥WebSocket] → [quote_update事件] → [EventBus] 
[新浪快照轮询] ↗       (无凭证时自动回退)   ↓
                              [StopLossHandler检查持仓]
                                          ↓
                          风险建议 → [通知]

[定时止损巡检 / TradePlan执行] → [校验新鲜价格] → [模拟账户卖出]
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
