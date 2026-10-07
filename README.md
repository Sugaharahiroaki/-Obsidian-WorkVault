# obsidian-vault-bridge

Windows上のObsidian WorkVaultを、**Claude / ChatGPTの普通のチャット**やCodexから、検索・閲覧・保存できるようにする「Vault API」です。

```
WorkVault (Windows) ⇄ windows_client ⇄ HTTPS ⇄ vault_api (Django, VPS) ⇄ MCP ⇄ Claude（iPhone含む）/ ChatGPT（Web）/ Codex
```

Obsidian Sync（PC↔iPhone）はそのまま使います。AIはObsidian Syncには接続しません。

## できること

- **同期（PC → サーバー）**
  - `.md` の変更を2.5秒のdebounce後に送る。内容が同じなら送らない
  - 秘密ファイル（`.obsidian`、`.env`、`*.key`、`secrets*` など）と、中身にAPIキーなどを含むノートは送らない
- **検索・閲覧**
  - 日本語の全文検索（SQLite FTS5 trigram。2文字以下の語はLIKE）
  - ノートの取得、一覧、ツリー、最近更新したノート
- **保存（AI → PC）**
  - `create_note` / `append_note` / `update_note`。書けるのは `00_INBOX/` と `01_Projects/` 〜 `07_DevLogs/` だけ
  - PCで先に編集されていたら上書きせず、`(conflict ...)` という別ファイルに保存する。削除の機能はない
- **AIとの接続**
  - MCPの接続口 `https://<ドメイン>/api/vault/mcp`
  - ClaudeとChatGPTはOAuth（持ち主のパスワードで許可）、Claude CodeとCodexはAPIキーで接続する
- **安全対策**
  - HTTPS前提、path traversal防止、権限を分けたAPIキー、本文を残さないアクセスログ

## ディレクトリ

```
vault_api/          Django サーバー（vault アプリは既存プロジェクトにも組み込み可）
windows_client/     Windows 同期クライアント（監視・送信・AIの書き込みの反映）
deploy/             VPS 用セットアップスクリプト、systemd / nginx テンプレート
docs/               ARCHITECTURE / API / SECURITY / INSTALL_SERVER / INSTALL_WINDOWS / CONNECT_AI / CODEX
```

## 使い始める手順

1. VPSにデプロイする → [docs/INSTALL_SERVER.md](docs/INSTALL_SERVER.md)
2. PCで同期クライアントを動かす → [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md)
3. ClaudeやChatGPTに接続する → [docs/CONNECT_AI.md](docs/CONNECT_AI.md)

運用前に [docs/SECURITY.md](docs/SECURITY.md) も確認してください。

## 開発

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r vault_api\requirements.txt -r windows_client\requirements.txt

cd vault_api;      ..\.venv\Scripts\python manage.py test vault --settings=config.test_settings; cd ..
cd windows_client; ..\.venv\Scripts\python -m unittest discover -s tests -t .;                 cd ..

.\.venv\Scripts\python windows_client\sync.py --dry-run   # 送信内容の確認（送信しない）
```
