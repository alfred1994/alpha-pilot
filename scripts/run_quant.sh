#!/bin/bash
# A股量化系统 - 定时任务包装脚本
# 用法: ./run_quant.sh [scan|execute|review|full]

cd /home/ubuntu/projects/a-stock-quant

# 加载环境变量
set -a
source /home/ubuntu/.hermes/.env 2>/dev/null
set +a

# 运行指定命令
CMD=${1:-full}
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 执行 $CMD ==="
python3 main.py --$CMD 2>&1
echo "=== 完成 ==="
