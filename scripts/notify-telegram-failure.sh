#!/bin/bash
# Called by notify-telegram@.service with the failed unit name as $1
set -uo pipefail

# Audit-12 #166 — fail loudly and quickly if Telegram envs are missing,
# instead of letting `set -u` blow up on the `${RSBS_TELEGRAM_TOKEN}`
# interpolation deeper in the script (which produces a confusing journal
# line and no actionable error). Use the `:?` parameter expansion so the
# operator sees exactly which env var is missing.
: "${RSBS_TELEGRAM_TOKEN:?RSBS_TELEGRAM_TOKEN not set — failure notification suppressed}"
: "${RSBS_TELEGRAM_CHAT_ID:?RSBS_TELEGRAM_CHAT_ID not set — failure notification suppressed}"

UNIT="${1:?unit name required}"
# systemctl status exits 3 for failed units — || true prevents set -e from
# killing the script before it can send the notification.
#
# parse_mode=HTML below means Telegram rejects the whole message with 400 on
# any unescaped `<`, `>`, or `&` in the status output — and you get no failure
# notification at exactly the moment you most need one (improvements.md #119).
# Order matters: escape `&` first, otherwise it eats the `&amp;` we produce.
STATUS=$(systemctl status "$UNIT" --no-pager -l 2>&1 \
    | head -30 \
    | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g') || true
TEXT="❌ <b>${UNIT}</b> failed on <b>$(hostname)</b>

<pre>${STATUS}</pre>"

# improvements.md #123 — keep the bot token out of curl's argv (and therefore
# out of /proc/<pid>/cmdline).  Write the URL line to a 0600 tmpfile and feed
# it via --config; the token never appears as a command-line argument.
CFG=$(mktemp)
chmod 600 "$CFG"
trap 'rm -f "$CFG"' EXIT
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$RSBS_TELEGRAM_TOKEN" > "$CFG"

curl -s -X POST --config "$CFG" \
    -d "chat_id=${RSBS_TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TEXT}" \
    -d "parse_mode=HTML" \
    || true
