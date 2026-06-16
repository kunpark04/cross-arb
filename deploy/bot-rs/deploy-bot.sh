#!/usr/bin/env bash
# deploy-bot.sh — ONE-COMMAND deploy of the LIVE bot to the droplet. Run from the repo root (needs ssh + scp;
# Git Bash / WSL / macOS / Linux). decisions 0015 / 0028.
#
#   wsl bash deploy/bot-rs/build-linux.sh           # 1. build the Linux binary first (once / after code changes)
#   deploy/bot-rs/deploy-bot.sh cross-arb-droplet   # 2. ship + provision + start
#
# *** OWNER OVERRIDE OF 0007 (decision 0028): this STAGES THE TRADE-CAPABLE KALSHI KEY ON THE DROPLET. ***
set -euo pipefail
HOST="${1:?usage: deploy/bot-rs/deploy-bot.sh <ssh-host>   (e.g. cross-arb-droplet or root@IP)}"
APP=/opt/cross-arb-bot
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# the trade-capable Kalshi key pair lives OUTSIDE the repo (never committed). Adjust if your path differs.
KEYDIR="${KALSHI_KEYDIR:-/c/Users/kunpa/Downloads/Projects/Authentication/kalshi-api-keys}"
[ -d "$KEYDIR" ] || KEYDIR="/mnt/c/Users/kunpa/Downloads/Projects/Authentication/kalshi-api-keys"  # WSL path fallback

BIN="$HERE/dist/cross-arb-bot"
[ -f "$BIN" ]                                   || { echo "missing $BIN — run: wsl bash deploy/bot-rs/build-linux.sh" >&2; exit 1; }
[ -f "$REPO/bot-rs/.env" ]                      || { echo "missing bot-rs/.env (pmus creds)" >&2; exit 1; }
[ -f "$KEYDIR/readwrite-private-key.pem" ]      || { echo "missing RW key pem in $KEYDIR" >&2; exit 1; }
[ -f "$KEYDIR/readwrite-key-id" ]               || { echo "missing RW key-id in $KEYDIR" >&2; exit 1; }

echo ">> [1/5] dir skeleton on $HOST"
ssh "$HOST" "mkdir -p $APP/secrets"
echo ">> [2/5] ship binary + wrapper + unit + provision"
scp "$BIN" "$HOST:$APP/cross-arb-bot"
scp "$HERE/run-live-droplet.sh" "$HERE/cross-arb-bot.service" "$HERE/provision-bot.sh" "$HOST:$APP/"
echo ">> [3/5] ship secrets OUT-OF-BAND (Kalshi RW key + key-id + pmus .env)"
scp "$KEYDIR/readwrite-private-key.pem" "$HOST:$APP/secrets/readwrite-private-key.pem"
scp "$KEYDIR/readwrite-key-id"          "$HOST:$APP/secrets/readwrite-key-id"
scp "$REPO/bot-rs/.env"                 "$HOST:$APP/.env"
echo ">> [4/5] provision (create cross-arb-bot user, confine 0700, install unit)"
ssh "$HOST" "bash $APP/provision-bot.sh"
echo ">> [5/5] start + status"
ssh "$HOST" "systemctl restart cross-arb-bot && sleep 3 && systemctl --no-pager status cross-arb-bot | head -n 16"

echo
echo "deployed. follow:  ssh $HOST tail -f $APP/bot-live.out"
echo "pull logs back:    pwsh deploy/bot-rs/pull-bot-logs.ps1 $HOST"
echo "STOP trading:      ssh $HOST systemctl stop cross-arb-bot     (or set CROSSARB_KILL=1 via systemctl edit)"
