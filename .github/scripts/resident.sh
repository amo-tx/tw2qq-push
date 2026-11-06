#!/usr/bin/env bash
# tw2qq resident runner inside GitHub Actions.
#
# Public repo => unlimited free Actions minutes. This script runs the FULL
# tw2qq container (panel + X poller + official bot + NapCat relay + QQ) as an
# always-on service, carrying state between runs via actions/cache:
#   .resident/data    = seen.db + runtime relay config (dedup/subscriptions)
#   .resident/persist = HOME + NapCat/QQ install + QQ login ticket
#
# SELF-RELAY: same pattern as keepalive.sh — every RESIDENT_EVERY seconds it
# dispatches a new run of resident.yml; the shared concurrency group cancels
# this run and the fresh one takes over, so the 6h job limit is never reached.
#
# QQ SCAN: before the first login, NapCat writes cache/qrcode.png inside the
# persist volume; this script pushes it to the repo path state/qrcode.png
# (PAT has repo scope) so the user can scan it from the browser.
set -u

RESIDENT_DIR="${RESIDENT_DIR:-$PWD/.resident}"
DATA_DIR="$RESIDENT_DIR/data"
PERSIST_DIR="$RESIDENT_DIR/persist"
mkdir -p "$DATA_DIR" "$PERSIST_DIR/home" "$PERSIST_DIR/Napcat"

IMAGE="${TW2QQ_IMAGE:-ghcr.io/amo-tx/tw2qq:latest}"
REPO="${GH_REPO:-amo-tx/tw2qq-push}"
BRANCH="${GH_REF:-main}"
WF_PATH=".github/workflows/resident.yml"
WF_ID_DEFAULT=""

END=$((SECONDS + 21000))              # 350 min, below job timeout
RESIDENT_EVERY=$((4 * 3600 + 30 * 60)) # hand over every 4h30m
NEXT_RELAY=$((SECONDS + RESIDENT_EVERY))
HEALTH_URL="http://127.0.0.1:8080/api/health"
QR_REMOTE="state/qrcode.png"
QR_LOCAL="$PERSIST_DIR/Napcat/cache/qrcode.png"

ok=0; miss=0; restarts=0; qr_pushes=0

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
  if [ -z "$wid" ]; then
    echo "$(date -u +%H:%M:%S)Z relay: no workflow id, skip"; return 1
  fi
  code=$(curl -sS -o /tmp/relay.json -w '%{http_code}' --max-time 25 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    "https://api.github.com/repos/$REPO/actions/workflows/$wid/dispatches" \
    -d '{"ref":"main"}' 2>/dev/null || echo 000)
  if [ "$code" = "204" ]; then
    echo "$(date -u +%H:%M:%S)Z relay: workflow_dispatch ok (workflow $wid)"; return 0
  fi
  echo "$(date -u +%H:%M:%S)Z relay: workflow_dispatch HTTP $code, trying repository_dispatch"
  code=$(curl -sS -o /tmp/relay.json -w '%{http_code}' --max-time 25 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/dispatches" \
    -d '{"event_type":"resident"}' 2>/dev/null || echo 000)
  [ "$code" = "204" ] && { echo "$(date -u +%H:%M:%S)Z relay: repository_dispatch ok"; return 0; }
  echo "$(date -u +%H:%M:%S)Z relay: all handover methods failed (HTTP $code)"; return 1
}

relay_online() {
  # /api/health -> relay.online is true once NapCat OneBot link is up (= logged in).
  curl -sS --max-time 10 "$HEALTH_URL" 2>/dev/null \
    | jq -r '.relay.online // .relay_status.online // empty' 2>/dev/null
}

push_qrcode() {
  [ -f "$QR_LOCAL" ] || return 0
  local b64 sha api
  b64=$(base64 -w0 "$QR_LOCAL")
  sha=$(curl -sS --max-time 20 -H "Authorization: Bearer $TOKEN" \
        "https://api.github.com/repos/$REPO/contents/$QR_REMOTE?ref=$BRANCH" \
        | jq -r '.sha // empty' 2>/dev/null)
  api=$(printf '{"message":"tw2qq qrcode %s","content":"%s","branch":"%s"%s}' \
        "$(date -u +%H%M)" "$b64" "$BRANCH" \
        "$([ -n "$sha" ] && printf ',"sha":"%s"' "$sha")")
  code=$(curl -sS -o /tmp/qr.json -w '%{http_code}' --max-time 30 -X PUT \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/contents/$QR_REMOTE" -d "$api" 2>/dev/null || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "201" ]; then
    qr_pushes=$((qr_pushes + 1))
    echo "$(date -u +%H:%M:%S)Z *** SCAN NEEDED *** open https://github.com/$REPO/blob/$BRANCH/$QR_REMOTE and scan with QQ"
    return 0
  fi
  echo "$(date -u +%H:%M:%S)Z qrcode push HTTP $code"
}

echo "resident start $(date -u +%FT%TZ) token=${TOKEN:+present} end_in=350m relay_every=$((RESIDENT_EVERY / 60))m"

# --- start container ---
docker pull "$IMAGE" || echo "WARN: pull failed, trying local image"
docker rm -f tw2qq >/dev/null 2>&1 || true
docker run -d --name tw2qq \
  -e PORT=8080 \
  -v "$DATA_DIR:/app/data" \
  -v "$PERSIST_DIR:/app/persist" \
  -p 8080:8080 "$IMAGE"
echo "container up"

# --- supervise ---
while [ "$SECONDS" -lt "$END" ]; do
  code=$(curl -sS -o /tmp/h.json -w '%{http_code}' --max-time 30 "$HEALTH_URL" || echo 000)
  if [ "$code" = "200" ]; then ok=$((ok + 1)); miss=0; else miss=$((miss + 1)); fi

  online=$(relay_online)
  if [ "$online" != "true" ]; then
    # Not logged in (yet): make sure the QR reaches the user.
    if [ -f "$QR_LOCAL" ]; then
      age=$(( $(date +%s) - $(stat -c %Y "$QR_LOCAL" 2>/dev/null || echo 0) ))
      [ "$age" -lt 600 ] && push_qrcode
    fi
    echo "$(date -u +%H:%M:%S)Z [$ok+${miss}] HTTP $code relay_online=$online"
  elif [ $((ok % 30)) -eq 1 ]; then
    echo "$(date -u +%H:%M:%S)Z [$i/ok=$ok] HTTP $code relay_online=$online"
  fi

  if [ "$miss" -ge 10 ]; then
    restarts=$((restarts + 1))
    echo "$(date -u +%H:%M:%S)Z health dead x10, restarting container (#$restarts)"
    docker restart tw2qq >/dev/null 2>&1 || true
    miss=0
  fi

  if [ "$SECONDS" -ge "$NEXT_RELAY" ] || [ $((END - SECONDS)) -lt 900 ]; then
    if relay; then :; fi
    if [ $((END - SECONDS)) -lt 900 ]; then NEXT_RELAY=$((SECONDS + 300));
    else NEXT_RELAY=$((SECONDS + RESIDENT_EVERY)); fi
  fi
  sleep 30
done

echo "resident done: ok=$ok restarts=$restarts qr_pushes=$qr_pushes"
[ "$ok" -gt 0 ] || exit 1
