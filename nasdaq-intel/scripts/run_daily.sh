#!/usr/bin/env bash
# 每个美股交易日（收盘后）运行一次。
# crontab 示例（美东时间）：  "15 17 * * 1-5 cd /workspace/nasdaq-intel && ./scripts/run_daily.sh"
set -euo pipefail

cd "$(dirname "$0")/.."

# LLM 密钥放在项目根目录 .env（已加入 .gitignore 语义，勿写入 settings.yaml）：
# OPENAI_API_KEY="sk-..."
# LLM_BASE_URL="https://api.deepseek.com/v1"
# LLM_MODEL="deepseek-chat"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -n "${PYTHON:-}" ]; then
  :
elif [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

"$PYTHON" run.py run "$@"
