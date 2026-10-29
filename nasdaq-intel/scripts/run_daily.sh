#!/usr/bin/env bash
# 每个美股交易日（收盘后）运行一次。
# crontab 示例（美东时间）：  "15 17 * * 1-5 cd /workspace/nasdaq-intel && ./scripts/run_daily.sh"
set -euo pipefail

cd "$(dirname "$0")/.."

# 需要 LLM 时在此注入（不要写入配置文件）：
# export OPENAI_API_KEY="sk-..."
# export LLM_BASE_URL="https://api.deepseek.com/v1"
# export LLM_MODEL="deepseek-chat"

python3 run.py run "$@"
