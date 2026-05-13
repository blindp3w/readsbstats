#!/bin/bash
# Called by notify-telegram@.service with the failed unit name as $1
set -uo pipefail

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

curl -s -X POST \
    "https://api.telegram.org/bot${RSBS_TELEGRAM_TOKEN}/sendMessage" \
    -d "chat_id=${RSBS_TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TEXT}" \
    -d "parse_mode=HTML" \
    || true
