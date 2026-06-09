#!/usr/bin/env bash
# deploy/deploy.sh — ONE-COMMAND hardened deploy of the cross-arb monitor.
# Run from the repo root on your machine (needs ssh + scp; Git Bash / WSL / macOS / Linux — NO rsync).
#
#   deploy/deploy.sh cross-arb-droplet        # an ~/.ssh/config Host alias (User root)
#   deploy/deploy.sh root@<droplet-ip>
#
# GATED: only after owner sign-off (decision 0006). Ships ONLY the runtime files (the 4 bot modules +
# requirements + unit/provision) — NOT the whole repo — to a dedicated 'cross-arb' user / 0700 dir;
# pushes the two read-only secrets out-of-band; then provisions + starts. No order code ever runs.
set -euo pipefail

HOST="${1:?usage: deploy/deploy.sh <ssh-host>   (e.g. cross-arb-droplet or root@IP)}"
APP_DIR=/opt/cross-arb
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# --- preflight: the two read-only secrets must exist locally (they are never in git) ---
[ -f "$REPO/scripts/.env" ] || { echo "missing scripts/.env locally"; exit 1; }
PEM="$REPO/scripts/kalshi_readonly.pem"
[ -f "$PEM" ] || { echo "missing scripts/kalshi_readonly.pem locally"; exit 1; }

echo ">> [1/5] create the dir skeleton on $HOST"
ssh "$HOST" "mkdir -p $APP_DIR/bot $APP_DIR/scripts/_data $APP_DIR/deploy"

echo ">> [2/5] ship ONLY the runtime cone (4 bot modules + requirements + unit/provision)"
scp "$REPO"/bot/ledger.py "$REPO"/bot/kalshi_book.py "$REPO"/bot/colisted_map.py \
    "$REPO"/bot/monitor.py "$HOST:$APP_DIR/bot/"
scp "$REPO"/requirements.txt "$HOST:$APP_DIR/"
scp "$REPO"/deploy/cross-arb-monitor.service "$REPO"/deploy/provision.sh "$HOST:$APP_DIR/deploy/"

echo ">> [3/5] ship the two secrets OUT-OF-BAND (scripts/.env + Kalshi read-only .pem)"
scp "$REPO/scripts/.env" "$HOST:$APP_DIR/scripts/.env"
scp "$PEM" "$HOST:$APP_DIR/scripts/"

echo ">> [4/5] provision (create cross-arb user, venv, deps, confine 0700, install unit)"
ssh "$HOST" "bash $APP_DIR/deploy/provision.sh"

echo ">> [5/5] (re)start the monitor"
ssh "$HOST" "systemctl restart cross-arb-monitor && sleep 2 && systemctl --no-pager status cross-arb-monitor | head -n 14"

echo
echo "deployed. follow logs:  ssh $HOST journalctl -u cross-arb-monitor -f"
echo "pull data back:         pwsh deploy/pull-data.ps1   (schedule pull+healthcheck: pwsh deploy/register-tasks.ps1)"
