#!/usr/bin/env bash
# ============================================================================================
# CANONICAL LIVE LAUNCH  —  *** ARMS REAL-MONEY PROD TRADING ***  (decisions 0015 / 0023 / 0025).
#
# One place for the FULL env so a launch can't miss an arming flag (the 2026-06-15
# PMUS_POST_SIGNING_VERIFIED omission that ran ~40 min capturing nothing, L41) or fumble the cwd
# (the exit-127 cwd-leak, L42). Secrets are read INLINE from the key files — never stored here.
#
#   ./run-live.sh > bot-live.out 2>&1            # launch (default cap = 5)
#   MAX_POSITIONS_PER_SLUG=2 ./run-live.sh ...   # override the per-slug cap
#   ./run-live.sh --smoke                        # pass-through extra args to the binary
# ============================================================================================
set -euo pipefail
cd "$(dirname "$0")"                              # always bot-rs (the bot needs cwd for .env / executions.jsonl)

KEYDIR="C:/Users/kunpa/Downloads/Projects/Authentication/kalshi-api-keys"
[ -f "$KEYDIR/readwrite-private-key.pem" ] || { echo "FATAL: read-write key not found in $KEYDIR" >&2; exit 1; }
[ -f "$KEYDIR/readwrite-key-id" ]          || { echo "FATAL: read-write key-id not found in $KEYDIR" >&2; exit 1; }
[ -x ./target/release/cross-arb-bot.exe ]  || { echo "FATAL: release binary missing (cargo build --release)" >&2; exit 1; }

# --- secrets (inline; NEVER persisted in this file) -----------------------------------------
export KALSHI_RW_KEY_PATH="$KEYDIR/readwrite-private-key.pem"
export KALSHI_ACCESS_KEY="$(tr -d '[:space:]' < "$KEYDIR/readwrite-key-id")"
# pmus PMUS_ACCESS_KEY / PMUS_SECRET load from bot-rs/.env

# --- arming gates (ALL required for live pmus trading) --------------------------------------
export EXECUTION_MODE=live VENUE_ENV=prod CROSSARB_I_UNDERSTAND_PROD=yes PMUS_POST_SIGNING_VERIFIED=yes
export REQUIRE_SETTLE_CLEAN=true CROSSARB_KILL=0
# settlement category gates — OVERRIDABLE (default GATED until recon ~6/23 / ~7/2; owner can arm at their risk)
export ASSUME_SPORTS_SETTLED="${ASSUME_SPORTS_SETTLED:-false}" ASSUME_ECON_SETTLED="${ASSUME_ECON_SETTLED:-false}"
# --- scale-in / re-entry (cap = 1 initial + adds; the initial clip stays 1) -----------------
export ENABLE_SCALE_IN=true ENABLE_REENTRY=true ADD_TAU_GAIN=0.01
export MAX_POSITIONS_PER_SLUG="${MAX_POSITIONS_PER_SLUG:-5}" MAX_CONTRACTS_PER_PAIR=1
# --- exposure caps (per-venue ~$250 wallets; these bind FAR below) --------------------------
export MAX_NOTIONAL_PER_PAIR=6 MAX_NOTIONAL_PER_CLUSTER=12 MAX_TOTAL_NOTIONAL=25 MAX_CONCURRENT_POSITIONS=10
# --- gates (0023 floor / 0017 edge-rate / H1 toxicity / fat-edge / 0020 recovery) -----------
export EDGE_FLOOR_CENTS="${EDGE_FLOOR_CENTS:-2.0}" MIN_EDGE_RATE_CPD=1.0 MAX_DAYS_TO_EVENT=2.0  # floor overridable (0023=2.0)
export SKIP_DEAR_LED_WEATHER=true FAT_EDGE_SIZE_FACTOR=0.5
export MAX_RECOVERY_SPREAD_RATIO=1.0 AGGRESSIVE_SECOND_LEG=true
export ENTRY_COOLDOWN_S=30 LEG_FILL_TIMEOUT_MS=500

echo "[run-live] LIVE prod, cap=${MAX_POSITIONS_PER_SLUG}, floor=${EDGE_FLOOR_CENTS}c, sports=${ASSUME_SPORTS_SETTLED}, econ=${ASSUME_ECON_SETTLED}, pmus armed — launching" >&2
exec ./target/release/cross-arb-bot.exe "$@"
