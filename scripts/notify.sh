#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env 2>/dev/null || true
set +a

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env"
  exit 1
fi

MESSAGE="${1:-Claude Code klaar!}"

curl -s -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "text=${MESSAGE}" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  > /dev/null
