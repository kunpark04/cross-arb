#!/usr/bin/env bash
# provision-bot.sh — run as ROOT on the droplet. Creates the confined `cross-arb-bot` user + /opt/cross-arb-bot
# (0700, holds the TRADE-CAPABLE key — decision 0028), locks down the secrets, installs + enables the unit.
# Idempotent: safe to re-run on every deploy. Leaves the read-only monitor's `cross-arb` user untouched.
set -euo pipefail
APP=/opt/cross-arb-bot
SVCUSER=cross-arb-bot

id "$SVCUSER" >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin "$SVCUSER"

mkdir -p "$APP/secrets"
# binary + wrapper must be executable
chmod 0755 "$APP/cross-arb-bot" "$APP/run-live-droplet.sh"
# secrets locked to the service user only
chmod 0700 "$APP/secrets"
chmod 0600 "$APP/secrets/readwrite-private-key.pem" "$APP/secrets/readwrite-key-id" "$APP/.env" 2>/dev/null || true
chown -R "$SVCUSER:$SVCUSER" "$APP"
chmod 0700 "$APP"                                  # whole dir traversable by the user + root only (matches the monitor)

install -m 0644 "$APP/cross-arb-bot.service" /etc/systemd/system/cross-arb-bot.service
systemctl daemon-reload
systemctl enable cross-arb-bot
echo "[provision-bot] done. start with:  systemctl restart cross-arb-bot"
