#!/bin/bash
# Quant Pilot 启动脚本
# 自动加载 Hermes 环境变量

# 加载 Hermes .env 文件（忽略注释和空行）
if [ -f "$HOME/.hermes/.env" ]; then
    while IFS= read -r line; do
        # 跳过注释和空行
        [[ "$line" =~ ^#.*$ ]] && continue
        [[ -z "$line" ]] && continue
        # 导出环境变量
        export "$line"
    done < "$HOME/.hermes/.env"
    echo "✅ 已加载 Hermes 环境变量"
fi

# 进入项目目录
cd "$(dirname "$0")"

# 运行指定的命令
if [ $# -eq 0 ]; then
    echo "用法: ./run.sh <命令>"
    echo "示例:"
    echo "  ./run.sh --health      # 健康检查"
    echo "  ./run.sh --scan        # 扫描信号"
    echo "  ./run.sh --execute     # 执行交易"
    echo "  ./run.sh --review      # 每日复盘"
    echo "  ./run.sh --auto        # 自动盯盘"
    exit 1
fi

python3 main.py "$@"