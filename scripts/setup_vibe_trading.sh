#!/usr/bin/env bash
# 安装 Vibe-Trading 研究层到独立 venv（主依赖树零新增）。
# 用法: bash scripts/setup_vibe_trading.sh
# 回滚: 删除 ~/.vibe-trading-venv 并设 VIBE_TRADING_ENABLED=0 即可，
#       AlphaPilot 主链路不依赖它（trader brief 会静默省略因子证据段）。
set -euo pipefail

VIBE_VERSION="0.1.15"
VIBE_HOME="${ALPHAPILOT_VIBE_HOME:-$HOME/.vibe-trading-venv}"

python_cmd="${PYTHON:-python3}"
command -v "$python_cmd" >/dev/null 2>&1 || python_cmd="python"
command -v "$python_cmd" >/dev/null 2>&1 || {
    echo "ERROR: 未找到 python3/python" >&2
    exit 1
}

"$python_cmd" - <<PY
import sys
if sys.version_info < (3, 11):
    print("ERROR: vibe-trading-ai 需要 Python >= 3.11，当前 %d.%d" % sys.version_info[:2])
    sys.exit(1)
PY

if [ ! -d "$VIBE_HOME" ]; then
    echo ">> 创建 venv: $VIBE_HOME"
    "$python_cmd" -m venv "$VIBE_HOME"
fi

PIP="$VIBE_HOME/bin/pip"
[ -x "$PIP" ] || PIP="$VIBE_HOME/Scripts/pip.exe"   # Windows
PYBIN="$VIBE_HOME/bin/python"
[ -x "$PYBIN" ] || PYBIN="$VIBE_HOME/Scripts/python.exe"

echo ">> 安装 vibe-trading-ai==$VIBE_VERSION（依赖较重，包含 langchain/langgraph，请耐心等待）"
"$PIP" install --upgrade pip >/dev/null
"$PIP" install "vibe-trading-ai==$VIBE_VERSION"

echo ">> 冒烟测试: 通过 MCP 契约调用 alpha_zoo"
if "$PYBIN" "$(dirname "$0")/vibe/vibe_tool_driver.py" alpha_zoo '{"action":"list_alphas","zoo":"gtja191","limit":3}'; then
    echo ">> 安装成功"
else
    echo "WARN: 冒烟测试失败（不影响安装）；请检查 Python 版本与网络后重试" >&2
fi

cat <<EOF

后续配置（AlphaPilot 侧）:
    VIBE_TRADING_ENABLED=1
    VIBE_PYTHON=$PYBIN        # 默认路径可省略

运行因子基准（需行情源可达；csi300 使用 Tushare，需 TUSHARE_TOKEN）:
    python scripts/vibe_alpha_screen.py --universe csi300 --zoo gtja191 --period 2024-2026
结果写入 data/vibe/alpha_bench_latest.json，盘后复盘自动引用。
EOF
