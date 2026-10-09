# Laya 影子决策层

AlphaPilot 可以把 [Laya](https://github.com/NandhaKishorM/laya)（Apache-2.0，
非自回归 System 1 类型化决策模型）作为**本地影子决策层**接入，与 MiMo 的既有
判断做并行对照。它不拦截、不改变任何交易决策，默认关闭。

## 它解决什么问题

- **速度**：MiMo 逐只判断 90s/只（经常触顶降级）；Laya 单次前向传播对一条
  中文候选状态做 2 个类型化决策，服务器上（ARM CPU，无 GPU）实测 **~1.1s**，
  100 只候选全量影子打分约 2 分钟。
- **零幻觉结构化输出**：不生成文本，只输出有界分类（带校准概率）和有序评分，
  无需解析自由文本。
- **本地部署**：权重在 HuggingFace，模型在服务器本地推理，无 API key、
  无 token 成本、数据不出服务器。

## 架构：独立 venv + 本机常驻服务

```
AlphaPilot 主进程              laya venv（独立安装，~/.laya-venv）
┌────────────────────┐   HTTP 127.0.0.1:8642   ┌──────────────────────────┐
│ strategy/          │ ─────────────────────>  │ scripts/laya/            │
│   laya_client.py   │  POST /predict          │   laya_server.py         │
└────────────────────┘  GET  /health           │   └─ laya Router(常热)   │
        │ 只读/影子文件                         └──────────────────────────┘
        v
data/laya/shadow_<date>.json + auto_event(laya_shadow) → 人工/复盘检查
```

- `laya` 及其重依赖（torch/transformers，venv 约 5.4GB）只存在于独立 venv，
  主依赖树零新增；
- 服务只监听 127.0.0.1，无鉴权，禁止暴露公网；
- 任何失败（未启用/服务未起/超时/输出异常）都表现为静默降级，交易主链路无感；
- 模型加载约 3 分钟（一次性，权重缓存在 `~/.cache/huggingface`）。

## 影子对照怎么读

`python -m strategy.laya_client <YYYY-MM-DD>` 对该日 **MiMo 已判断过**的候选
跑 Laya 对照：

- **状态只含特征**（6 维分数+置信度、股票名、日期、固定中文市场标签），
  **绝不含 MiMo 的答案/置信度/理由**——否则"对照一致率"就是循环论证；
- **股票名优先从 `llm_prompt` 提取**（与 web 路由同一规则），研究池仅兜底
  且跳过 name==code 的占位条目；
- **固定中文市场标签的原因**：Laya Router 按脚本路由 checkpoint——纯数字
  代码状态（无汉字）会被路由到 english checkpoint，首轮 40 只候选全部误
  路由并呈现系统性 sell 偏置（0/40 一致）；标签保证确定性路由到
  multilingual checkpoint，真实中文名同时是模型可用特征（如 ST 前缀）；
- 每只明细记录 `routing`（model/repo/reason），便于复核 checkpoint 选择；
- Laya 回答两个 typed 问题：
  - `action`：choice，`buy/hold/sell` 三分类，带校准概率；
  - `conviction`：score，信号强度 0-2 连续评分，带级别措辞；
- 结果落盘 `data/laya/shadow_<date>.json`（含逐只明细与混淆矩阵），并写一条
  `laya_shadow` 自动事件。

**一致率不是验收指标本身**。两周后看：
1. `buy->buy`、`sell->sell` 的对角线占比（Laya 与 MiMo 同向的比例）；
2. 结合 `candidate_outcomes` 的 T+5 结果，看**分歧样本**里谁对得多；
3. 达标（例如对角线 >90% 且分歧样本不劣于 MiMo）才讨论把 Laya 提为扫描
   预筛门卫，否则只保留在影子位或用于其他分类位。

## 安装与启用

```bash
bash scripts/setup_laya.sh --install-service   # 装 venv + 冒烟 + systemd user 服务
```

`~/.hermes/.env` 追加（**必须带 export 前缀**）：

```bash
export LAYA_ENABLED=1
# export LAYA_BASE_URL=http://127.0.0.1:8642   # 默认值，一般不用设
```

验证：

```bash
curl -s http://127.0.0.1:8642/health
python -m strategy.laya_client 2026-09-22
```

## 回滚

```bash
systemctl --user disable --now alpha-pilot-laya.service   # 停影子服务
# 或直接把 LAYA_ENABLED 设回 0（主链路零调用）
rm -rf ~/.laya-venv                                      # 彻底移除（可选）
```

## 文本情绪分类（分布内任务）

6 维特征对照证明 laya 读不懂数值特征向量后，把它用到分布内的文本任务上：
`strategy/laya_text.py` 对财经文本（市场头条+微博热帖+个股新闻，全部走
既有数据链路：news_aggregator/同花顺/新浪/东财搜索，无新数据源）做
positive/neutral/negative 三分类。

- **为什么可行**：官方 presets 就是文本分类（工单分诊/邮件分类）；中文
  金融文本服务器实测 6/8——强利好 pos 0.996、强利空 neg 0.997、例行公告
  neutral 0.902；两个 miss 为领域细微差（减持→neutral）与关键词带偏
  （"增速符合预期"→positive），属于已知边界；
- **用法**：盘后 `python -m strategy.laya_text <YYYY-MM-DD>` 对该日候选
  打分，落盘 `data/laya/text_sentiment_<date>.json`（并刷新
  `text_sentiment_latest.json`）+ `laya_text_sentiment` 事件；
- **消费方**：trader brief 的 `text_sentiment` 字段 → 复盘 prompt 追加一行
  证据（"头条/热帖 N利好/N中性/N利空；个股新闻: 代码(利/中/空)"），与
  vibe_factors 同模式，只读交叉参考，不进决策分；
- **定位**：sentiment 维度的**快速本地旁证**（~1s/条 vs MiMo 90s/批），
  不替代 MiMo 的舆情分析；两者长期分歧样本的对比数据可决定是否用它做
  预筛或加权。

## 格式实验记录（2026-09-23，服务器实测）

首轮影子对照 0/40 一致且全 sell，逐层排查做了 7 组受控实验（同一 laya
服务、确定性输出、重复请求结果一致）：

1. **路由**：研究池 name 多为纯数字代码 → 40 只全部被 Router 路由到
   english checkpoint。A/B 实测 multilingual 方向正确（buy）、english
   偏 sell。**已修复**（中文市场标签+提示词提名，现 40/40 multilingual）。
2. **键序**：同一 002916 特征状态，market 键在前 → sell 0.447，code 键在
   前 → buy 0.482。322M 小模型对 JSON 键序敏感。**已修复**（身份→上下文
   →特征的自然键序）。
3. **状态格式**：英文句子状态被路由回 english（汉字占比低）；中文句子+
   英文问题 → 全 sell；中文句子+中文问题 → 全 hold（连构造的强看空状态
   也答 hold）；维度口语化（"偏多/偏空/中性"）→ 概率近均匀、构造看空
   仍答 buy。

**结论**：laya 0.3.5 multilingual checkpoint 的分布内任务是客服工单分诊
与邮件分类（官方 presets 引用 `message`/`body` 等自然语言字段），对
"6 维数值特征向量 → buy/hold/sell"这一分布外任务**不能稳健读取特征**，
输出主要由表面线索（语言、键序、问题语言）驱动。影子位继续保留（成本
每天约 3 分钟 CPU），但**不应期待它当前形态下产生有判别力的对照数据**；
提为预筛门卫的计划在拿到新证据（更好的 checkpoint/格式或分布内任务）
之前搁置。

## 版本固定

当前安装版本固定为 `laya==0.4.1`（`scripts/setup_laya.sh`）。升级前先跑
`tests/test_laya_client.py` 并核对 `router.predict` 的输出契约。
本项目保留0.3.5解析契约的兼容测试；`/health`返回`laya_version`便于核验实际加载版本。

## 2026-10-09 更新核查与金融任务探测

GitHub/PyPI 最新版本为 [v0.4.1](https://github.com/NandhaKishorM/laya/releases/tag/v0.4.1)，
发布于 2026-10-08 17:29 UTC（北京时间10月9日01:29）。本次升级前服务为0.3.5。
0.4.1包括choice选项键排序以减少选项插入顺序影响、自定义checkpoint注册、批量结构化
决策指定checkpoint及训练更新次数诊断。选项排序修复不代表金融泛化或状态字段顺序问题
已经解决；运行库版本升级也不等于金融模型训练完成。

使用现有multilingual服务做了12条手工构造的中文金融事件三分类探测，6条符合预设标签。
场景包含订单、业绩、减持、澄清、传闻和监管；部分错例有较高置信度，说明未经领域校准
的概率不能直接当金融任务正确率。这是狭窄的能力探测，非独立基准、收益回测或新版本对比。
这组探测使用升级前的0.3.5环境，未训练权重、未赋予模型交易权限。

### 更深入参与的路线

1. **事件证据头**：识别标的相关性、事件类型、已发生/计划/传闻/否定，保存原文与可见时间。
   分别训练金融领域任务，避免“利好关键词=支持买入”。低置信或否定语义交给主模型。
2. **机会匹配头**：输入当时可用的行情摘要、市场状态与事件，输出趋势/波段/反弹/无充分证据。
   数值由代码计算并附定义，模型不承担算术和硬规则判定。先旁路观察，不阻断任何候选。
3. **候选排序与二次复核头**：比较“分析优先/证据冲突/等待触发”，为主模型调度有限分析预算。
   稳定后才在模拟盘中验证排序或软评分作用，不直接批准下单。
4. **领域微调和校准**：以当时可见状态建样本，事件事实使用核验标签；交易价值使用多个持有期
   的扣费、可成交结果和不利波动，不以模仿MiMo/DeepSeek或未来最高价为目标。
   时间切分、重叠窗口隔离、同事件去重，单独留出校准集与测试集。
5. **版本对照**：隔离安装0.4.1，固定checkpoint修订，先测接口、否定句、选项/字段顺序，
   再比较0.3.5、0.4.1、领域微调版和简单规则。记录精度、风险事件召回、拒答覆盖、校准、
   CPU延迟、主模型调用节省与模拟盘净收益；先验证再考虑接替当前服务。

与主交易员、独立复盘角色的整体分工见 [机会发现与动态风格](opportunity-driven-trading.md)。
