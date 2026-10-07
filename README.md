# obsidian-vault-bridge

Windows上のObsidian WorkVaultを、ChatGPTやCodexから検索・閲覧できるようにする「Vault API」です。

```
WorkVault (Windows) → windows_client (監視・差分同期) → HTTPS → vault_api (Django) → ChatGPT / Codex
```

Obsidian Sync（PC↔iPhone）はそのまま使います。AIクライアントはObsidian Syncに接続しません。

## 現在の範囲: Phase 1（AIからは読み取り専用）

- PCのVaultを監視し、`.md` の新規作成・変更を2.5秒のdebounce後に送る。内容のhashが同じなら送らない
- `.obsidian/**`、隠しファイル、`.env`、`*.key`、`*.pem`、`*.crt`、`*.pfx`、`secrets*`、`password*` は送らない。中身にAPIキーなどがあるノートも送らない
- API: `POST sync/`、`GET search/`、`GET note/`、`GET tree/`、`GET recent/`
- 検索: SQLite FTS5（trigram）で日本語を部分一致検索し、2文字以下の語はLIKEで検索する
- APIキー認証（sync / readのスコープ）、HTTPS前提、path traversal防止、アクセスログ（本文は保存しない）
- 削除APIはない

## ディレクトリ

```
vault_api/          Django サーバー（vault アプリは既存プロジェクトにも組み込み可）
windows_client/     Windows 同期クライアント
docs/               ARCHITECTURE / API / SECURITY / INSTALL_WINDOWS / INSTALL_SERVER
```

## ローカルで試す

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r vault_api\requirements.txt -r windows_client\requirements.txt

# テスト
cd vault_api;      ..\.venv\Scripts\python manage.py test vault --settings=config.test_settings; cd ..
cd windows_client; ..\.venv\Scripts\python -m unittest discover -s tests -t .;                 cd ..

# 何が送られるかの確認（送信しない）
.\.venv\Scripts\python windows_client\sync.py --dry-run
```

本番の手順は [docs/INSTALL_SERVER.md](docs/INSTALL_SERVER.md) と [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) を、運用前の注意は [docs/SECURITY.md](docs/SECURITY.md) を参照してください。

## 今後

- Phase 2: `search_notes` / `read_note` / `list_notes` / `get_recent_notes` をAIツールとして公開する（実装は `vault_api/vault/services/notes.py` にある）
- Phase 3: 許可フォルダへの書き込み（create / append / update）と競合検出
