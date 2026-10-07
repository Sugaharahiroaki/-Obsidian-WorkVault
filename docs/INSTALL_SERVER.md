# サーバー（Vault API）のセットアップ

前提:

- Linux VPS、Python 3.10以上（Django 5.2 LTS）
- SQLite 3.34以上（FTS5 trigramを使うため。それ未満やPostgreSQLではLIKE検索だけになる）
- TLS証明書（Let's Encryptなど）

確認方法:

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

方式は2つあります。

- **A. 単独のプロジェクトとして動かす**（推奨。既存システムに影響しない）
- **B. 既存のDjangoプロジェクトに `vault` アプリを組み込む**

---

## A. 単独デプロイ

### 1. 配置

```bash
sudo useradd --system --home /opt/obsidian-vault-bridge vaultapi
sudo mkdir -p /opt/obsidian-vault-bridge /var/lib/vault_api
sudo chown vaultapi:vaultapi /opt/obsidian-vault-bridge /var/lib/vault_api
sudo chmod 700 /var/lib/vault_api

sudo -u vaultapi git clone <このリポジトリ> /opt/obsidian-vault-bridge
cd /opt/obsidian-vault-bridge
sudo -u vaultapi python3 -m venv .venv
sudo -u vaultapi .venv/bin/pip install -r vault_api/requirements.txt
```

### 2. 設定（`vault_api/.env`）

```bash
sudo -u vaultapi cp vault_api/.env.example vault_api/.env
sudo chmod 600 vault_api/.env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 必要な数だけ生成
```

| 変数 | 例 |
|---|---|
| `DJANGO_SECRET_KEY` | 生成した値 |
| `DJANGO_ALLOWED_HOSTS` | `vault.example.com` |
| `VAULT_DB_PATH` | `/var/lib/vault_api/db.sqlite3` |
| `VAULT_SYNC_KEYS` | `windows-pc:<生成した値>` |
| `VAULT_READ_KEYS` | `chatgpt:<生成した値>,codex:<生成した値>` |
| `VAULT_REQUIRE_HTTPS` | `true` |
| `VAULT_SECURE_PROXY_SSL_HEADER` | `true`（nginxがTLSを終端する場合） |
| `VAULT_TRUSTED_PROXY_COUNT` | `1`（nginx 1段の場合） |
| `DJANGO_SECURE_HSTS_SECONDS` | 動作確認後に `31536000` |

キーの形式が不正だと（32文字未満、`name:` がないなど）起動時にエラーになります。エラーメッセージにキーの値は出ません。

### 3. DB作成と確認

```bash
cd /opt/obsidian-vault-bridge/vault_api
sudo -u vaultapi ../.venv/bin/python manage.py migrate
sudo -u vaultapi ../.venv/bin/python manage.py check --deploy
sudo chmod 600 /var/lib/vault_api/db.sqlite3*
```

FTSが作られたかの確認（`vault_note_fts` と表示されればOK）:

```bash
sudo -u vaultapi sqlite3 /var/lib/vault_api/db.sqlite3 "select name from sqlite_master where name='vault_note_fts';"
```

### 4. gunicorn（systemd）

`/etc/systemd/system/vault-api.service`:

```ini
[Unit]
Description=Obsidian Vault API
After=network.target

[Service]
User=vaultapi
Group=vaultapi
WorkingDirectory=/opt/obsidian-vault-bridge/vault_api
# Access log without query strings (search words / note paths stay out of logs)
ExecStart=/opt/obsidian-vault-bridge/.venv/bin/gunicorn config.wsgi:application \
    --bind 127.0.0.1:8100 --workers 2 \
    --access-logfile - --access-logformat '%%(h)s %%(t)s "%%(m)s %%(U)s" %%(s)s %%(b)s'
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/vault_api

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vault-api
```

`.env` は `settings.py` が読み込むので、`EnvironmentFile` は不要です。

### 5. nginx（TLS終端、レート制限、クエリを含まないログ）

```nginx
limit_req_zone $binary_remote_addr zone=vault_api:10m rate=10r/s;
log_format vault_noquery '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent';

server {
    listen 80;
    server_name vault.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name vault.example.com;

    ssl_certificate     /etc/letsencrypt/live/vault.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vault.example.com/privkey.pem;

    client_max_body_size 10m;            # >= VAULT_MAX_REQUEST_BYTES
    access_log /var/log/nginx/vault_api.log vault_noquery;

    location /api/vault/ {
        limit_req zone=vault_api burst=50 nodelay;
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / { return 404; }
}
```

既存のnginxにサーバーブロックを追加する場合は、既存の設定を壊さないよう別ファイル（`/etc/nginx/sites-available/vault-api`）にして `nginx -t` で確認してから再読み込みしてください。

### 6. 動作確認

```bash
curl -s -H "Authorization: Bearer <readキー>" https://vault.example.com/api/vault/recent/
# => {"count": 0, "results": []}
curl -s https://vault.example.com/api/vault/recent/
# => 401
```

---

## B. 既存の Django プロジェクトに組み込む

1. `vault_api/vault/` を既存プロジェクトにコピーする
2. `settings.py` に追記する

   ```python
   INSTALLED_APPS += ["vault"]

   VAULT_SYNC_KEYS = os.environ.get("VAULT_SYNC_KEYS", "")
   VAULT_READ_KEYS = os.environ.get("VAULT_READ_KEYS", "")
   VAULT_REQUIRE_HTTPS = True
   VAULT_TRUSTED_PROXY_COUNT = 1
   VAULT_MAX_NOTE_BYTES = 2 * 1024 * 1024
   VAULT_MAX_BATCH_NOTES = 100
   ```

3. `urls.py` に追記する

   ```python
   path("api/vault/", include("vault.urls")),
   ```

4. `python manage.py migrate vault`

注意点:

- 既存プロジェクトの `DATA_UPLOAD_MAX_MEMORY_SIZE` が既定（2.5MB）のままなら、クライアントの `VAULT_BATCH_MAX_BYTES` をそれより小さく（例: `2000000`）する
- 既存DBがPostgreSQLの場合、FTSは作られずILIKE検索になる（機能は同じで、件数が多いと遅くなる）
- `LoginRequiredMiddleware` を使っていても、APIビューはログイン不要として扱われる（`login_required = False`）
- APIビューは `csrf_exempt` なので、既存のCSRF設定には影響しない
- `vault` 以外のアプリやテーブルには触れない（テーブル名はすべて `vault_` で始まる）

---

## バックアップ

SQLiteはWALモードなので、ファイルをコピーせずに `.backup` を使います。

```bash
sqlite3 /var/lib/vault_api/db.sqlite3 ".backup '/var/backups/vault_api-$(date +%F).sqlite3'"
chmod 600 /var/backups/vault_api-*.sqlite3
```

Vaultの本文が平文で入っているので、バックアップの保管先の権限にも注意してください。サーバー側のデータは、PCから `sync.py --once --force` を実行すればいつでも作り直せます。

## テスト

```bash
cd vault_api
../.venv/bin/python manage.py test vault --settings=config.test_settings
```
