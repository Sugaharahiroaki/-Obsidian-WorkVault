# AI（Claude / ChatGPT / Codex）とつなぐ

Vault APIは **MCP**（Model Context Protocol）の接続口 `https://<ドメイン>/api/vault/mcp` を持っています。ここをClaudeやChatGPTの「コネクタ／アプリ」として登録すると、**普通のチャットのまま**次のように頼めます。

- 「ObsidianからTOMONIの料金案を探して」
- 「この会話をObsidianに保存して」

## どれで使えるか

調査時点（2026年10月）の情報です。各社の仕様は変わるので、使う前に公式のヘルプを確認してください。

| AI | 普通のチャット | iPhoneアプリ | 必要なプラン | 接続方法 |
|---|---|---|---|---|
| **Claude**（claude.ai） | ○ | **○** | Free（1個まで）、Pro、Max、Team、Enterprise | カスタムコネクタ（OAuth） |
| **ChatGPT** | ○（開発者モード） | **×（PCのブラウザ版のみ）** | 書き込みまで使うのは主にBusiness / Enterprise。Plus / Proは制限あり | 開発者モードのアプリ（OAuth） |
| Claude Code | ○ | — | — | MCP（APIキー または OAuth） |
| Codex | ○ | — | — | MCP または curl（APIキー） |

**iPhoneの普通のチャットから使うなら、現状はClaudeが確実です。**

## 前提

- サーバーのデプロイが済んでいること（[INSTALL_SERVER.md](INSTALL_SERVER.md)）。`setup_server.sh` で「Owner password」を設定済みであること
- PCで同期クライアントが動いていること（[INSTALL_WINDOWS.md](INSTALL_WINDOWS.md)）。AIが保存したノートがObsidianに反映されるのは、PCが起動している間だけです

## 接続の流れ（共通）

1. ClaudeまたはChatGPTにMCPのURLを登録し、「接続」を押す
2. ブラウザで **「接続の許可」画面**（このサーバーのページ）が開く
3. 戻り先が `claude.ai` または `chatgpt.com` になっていることを確認する
4. 「書き込みも許可する」にチェックを入れるか決める。外すと検索・閲覧だけになる
5. Vaultのパスワード（Owner password）を入力して「許可する」を押す
6. Claude / ChatGPTの画面に戻り、接続済みになる

パスワードを15分間に10回間違えると、一時的にロックされます。

## Claude

### 登録（PCのブラウザで1回だけ）

1. claude.ai を開き、「設定」→「コネクタ」→「カスタムコネクタを追加」を選ぶ
2. 次のとおり入力する
   - 名前: `Obsidian WorkVault`
   - URL: `https://<ドメイン>/api/vault/mcp`
3. 追加したら「接続」を押し、上の「接続の流れ」でパスワードを入力して許可する

### 使い方（iPhone・PC共通）

- 新しいチャットで、入力欄のツール（＋）メニューから「Obsidian WorkVault」が有効になっていることを確認する
- 例: 「ObsidianでMiseMiruの決定事項を探して」
- 例: 「この会話をObsidianに保存して」
  - 保存系の操作は、Claudeが実行前に確認を求めることがあります

## ChatGPT（PCのブラウザ版）

1. ChatGPTの「設定」→「アプリ」→「詳細設定」で **開発者モード** をオンにする
2. 「アプリを作成」を選び、次のとおり入力する
   - 名前: `Obsidian WorkVault`
   - MCPサーバーのURL: `https://<ドメイン>/api/vault/mcp`
   - 認証: OAuth
3. 作成すると「接続の許可」画面が開くので、パスワードを入力して許可する
4. チャットでは、＋メニュー →「開発者モード」→「Obsidian WorkVault」を選んでから頼む

プランによっては、読み取り系のツールしか使えない場合があります。

## Claude Code

固定のAPIキー（`VAULT_READ_KEYS` の `codex:` のキーなど）を使う場合:

```bash
claude mcp add --transport http obsidian https://<ドメイン>/api/vault/mcp --header "Authorization: Bearer <キー>"
```

キーを書かずにOAuthで接続することもできます。ヘッダーを付けずに `claude mcp add` し、Claude Codeの `/mcp` から認証すると、ブラウザで「接続の許可」画面が開きます。

## Codex

[CODEX.md](CODEX.md) を参照してください。

## 接続を切る・パスワードを変える

| やりたいこと | 方法 |
|---|---|
| 1つのAIだけ外す | そのAIの設定画面でコネクタ／アプリを削除する |
| すべての接続を無効にする（スマホを紛失したときなど） | サーバーで次を実行する |
| 接続中の一覧を見る | 同じコマンドに `--list` を付ける |

```bash
cd /opt/obsidian-vault-bridge/vault_api
sudo -u vaultapi ../.venv/bin/python manage.py vault_revoke_tokens          # 全接続を無効化
sudo -u vaultapi ../.venv/bin/python manage.py vault_revoke_tokens --list   # 一覧
```

パスワードを変える手順:

1. `sudo -u vaultapi ../.venv/bin/python manage.py vault_owner_password` で新しいハッシュを作る
2. `vault_api/.env` の `VAULT_OWNER_PASSWORD_HASH=...` の行を差し替える
3. `sudo systemctl restart vault-api` で再起動する

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| 「MCPサーバーに接続できない」 | URLの末尾が `/api/vault/mcp`（スラッシュなし）か。`https://<ドメイン>/.well-known/oauth-protected-resource` をブラウザで開いてJSONが出るか。`.env` の `VAULT_PUBLIC_URL` が `https://<ドメイン>` か |
| 「接続の許可」画面で「パスワードが設定されていません」 | `.env` に `VAULT_OWNER_PASSWORD_HASH` がない。上の手順で作る |
| 許可後にエラーになる | サーバーのログ（`sudo journalctl -u vault-api -n 50`）を見る。戻り先のホストが `VAULT_OAUTH_REDIRECT_HOSTS`（既定は chatgpt.com と claude.ai）に入っているか |
| 保存しようとすると「read-only」と言われる | 許可するときに「書き込みも許可する」を外していた。コネクタを削除して接続し直す |
| 保存できたのにObsidianに出ない | PCの同期クライアントが止まっている。`%LOCALAPPDATA%\obsidian-vault-bridge\sync.log` を確認する |
