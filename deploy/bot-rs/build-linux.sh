#!/usr/bin/env bash
# build-linux.sh — build the cross-arb-bot Linux release binary in WSL Ubuntu-24.04 (matches the droplet OS,
# so glibc + the rustls/ring crates line up exactly). The bot uses RUSTLS (not OpenSSL), so the produced
# binary has NO system-TLS dependency — only glibc. decisions 0015 / 0028.
#
#   From the Windows repo root:   wsl bash deploy/bot-rs/build-linux.sh
#   Output:                       deploy/bot-rs/dist/cross-arb-bot   (Linux x86_64 ELF — scp this to the droplet)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"            # .../cross-arb/deploy/bot-rs
SRC="$(cd "$HERE/../../bot-rs" && pwd)"          # .../cross-arb/bot-rs
DIST="$HERE/dist"

# --- toolchain (ring needs a C compiler; rustls means NO OpenSSL/pkg-config dep; rustup is user-local) ---
need=""
command -v cc    >/dev/null 2>&1 || need="build-essential"
command -v curl  >/dev/null 2>&1 || need="$need curl"
if [ -n "${need// }" ]; then
  echo "[build] installing build deps:$need (sudo)"
  sudo apt-get update -qq && sudo apt-get install -y -qq $need || {
    echo "[build] FATAL: need to apt-install ($need) — run once manually, then re-run this script:" >&2
    echo "        sudo apt-get install -y build-essential curl" >&2
    exit 1
  }
fi
if ! command -v cargo >/dev/null 2>&1; then
  export RUSTUP_HOME="$HOME/.rustup" CARGO_HOME="$HOME/.cargo"
  echo "[build] installing rustup (stable, minimal)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
fi
export PATH="$HOME/.cargo/bin:$PATH"

# --- build on the WSL-native FS (building under /mnt/c is slow + can hit 9p file-lock errors) ---
BUILD="$HOME/cross-arb-bot-build"
mkdir -p "$BUILD"
cp -r "$SRC/src" "$SRC/Cargo.toml" "$SRC/Cargo.lock" "$BUILD/"
cd "$BUILD"
echo "[build] cargo build --release --locked  (first build pulls + compiles the dep tree; be patient)"
cargo build --release --locked

mkdir -p "$DIST"
cp "$BUILD/target/release/cross-arb-bot" "$DIST/cross-arb-bot"
echo "[build] OK -> $DIST/cross-arb-bot"
file "$DIST/cross-arb-bot" 2>/dev/null || true
echo "[build] next:  deploy/bot-rs/deploy-bot.sh cross-arb-droplet"
