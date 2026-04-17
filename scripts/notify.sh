#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

eval "$(grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' .env)"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env"
  exit 1
fi

curl -s -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  -d "text=${1:-Claude Code klaar!}" \
  > /dev/null
