#!/usr/bin/env bash
# First-time setup of the Vault API on Ubuntu 22.04/24.04 (e.g. Sakura VPS).
#
#   sudo bash deploy/setup_server.sh vault.example.com you@example.com
#
# Expects this repository cloned at /opt/obsidian-vault-bridge by your admin
# user (who holds the GitHub deploy key). The service runs as "vaultapi",
# which can only read the code and write /var/lib/vault_api. Only adds its
# own files (user "vaultapi", systemd unit "vault-api", nginx site
# "vault-api"), so other sites on the same server are left alone.
# Safe to re-run: an existing .env is never overwritten.
set -euo pipefail

DOMAIN="${1:?usage: setup_server.sh <domain> <email-for-letsencrypt>}"
EMAIL="${2:?usage: setup_server.sh <domain> <email-for-letsencrypt>}"
APP_DIR=/opt/obsidian-vault-bridge
DATA_DIR=/var/lib/vault_api
APP_USER=vaultapi
PORT=8100

if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
if [[ ! -f "$APP_DIR/vault_api/manage.py" ]]; then
    echo "Repository not found at $APP_DIR (clone it there first)." >&2; exit 1
fi

echo "==> Packages"
apt-get update -q
apt-get install -y -q python3-venv python3-pip nginx certbot python3-certbot-nginx sqlite3 git

echo "==> User and directories"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$DATA_DIR"

echo "==> Python environment"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/vault_api/requirements.txt"

python3 -c "import sqlite3, sys; v = tuple(map(int, sqlite3.sqlite_version.split('.'))); \
print('SQLite', sqlite3.sqlite_version, '(FTS5 trigram OK)' if v >= (3, 34) else '(too old for trigram: LIKE search only)')"

ENV_FILE="$APP_DIR/vault_api/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "==> Generating $ENV_FILE (random keys)"
    gen() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }
    (umask 077; cat > "$ENV_FILE") <<EOF
DJANGO_SECRET_KEY=$(gen)
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=$DOMAIN
VAULT_DB_ENGINE=sqlite
VAULT_DB_PATH=$DATA_DIR/db.sqlite3
VAULT_SYNC_KEYS=windows-pc:$(gen)
VAULT_WRITE_KEYS=
VAULT_READ_KEYS=codex:$(gen)
VAULT_PUBLIC_URL=https://$DOMAIN
VAULT_REQUIRE_HTTPS=true
VAULT_SECURE_PROXY_SSL_HEADER=true
VAULT_TRUSTED_PROXY_COUNT=1
VAULT_MAX_NOTE_BYTES=2097152
VAULT_MAX_BATCH_NOTES=100
VAULT_MAX_REQUEST_BYTES=10485760
VAULT_LOG_LEVEL=INFO
EOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
else
    echo "==> Keeping existing $ENV_FILE"
fi

if ! grep -q '^VAULT_OWNER_PASSWORD_HASH=' "$ENV_FILE"; then
    echo "==> Owner password (asked when ChatGPT / Claude connect; at least 12 characters)"
    while true; do
        read -rsp "Owner password: " PW1; echo
        read -rsp "Again: " PW2; echo
        [[ "$PW1" == "$PW2" ]] || { echo "Passwords do not match."; continue; }
        if LINE=$(printf '%s' "$PW1" | sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/vault_api/manage.py" vault_owner_password --stdin); then
            echo "$LINE" >> "$ENV_FILE"
            break
        fi
    done
    unset PW1 PW2 LINE
fi

echo "==> Database"
cd "$APP_DIR/vault_api"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" manage.py migrate --noinput
chmod 600 "$DATA_DIR"/db.sqlite3* 2>/dev/null || true

echo "==> systemd"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__DATA_DIR__#$DATA_DIR#g" -e "s#__PORT__#$PORT#g" \
    "$APP_DIR/deploy/vault-api.service" > /etc/systemd/system/vault-api.service
systemctl daemon-reload
systemctl enable --now vault-api
systemctl restart vault-api

echo "==> nginx"
sed -e "s#__DOMAIN__#$DOMAIN#g" -e "s#__PORT__#$PORT#g" \
    "$APP_DIR/deploy/nginx-vault-api.conf" > /etc/nginx/sites-available/vault-api
ln -sf /etc/nginx/sites-available/vault-api /etc/nginx/sites-enabled/vault-api
nginx -t
systemctl reload nginx

echo "==> HTTPS certificate (Let's Encrypt)"
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos -n --redirect

echo "==> Check"
sleep 2
code=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/api/vault/recent/" || true)
echo "GET /api/vault/recent/ without a key -> HTTP $code (401 expected)"

cat <<EOF

Done.
  MCP URL for ChatGPT / Claude (sign in with the owner password):
    https://$DOMAIN/api/vault/mcp
  API keys are in $ENV_FILE (readable by root only):
    sudo grep -E '^VAULT_(SYNC|WRITE|READ)_KEYS' $ENV_FILE
    windows-pc -> windows_client/.env VAULT_API_KEY
    codex      -> Codex / Claude Code (read only)
EOF
