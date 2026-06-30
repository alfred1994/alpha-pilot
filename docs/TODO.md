# TODO 待办事项

## 📅 2026-06-12 待完成

### 1. Telegram Bot 控制面板 🚧 [进行中]
**需求**：通过 Telegram Bot 自定义面板控制量化系统

**功能设计**：
- [ ] 自定义键盘（快捷操作按钮）
  - 启动/停止自动盯盘
  - 立即选股扫描
  - 查看持仓/收益
  - 手动买入/卖出
  - 查看Top候选
  
- [ ] Inline 按钮（动态交互面板）
  - 候选股票列表（点击查看详情）
  - 持仓操作（止损/止盈/平仓）
  - 策略配置切换（低位模式/混合模式）
  
- [ ] 命令系统
  - `/start` - 显示主菜单
  - `/status` - 系统状态（运行中/已暂停/账户概览）
  - `/positions` - 当前持仓详情
  - `/scan` - 立即扫描选股
  - `/buy <code>` - 手动买入
  - `/sell <code>` - 手动卖出
  - `/config` - 查看/修改配置
  - `/logs` - 最近日志

- [ ] 状态查询
  - 账户余额、持仓、收益率
  - 今日选股信号Top10
  - 最新交易记录
  - 风控状态（止损触发、熔断状态）

**技术方案**：
- 使用 `python-telegram-bot` 库
- 创建 `scheduler/telegram_bot.py` 模块
- 集成到 `main.py` 的自动盯盘循环
- 通过 Bot 回调函数触发系统功能

**相关文件**：
- `scheduler/notifier.py` - 现有通知模块（单向推送）
- `config.py` - 配置参数（需添加Bot控制开关）
- `main.py` - 主入口（需集成Bot循环）

**环境变量**：
```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_BOT_ENABLED=1             # 新增：是否启用Bot控制
```

---

## 📋 重构主线进度

根据 `CLAUDE.md` 的7阶段重构计划：

- [x] **Phase 1**: SQLite存储层 + 长桥数据源集成 ✅ (已完成)
  - 提交: `2bba501 feat: 事件驱动架构 + Phase1数据源集成`

- [ ] **Phase 2**: 市场环境识别器 🔜 (下一步)
  - 创建 `strategy/market_regime.py`
  - 识别：牛市/熊市/震荡/极端行情
  - 输出：环境标签 + 置信度

- [ ] **Phase 3**: LLM决策引擎
  - 创建 `strategy/llm_trader.py`
  - 替换现有加权打分逻辑
  - 集成 MiMo LLM API

- [ ] **Phase 4**: 交易记忆系统
  - 创建 `strategy/memory.py`
  - 存储历史决策 + 结果
  - 反馈到LLM上下文

- [ ] **Phase 5**: 策略配置中心
  - 创建 `strategy/regime_config.py`
  - 根据市场环境自动调整参数

- [ ] **Phase 6**: 执行层+风控层改造
  - 适配LLM决策输出
  - 增强风控规则

- [ ] **Phase 7**: 回测引擎改造
  - 支持LLM决策回测
  - 性能指标统计

---

## 🎯 选股优化进度

- [x] **低位选股器** ✅ (已完成)
  - 提交: `e28e5ed feat: 新增低位潜力股选股器`
  - 文件: `strategy/low_position_picker.py`
  - 文档: `docs/选股优化说明.md`
  
- [ ] **测试验证** (待完成)
  - [ ] 运行 `tests/test_low_position.py` 验证数据源
  - [ ] 回测低位模式 vs 混合模式收益对比
  - [ ] 调整评分权重（可能需要优化）

- [ ] **后续优化方向** (已规划)
  - [ ] 增加基本面筛选（PE/PB/ROE）
  - [ ] 技术形态识别（MACD底背离、RSI超卖）
  - [ ] 行业轮动分析
  - [ ] 解禁预警集成

---

## 🐛 已知问题

1. **API稳定性**
   - 东方财富部分接口偶尔超时
   - 需增加重试机制和缓存

2. **数据质量**
   - 涨停板/龙虎榜数据可能缺失
   - 需要多数据源验证

3. **性能优化**
   - 选股扫描速度慢（扫描100只需30-60秒）
   - 考虑并发请求 + 数据预加载

---

## 📝 备注

- 优先级: **Telegram Bot** > Phase 2市场环境识别 > 测试验证
- Hermes Agent 通过 Telegram 交互，Bot 面板是刚需
- 重构主线可以并行推进，互不阻塞
