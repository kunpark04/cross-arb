#!/usr/bin/env bash
# ============================================================================================
# CANONICAL DROPLET LIVE LAUNCH — invoked by cross-arb-bot.service (ExecStart).
# *** ARMS REAL-MONEY PROD TRADING *** (decisions 0015 / 0023 / 0025 / 0028).
#
# Mirrors bot-rs/run-live.sh, but with DROPLET paths (secrets under ./secrets, cwd = /opt/cross-arb-bot).
# >>> KEEP THE ARMING + RISK BLOCK BELOW IN SYNC WITH bot-rs/run-live.sh — only the secret paths differ. <<<
# ============================================================================================
set -euo pipefail
cd "$(dirname "$0")"                              # /opt/cross-arb-bot — cwd for .env (pmus creds), executions.jsonl

SECRETS="$PWD/secrets"
[ -f "$SECRETS/readwrite-private-key.pem" ] || { echo "FATAL: RW key not found in $SECRETS" >&2; exit 1; }
[ -f "$SECRETS/readwrite-key-id" ]          || { echo "FATAL: RW key-id not found in $SECRETS" >&2; exit 1; }
[ -x ./cross-arb-bot ]                       || { echo "FATAL: ./cross-arb-bot binary missing (build-linux.sh)" >&2; exit 1; }

# --- secrets (Kalshi RW inline from files; pmus PMUS_ACCESS_KEY/PMUS_SECRET load from ./.env via dotenvy) ---
export KALSHI_RW_KEY_PATH="$SECRETS/readwrite-private-key.pem"
export KALSHI_ACCESS_KEY="$(tr -d '[:space:]' < "$SECRETS/readwrite-key-id")"

# --- arming gates (ALL required for live pmus trading) --------------------------------------
export EXECUTION_MODE=live VENUE_ENV=prod CROSSARB_I_UNDERSTAND_PROD=yes PMUS_POST_SIGNING_VERIFIED=yes
export REQUIRE_SETTLE_CLEAN=true CROSSARB_KILL=0
# settlement gates — DEFAULT GATED on the droplet (recon ~6/23 sports / ~7/2 econ not yet reached); arm at OWNER risk
# via a systemd drop-in:  systemctl edit cross-arb-bot  ->  [Service]\nEnvironment=ASSUME_SPORTS_SETTLED=true
export ASSUME_SPORTS_SETTLED="${ASSUME_SPORTS_SETTLED:-false}" ASSUME_ECON_SETTLED="${ASSUME_ECON_SETTLED:-false}"
# --- scale-in / re-entry (initial clip sizes UP TO MAX_CONTRACTS_PER_PAIR; adds stack within the per-pair cap) ---
export ENABLE_SCALE_IN=true ENABLE_REENTRY=true ADD_TAU_GAIN=0.01
export MAX_POSITIONS_PER_SLUG="${MAX_POSITIONS_PER_SLUG:-5}" MAX_CONTRACTS_PER_PAIR="${MAX_CONTRACTS_PER_PAIR:-3}"
# --- exposure caps — MAX_NOTIONAL_PER_PAIR=3 makes 3 ctr the HARD per-pair ceiling (incl. scale-in adds) ---
export MAX_NOTIONAL_PER_PAIR=3 MAX_NOTIONAL_PER_CLUSTER=12 MAX_TOTAL_NOTIONAL=25 MAX_CONCURRENT_POSITIONS=10
# --- gates (0023 floor / 0017 edge-rate / H1 toxicity / fat-edge / 0020 recovery) -----------
export EDGE_FLOOR_CENTS="${EDGE_FLOOR_CENTS:-2.0}" MIN_EDGE_RATE_CPD=1.0 MAX_DAYS_TO_EVENT=2.0
export SKIP_DEAR_LED_WEATHER=true FAT_EDGE_SIZE_FACTOR=0.5
export MAX_RECOVERY_SPREAD_RATIO=1.0 AGGRESSIVE_SECOND_LEG=true
export ENTRY_COOLDOWN_S=30 LEG_FILL_TIMEOUT_MS=500

echo "[run-live-droplet] LIVE prod, ${MAX_CONTRACTS_PER_PAIR}ctr/pair (hard cap), slug-cap=${MAX_POSITIONS_PER_SLUG}, floor=${EDGE_FLOOR_CENTS}c, sports=${ASSUME_SPORTS_SETTLED}, econ=${ASSUME_ECON_SETTLED}, pmus armed — launching" >&2
exec ./cross-arb-bot "$@"
