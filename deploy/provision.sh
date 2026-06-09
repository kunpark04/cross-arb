#!/usr/bin/env bash
# deploy/provision.sh — provision the droplet to run the cross-arb monitor as a CONFINED service.
# Runs ON the droplet as root (deploy/deploy.sh invokes it over ssh). Idempotent: safe to re-run.
#
# Creates a dedicated unprivileged 'cross-arb' user that owns ONLY /opt/cross-arb (mode 0700 — readable
# by that user + root only), builds the venv, locks down the secrets, installs the systemd unit.
# READ-ONLY logger; deploy is GATED on owner sign-off (decision 0006).
set -euo pipefail

APP_USER=cross-arb
APP_DIR=/opt/cross-arb

echo ">> system deps (python venv only — file transfers use scp, no rsync needed)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip

echo ">> dedicated unprivileged user '$APP_USER' (system, no login shell, home=$APP_DIR)"
id -u "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo ">> venv + pinned deps (websockets, cryptography — wheels, no swap needed)"
mkdir -p "$APP_DIR/scripts/_data"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo ">> confine: chown $APP_DIR -> $APP_USER, mode 0700 (only $APP_USER + root); secrets 0600"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR"
chmod 600 "$APP_DIR/scripts/.env" 2>/dev/null || true
chmod 600 "$APP_DIR"/scripts/*.pem 2>/dev/null || true

echo ">> install + enable systemd unit (runs as $APP_USER)"
install -m 644 "$APP_DIR/deploy/cross-arb-monitor.service" /etc/systemd/system/cross-arb-monitor.service
systemctl daemon-reload
systemctl enable cross-arb-monitor

if [ -f "$APP_DIR/scripts/.env" ] && ls "$APP_DIR"/scripts/*.pem &>/dev/null; then
  echo ">> creds present — ready. (re)start with:  systemctl restart cross-arb-monitor"
else
  echo ">> !! MISSING creds (scripts/.env and/or Kalshi .pem) — scp them before starting (deploy/README.md)"
fi
