#!/usr/bin/env bash
# External keepalive for the WorkBuddy cloud sandbox.
#
# Runs ONLY on GitHub-hosted runners: no WorkBuddy desktop client, no user PC,
# no always-on device at home. The free WorkBuddy app is an on-demand sandbox
# and is reclaimed when idle; one external HTTP request per minute keeps the
# process (poller + official QQ bot + NapCat relay) alive.
#
# SELF-RELAY (the important part)
# GitHub's `schedule` events for public repos are frequently delayed or dropped
# (measured: a `*/10` cron actually fired every 4-8 hours, and a `0 */6` cron
# never fired at all). So this job does NOT rely on cron to be relaunched:
# every 90 minutes it triggers a NEW workflow run through the GitHub API. The
# shared concurrency group cancels this run and the new one takes over, so the
# 6-hour GitHub job limit is never reached. Schedule + push + manual dispatch
# remain as extra safety nets.
set -u

HEALTH_URL="${HEALTH_URL:-https://tw2qq-pusher.app.workbuddy.host/api/health?sharecode=Z3YR5C5DTB}"
CHECK_URL="${CHECK_URL:-https://tw2qq-pusher.app.workbuddy.host/api/check?sharecode=Z3YR5C5DTB}"
REPO="${GH_REPO:-amo-tx/tw2qq-push}"
WF_PATH=".github/workflows/keepalive.yml"
WF_ID_DEFAULT="408910876"

END=$((SECONDS + 21000))          # 350 min, below the 358 min job timeout
RELAY_EVERY=$((90 * 60))          # hand over every 90 minutes
NEXT_RELAY=$((SECONDS + RELAY_EVERY))

i=0
ok=0
miss=0
relays=0

# Token priority: PAT secret (user identity, guaranteed to create a new run)
# -> github.token injected by the workflow -> token left by actions/checkout.
TOKEN="${RELAY_PAT:-}"
if [ -z "$TOKEN" ] && [ -n "${GH_TOKEN:-}" ]; then TOKEN="$GH_TOKEN"; fi
if [ -z "$TOKEN" ] && [ -f .git/config ]; then
  TOKEN=$(sed -n 's/.*AUTHORIZATION: basic //p' .git/config | head -1 \
          | base64 -d 2>/dev/null | sed 's/^x-access-token://')
fi

workflow_id() {
  local id=""
  if command -v jq >/dev/null 2>&1 && [ -n "$TOKEN" ]; then
    id=$(curl -sS --max-time 20 -H "Authorization: Bearer $TOKEN" \
         "https://api.github.com/repos/$REPO/actions/workflows?per_page=100" \
         | jq -r --arg p "$WF_PATH" '.workflows[] | select(.path==$p) | .id' 2>/dev/null \
         | head -1)
  fi
  echo "${id:-$WF_ID_DEFAULT}"
}

relay() {
  local code wid
  wid=$(workflow_id)
  # 1) workflow_dispatch: a PAT always creates a new run (no recursion guard).
  code=$(curl -sS -o /tmp/relay.json -w '%{http_code}' --max-time 25 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    "https://api.github.com/repos/$REPO/actions/workflows/$wid/dispatches" \
    -d '{"ref":"main"}' 2>/dev/null || echo 000)
  if [ "$code" = "204" ]; then
    echo "$(date -u +%H:%M:%S)Z relay: workflow_dispatch ok (workflow $wid)"
    return 0
  fi
  echo "$(date -u +%H:%M:%S)Z relay: workflow_dispatch HTTP $code, trying repository_dispatch"
  # 2) repository_dispatch fallback (needs contents: write).
  code=$(curl -sS -o /tmp/relay.json -w '%{http_code}' --max-time 25 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    "https://api.github.com/repos/$REPO/dispatches" \
    -d '{"event_type":"relay"}' 2>/dev/null || echo 000)
  if [ "$code" = "204" ]; then
    echo "$(date -u +%H:%M:%S)Z relay: repository_dispatch ok"
    return 0
  fi
  echo "$(date -u +%H:%M:%S)Z relay: repository_dispatch HTTP $code, trying empty commit"
  # 3) last resort: an empty commit fires on.push, which GitHub always honours.
  if git -c user.name=tw2qq-bot -c user.email=bot@local commit --allow-empty \
        -m "keepalive relay $(date -u +%Y%m%d%H%M)" >/dev/null 2>&1 && \
     git push origin HEAD:main >/dev/null 2>&1; then
    echo "$(date -u +%H:%M:%S)Z relay: empty-commit push ok"
    return 0
  fi
  echo "$(date -u +%H:%M:%S)Z relay: all handover methods failed"
  return 1
}

echo "start $(date -u +%FT%TZ) token=${TOKEN:+present} end_in=350m relay_every=$((RELAY_EVERY / 60))m"

while [ "$SECONDS" -lt "$END" ]; do
  i=$((i + 1))
  code=$(curl -sS -o /tmp/h.json -w '%{http_code}' --max-time 30 --retry 2 \
        --retry-delay 3 "$HEALTH_URL" || echo 000)
  if [ "$code" = "200" ]; then ok=$((ok + 1)); miss=0; else miss=$((miss + 1)); fi
  if [ $((i % 30)) -eq 1 ] || [ "$code" != "200" ]; then
    echo "$(date -u +%H:%M:%S)Z [$i] HTTP $code ok=$ok relays=$relays"
  fi
  if [ "$miss" -ge 3 ]; then
    # Nudge a real poll as well; /api/check respects manual_pause unless forced.
    curl -sS -o /dev/null --max-time 60 -X POST "$CHECK_URL" || true
    echo "$(date -u +%H:%M:%S)Z 3 consecutive misses, triggered /api/check"
    miss=0
  fi

  # Hand over to a fresh run every 90 min; retry every 5 min near the deadline.
  if [ "$SECONDS" -ge "$NEXT_RELAY" ] || [ $((END - SECONDS)) -lt 900 ]; then
    if relay; then relays=$((relays + 1)); fi
    if [ $((END - SECONDS)) -lt 900 ]; then
      NEXT_RELAY=$((SECONDS + 300))
    else
      NEXT_RELAY=$((SECONDS + RELAY_EVERY))
    fi
  fi
  sleep 60
done

echo "done: $i probes, $ok succeeded, $relays handovers"
[ "$ok" -gt 0 ] || exit 1
