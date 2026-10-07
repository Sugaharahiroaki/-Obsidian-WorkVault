# サーバー（Vault API）のセットアップ

新しく借りるVPS（さくらのVPS 2G、Ubuntu 24.04 を想定）へのデプロイ手順です。HPやサンプルアプリを同じサーバーに置いても干渉しないよう、Vault APIは専用ユーザー・専用サービス・専用のnginxサイトで動かします。

## 0. 準備するもの

- VPS（Ubuntu 22.04 / 24.04）とSSHでログインできる管理ユーザー（sudo可）
- Vault API用のドメイン（例: `vault.example.com`）
  - DNSの **Aレコード** をVPSのIPアドレスに向けておく
- さくらのVPSの **パケットフィルタ** で、80番（HTTP）と443番（HTTPS）を許可しておく（コントロールパネルで設定）
- Let's Encryptの通知を受け取るメールアドレス

## 1. コードを置く（非公開リポジトリ）

VPSで、管理ユーザーとしてGitHub用のデプロイキーを作ります。

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_vault -N ""
cat ~/.ssh/github_vault.pub
```

表示された公開鍵を、GitHubのリポジトリの **Settings → Deploy keys → Add deploy key** に登録します。「Allow write access」はオフのままにします。

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-vault
    HostName github.com
    IdentityFile ~/.ssh/github_vault
EOF
sudo mkdir -p /opt/obsidian-vault-bridge
sudo chown "$USER": /opt/obsidian-vault-bridge
git clone git@github-vault:Sugaharahiroaki/-Obsidian-WorkVault.git /opt/obsidian-vault-bridge
```

## 2. セットアップスクリプトを実行する

```bash
sudo bash /opt/obsidian-vault-bridge/deploy/setup_server.sh vault.example.com you@example.com
```

スクリプトが行うこと:

1. パッケージを入れる（python3-venv、nginx、certbot、sqlite3）
2. 専用ユーザー `vaultapi` と、データ置き場 `/var/lib/vault_api`（権限700）を作る
3. venvを作り、依存パッケージを入れる
4. `vault_api/.env` を作る（秘密鍵とAPIキーはランダムに生成。既にあれば上書きしない）
5. **Owner password を聞く**。ChatGPTやClaudeを接続するときに入力するパスワードです（12文字以上）。ハッシュだけを `.env` に保存します
6. DBを作る（migrate）
7. systemdサービス `vault-api`（gunicorn、127.0.0.1:8100、メモリ上限400MB）を起動する
8. nginxサイト `vault-api` を追加し、Let's EncryptでHTTPSにする（HTTPからHTTPSへの転送も設定）

最後に、MCPのURLとAPIキーの確認方法が表示されます。

```bash
sudo grep -E '^VAULT_(SYNC|WRITE|READ)_KEYS' /opt/obsidian-vault-bridge/vault_api/.env
```

| キー | 渡す先 |
|---|---|
| `windows-pc:` の後ろ | PCの `windows_client/.env` の `VAULT_API_KEY` |
| `codex:` の後ろ | Codex / Claude Code（読み取り専用） |

## 3. 動作確認

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://vault.example.com/api/vault/recent/       # 401
curl -s https://vault.example.com/.well-known/oauth-protected-resource                        # JSON
sudo systemctl status vault-api
```

続けて、PC側（[INSTALL_WINDOWS.md](INSTALL_WINDOWS.md)）とAIとの接続（[CONNECT_AI.md](CONNECT_AI.md)）を設定します。

## 更新

```bash
sudo bash /opt/obsidian-vault-bridge/deploy/update.sh
```

git pull、pip、migrate、check、再起動をまとめて行います。

## 設定（`vault_api/.env`）

| 変数 | 説明 |
|---|---|
| `DJANGO_SECRET_KEY` / `DJANGO_ALLOWED_HOSTS` | Djangoの基本設定 |
| `VAULT_PUBLIC_URL` | `https://<ドメイン>`。OAuthの発行者とMCPのURLの基準になるので、本番では必須 |
| `VAULT_DB_PATH` | SQLiteのファイル（`/var/lib/vault_api/db.sqlite3`） |
| `VAULT_SYNC_KEYS` / `VAULT_WRITE_KEYS` / `VAULT_READ_KEYS` | 固定のAPIキー（`名前:キー` をカンマ区切り） |
| `VAULT_OWNER_PASSWORD_HASH` | 許可画面のパスワードのハッシュ（`manage.py vault_owner_password` で作る） |
| `VAULT_OAUTH_REDIRECT_HOSTS` | OAuthの戻り先として許可するホスト（既定は `chatgpt.com,claude.ai`） |
| `VAULT_REQUIRE_HTTPS` / `VAULT_SECURE_PROXY_SSL_HEADER` / `VAULT_TRUSTED_PROXY_COUNT` | HTTPS関連。nginxの後ろなら `true` / `true` / `1` |
| `DJANGO_SECURE_HSTS_SECONDS` | 動作確認後に `31536000` にするとHSTSが有効になる |

`.env` を変えたら `sudo systemctl restart vault-api` で再起動します。

## 同じVPSに他のサイトを置くとき

- nginxは、サイトごとに `/etc/nginx/sites-available/` に別ファイルを作り、`server_name` で分ける。Vault APIのファイル（`vault-api`）は編集しない
- アプリごとに別のLinuxユーザーとsystemdサービスにする。`/var/lib/vault_api` は `vaultapi` 以外が読めない
- ポートは重ならないようにする（Vault APIは127.0.0.1:8100）

## 既存のDjangoプロジェクトに組み込む場合

1. `vault_api/vault/` をプロジェクトにコピーし、`INSTALLED_APPS` に `"vault"` を追加する
2. `urls.py` に次の2行を追加する

   ```python
   path("api/vault/", include("vault.urls")),
   path("", include("vault.oauth_urls")),   # OAuthの .well-known はサイトのルートに必要
   ```

3. `config/settings.py` の「Vault API」と「OAuth for MCP clients」の設定ブロックをコピーし、`python manage.py migrate vault` を実行する

注意点:

- `DATA_UPLOAD_MAX_MEMORY_SIZE` が既定（2.5MB）なら、PCの `VAULT_BATCH_MAX_BYTES` を小さく（例: `2000000`）する
- PostgreSQLではFTSを使わず、ILIKE検索になる
- APIビューは `csrf_exempt` と `login_required = False` を付けているので、既存のCSRF設定や `LoginRequiredMiddleware` の影響を受けない
- テーブル名はすべて `vault_` で始まり、他のアプリには触れない

## バックアップ

```bash
sudo -u vaultapi sqlite3 /var/lib/vault_api/db.sqlite3 ".backup '/var/lib/vault_api/backup-$(date +%F).sqlite3'"
```

Vaultの本文が平文で入っているので、バックアップの保管先の権限にも注意してください。サーバーのデータは、PCで `sync.py --once --force` を実行すれば作り直せます。ただし、PCに未反映のAIの書き込み（反映待ち）は、サーバーにしかありません。

## テスト（開発PC）

```bash
cd vault_api
../.venv/Scripts/python manage.py test vault --settings=config.test_settings
```
