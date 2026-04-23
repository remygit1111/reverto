#!/bin/bash
# notify.sh — Telegram notification helper used by `make beep`.
#
# Two bot-lanes supported:
#   * CLAUDE lane (TELEGRAM_CLAUDE_BOT_TOKEN + TELEGRAM_CLAUDE_CHAT_ID)
#     — if set, Claude Code status pings go to a dedicated channel so
#     they don't clutter the trade-notification chat.
#   * STANDARD lane (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) — the
#     portal's trade-alert lane. Used as fallback when the Claude lane
#     isn't configured, which matches the pre-split behaviour for
#     operators who haven't adopted the second bot yet.
#
# Refuses to send (exit 1) when neither lane is fully configured so
# the failure is loud — silent no-op would hide a forgotten .env.

set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env 2>/dev/null || true
set +a

# Prefer the Claude-specific lane if both its vars are present AND
# non-empty. A half-configured Claude lane (only the token set, for
# example) falls through to the standard lane so the operator
# doesn't silently lose notifications after a typo.
if [ -n "${TELEGRAM_CLAUDE_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CLAUDE_CHAT_ID:-}" ]; then
  BOT_TOKEN="$TELEGRAM_CLAUDE_BOT_TOKEN"
  CHAT_ID="$TELEGRAM_CLAUDE_CHAT_ID"
  LANE="claude"
elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
  CHAT_ID="$TELEGRAM_CHAT_ID"
  LANE="standard"
else
  echo "Neither TELEGRAM_CLAUDE_BOT_TOKEN/CHAT_ID nor TELEGRAM_BOT_TOKEN/CHAT_ID set in .env" >&2
  exit 1
fi

MESSAGE="${1:-Claude Code klaar!}"

curl -s -X POST \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "text=${MESSAGE}" \
  -d "chat_id=${CHAT_ID}" \
  > /dev/null
