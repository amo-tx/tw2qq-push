#!/usr/bin/env bash
# External keepalive for the WorkBuddy cloud sandbox.
# Runs entirely on GitHub-hosted runners: no desktop client, no user PC.
# The free WorkBuddy app is an on-demand sandbox and is reclaimed when idle;
# one external HTTP request per minute keeps the process (and its poller) alive.
set -u

HEALTH_URL="${HEALTH_URL:-https://tw2qq-pusher.app.workbuddy.host/api/health?sharecode=Z3YR5C5DTB}"
CHECK_URL="${CHECK_URL:-https://tw2qq-pusher.app.workbuddy.host/api/check?sharecode=Z3YR5C5DTB}"

# GitHub Actions hard limit is 6h per job. Run 350 min and let the 6h cron relay.
end=$((SECONDS + 21000))
i=0
ok=0
miss=0

while [ "$SECONDS" -lt "$end" ]; do
  i=$((i + 1))
  code=$(curl -sS -o /tmp/h.json -w '%{http_code}' --max-time 30 --retry 2 --retry-delay 3 "$HEALTH_URL" || echo 000)
  if [ "$code" = "200" ]; then ok=$((ok + 1)); miss=0; else miss=$((miss + 1)); fi
  if [ $((i % 30)) -eq 1 ] || [ "$code" != "200" ]; then
    echo "$(date -u +%H:%M:%S)Z [$i] HTTP $code ok=$ok"
  fi
  if [ "$miss" -ge 3 ]; then
    # Nudge a real poll as well; /api/check respects manual_pause unless forced.
    curl -sS -o /dev/null --max-time 60 -X POST "$CHECK_URL" || true
    echo "3 consecutive misses, triggered /api/check"
    miss=0
  fi
  sleep 60
done

echo "done: $i probes, $ok succeeded"
[ "$ok" -gt 0 ] || exit 1
