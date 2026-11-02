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

# 交易日守卫：cron 按周一至周五触发，美股节假日自动跳过。
# 本地调试可设 NASDAQ_INTEL_SKIP_CALENDAR=1 强制运行。
if [ "${NASDAQ_INTEL_SKIP_CALENDAR:-0}" != "1" ]; then
  if ! "$PYTHON" - <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from nasdaq_intel.market_calendar import is_trading_day

today = datetime.now(ZoneInfo("America/New_York")).date()
sys.exit(0 if is_trading_day(today) else 1)
PY
  then
    mkdir -p data
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') 非美股交易日（美东），跳过本次运行" >> data/cron.log
    exit 0
  fi
fi

"$PYTHON" run.py run "$@"
