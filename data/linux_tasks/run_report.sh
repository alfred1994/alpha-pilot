#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR='/home/ubuntu/projects/quant-pilot'
PYTHON_CMD='python3'
LOG_FILE='/home/ubuntu/projects/quant-pilot/logs/ai_report.log'
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
fi

if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

export BROKER_MODE=paper
export PYTHONUNBUFFERED=1

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp START --ai-report --report-days 5 =====" >> "$LOG_FILE"
$PYTHON_CMD main.py --ai-report --report-days 5 >> "$LOG_FILE" 2>&1
exit_code=$?
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp END exit=$exit_code =====" >> "$LOG_FILE"
exit $exit_code
