# Hermes Agent (AI 驾驶员) 自动驾驶与故障自愈部署手册

本手册定义了 **AI 驾驶员 (Hermes Agent)** 如何对 **交易车 (Quant Pilot)** 进行全自动托管、故障诊断、热补丁修复和自动重启的配置规范。

---

## 1. 基础配置：驾驶员与车的绑定

请确保在你的服务器上，将 `quant-pilot` 源码路径和相关系统命令映射给 Hermes Agent。

*   **项目根路径**: `/path/to/quant-pilot`
*   **状态监测文件**: `/path/to/quant-pilot/data/latest_crash.json`
*   **控制命令**:
    *   体检状态: `python3 main.py --agent-status`
    *   崩溃现场: `python3 main.py --crash-info`
    *   服务控制: `systemctl --user status/restart/stop quant-pilot-auto`

---

## 2. 自动驾驶指令集 (Hermes Skill 伪代码)

你可以直接将以下规则注入 Hermes Agent 的 Action System 中：

### 指令 1: `/pilot status` (状态查询)
*   **动作**：执行 `python3 main.py --agent-status`。
*   **响应**：
    *   如果 `health.ok` 且 `watchdog.ok` 为 `true`，向 Tg 报告：“🟢 量化系统状态健康。当前持仓：{positions}，总资产：{total_assets}。”
    *   如果有任何 `failed_required` 或 `criticals`，向 Tg 报告：“🔴 量化系统出现异常！健康阻断：{failed_required}，Watchdog异常：{criticals}。”

### 指令 2: `/pilot pause` / `/pilot resume` (驾驶状态调节)
*   **动作**：执行 `python3 main.py --auto-pause --reason "{reason}"` 或 `python3 main.py --auto-resume`。
*   **响应**：向 Tg 报告：“⏸ 已成功暂停自动盯盘动作，原因：{reason}” 或 “▶ 已恢复自动驾驶，系统开始盘中扫描。”

### 指令 3: `/pilot crash` (崩溃查询)
*   **动作**：执行 `python3 main.py --crash-info`。
*   **响应**：输出最近崩溃的 Error Type, File, Line 以及 Traceback 代码片段。

---

## 3. 核心：自愈驾驶流程 (Self-Healing Driver Rule)

当交易系统运行时，Hermes Agent 应注册一个**文件监听器 (File Watcher)**，监听 `/path/to/quant-pilot/data/latest_crash.json` 的变动。

一旦该文件被写入，代表车辆发生了致命崩溃。Hermes Agent 自动执行以下 **自愈闭环工作流**：

### 流程步骤

```
1. 监听触发 ────> 2. 获取崩溃诊断 ────> 3. LLM生成补丁
                        │                     │
                        ▼                     ▼
5. 部署热更 <──── 4. 跑单元测试(可选) <─── 运行代码修复
```

#### Step 1. 获取崩溃现场
Hermes Agent 读取本地崩溃信息并输出：
```bash
python3 main.py --crash-info
```
提取出报错文件 `file`，行号 `line`，以及异常栈 `traceback`。

#### Step 2. 源码定位与分析
Hermes Agent 读取出错文件附近的代码块：
*   **指令**：读取 `file` 发生报错的 `line` 上下文前后 30 行代码。
*   **LLM 推理上下文**：
    *   *“这是 Quant Pilot 运行时的报错：{traceback}。”*
    *   *“出错的代码位置在：{file} 第 {line} 行。”*
    *   *“请阅读该文件上下文，分析报错成因（如拼写错误、类型未匹配、外部API返回格式变动），并输出修复后的代码补丁。”*

#### Step 3. 自动编辑源码与热更修复
*   Hermes Agent 使用本地代码编辑能力，将修复代码精准替换回 `{file}` 中。

#### Step 4. 车辆自检门禁 (测试验证)
为了保证修复后的赛车能够安全上路，驾驶员需运行以下命令进行快速验证：
1.  **运行单元测试**：针对修复的文件执行测试。如果是 `low_position_picker.py` 报错，则跑 `python3 tests/test_low_position.py`。
2.  **就绪状态自检**：
    ```bash
    python3 main.py --health
    ```
    确保没有必需条件报错。

#### Step 5. 复位崩溃状态与重启服务
若验证通过，Hermes Agent 执行以下命令完成崩溃解除和服务的热重启：
```bash
# 1. 标记崩溃已修复
python3 main.py --resolve-crash

# 2. 重启自动盯盘循环
systemctl --user restart quant-pilot-auto.service
```

#### Step 6. 成果推送
通过 Telegram 向用户推送喜报：
> 🤖 **AI 驾驶员自动修复报告**
>
> 报告主人，今天系统在运行中由于出现以下异常导致崩溃：
> `NameError: name 'api_client' is not defined` (在 `data/eastmoney.py` 第 42 行)
>
> 我已定位到源码，并成功应用了热修复补丁。服务已重新拉起并继续执行自动驾驶。
>
> 🛠 **代码修改 diff:**
> ```diff
> -        data = api_client.get_history()
> +        from data.htsc_client import get_htsc_client
> +        client = get_htsc_client()
> +        data = client.get_history()
> ```
