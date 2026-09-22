#!/usr/bin/env bash
# 安装 Laya 影子决策层到独立 venv 并启动本机服务（主依赖树零新增）。
# 用法: bash scripts/setup_laya.sh
# 回滚: systemctl --user disable --now alpha-pilot-laya.service && rm -rf ~/.laya-venv
#       主链路不依赖它（LAYA_ENABLED=0 时 trader 侧零调用）。
set -euo pipefail

LAYA_VERSION="0.3.5"
LAYA_HOME="${ALPHAPILOT_LAYA_HOME:-$HOME/.laya-venv}"
PORT="${LAYA_PORT:-8642}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

python_cmd="${PYTHON:-python3}"
command -v "$python_cmd" >/dev/null 2>&1 || python_cmd="python"
command -v "$python_cmd" >/dev/null 2>&1 || {
    echo "ERROR: 未找到 python3/python" >&2
    exit 1
}

"$python_cmd" - <<PY
import sys
if sys.version_info < (3, 10):
    print("ERROR: laya 需要 Python >= 3.10，当前 %d.%d" % sys.version_info[:2])
    sys.exit(1)
PY

if [ ! -d "$LAYA_HOME" ]; then
    echo ">> 创建 venv: $LAYA_HOME"
    "$python_cmd" -m venv "$LAYA_HOME"
fi

PIP="$LAYA_HOME/bin/pip"
PYBIN="$LAYA_HOME/bin/python"

echo ">> 安装 laya==$LAYA_VERSION（torch/transformers 较重，请耐心等待）"
"$PIP" install --upgrade pip >/dev/null
"$PIP" install "laya==$LAYA_VERSION"

echo ">> 冒烟测试: 加载 Router 并对一条中文状态做类型化决策"
"$PYBIN" - <<'PY'
import json, time
t0 = time.time()
from laya import Router
router = Router(preload=True)
print(f"LOAD {time.time() - t0:.1f}s")
state = {"code": "601123", "name": "恒瑞医药",
         "dimensions": {"technical": {"score": 70, "confidence": 0.8},
                        "sentiment": {"score": 65, "confidence": 0.6}}}
questions = {"action": {"type": "choice",
                        "instructions": "Trading decision from 0-100 dimension scores (60+ bullish).",
                        "criteria": {"buy": "bullish", "hold": "neutral", "sell": "bearish"}}}
t1 = time.time()
res = router.predict(state, questions)
print(f"CALL {(time.time() - t1) * 1000:.0f}ms")
print(json.dumps(res["answers"], ensure_ascii=False))
PY

if [ "${1:-}" = "--install-service" ] && command -v systemctl; then
    echo ">> 安装 systemd user 服务 alpha-pilot-laya.service (port=$PORT)"
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/alpha-pilot-laya.service" <<EOF
[Unit]
Description=Laya shadow decision service (local, 127.0.0.1:$PORT)
After=network-online.target

[Service]
WorkingDirectory=$PROJECT_DIR
Environment=HF_HUB_ENABLE_HF_TRANSFER=1
ExecStart=$PYBIN $PROJECT_DIR/scripts/laya/laya_server.py --port $PORT
Restart=on-failure
RestartSec=10
# 影子服务挂了交易主链路照常运行，只少一路影子对照
Nice=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now alpha-pilot-laya.service
    sleep 3
    if curl -sf --max-time 10 "http://127.0.0.1:$PORT/health" >/dev/null; then
        echo ">> 服务健康: http://127.0.0.1:$PORT/health"
    else
        echo "WARN: 服务已启动但健康检查未通过，查看 journalctl --user -u alpha-pilot-laya" >&2
    fi
fi

cat <<EOF

后续配置（AlphaPilot 侧，写入 ~/.hermes/.env 并带 export 前缀）:
    export LAYA_ENABLED=1
    # LAYA_BASE_URL 默认 http://127.0.0.1:$PORT，一般无需设置

跑影子对照（对某日 MiMo 已判断的候选）:
    python -m strategy.laya_client 2026-09-22
结果落盘 data/laya/shadow_<date>.json，并写一条 laya_shadow 自动事件。
EOF
