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

- **状态只含特征**（6 维分数+置信度、股票名、日期），**绝不含 MiMo 的答案/
  置信度/理由**——否则"对照一致率"就是循环论证；
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

## 版本固定

当前固定 `laya==0.3.5`（`scripts/setup_laya.sh`）。该项目 4 天内发了 15 个
版本，升级前先跑 `tests/test_laya_client.py` 并人工核对 `router.predict`
的输出契约（本项目的解析按 0.3.5 实测输出对齐）。
