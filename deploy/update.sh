#!/usr/bin/env bash
# Deploy the latest code: sudo bash /opt/obsidian-vault-bridge/deploy/update.sh
set -euo pipefail

APP_DIR=/opt/obsidian-vault-bridge
APP_USER=vaultapi

if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
cd "$APP_DIR"
# Pull as the admin user who owns the clone and holds the GitHub deploy key.
sudo -u "${SUDO_USER:-root}" git -C "$APP_DIR" pull --ff-only
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r vault_api/requirements.txt
cd vault_api
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" manage.py migrate --noinput
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" manage.py check --deploy --fail-level ERROR
systemctl restart vault-api
systemctl --no-pager --lines=3 status vault-api
