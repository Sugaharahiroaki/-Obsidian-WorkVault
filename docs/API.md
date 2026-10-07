# Vault API リファレンス

ベースURL: `https://<host>/api/vault/`

| 区分 | エンドポイント | 認証 |
|---|---|---|
| **MCP**（Claude / ChatGPTの普通のチャット、Claude Code、Codex） | `POST mcp` | OAuthのアクセストークン、またはAPIキー |
| 読み取り | `GET search/` `GET note/` `GET list/` `GET tree/` `GET recent/` | read / write / sync キー |
| 書き込み（Phase 3） | `POST note/create/` `POST note/append/` `POST note/update/` | write キー |
| Windowsクライアント用 | `POST sync/` `GET changes/` `POST changes/ack/` | sync キー |
| OAuth（サイトのルート） | `/.well-known/oauth-protected-resource` `/.well-known/oauth-authorization-server` `/oauth/register` `/oauth/authorize` `/oauth/token` | — |
| スキーマ | `GET openapi.json` | 不要（データは含まない。MCPには不要） |

削除のエンドポイントはありません。AIからの利用は、通常はMCPを使います（設定手順は [CONNECT_AI.md](CONNECT_AI.md)）。

## 認証

```
Authorization: Bearer <API_KEY>
```

| キーの種類 | 設定 | 用途 | できること |
|---|---|---|---|
| sync | `VAULT_SYNC_KEYS=windows-pc:<key>` | Windowsクライアント | sync、changes、全GET |
| write | `VAULT_WRITE_KEYS=<name>:<key>` | 保存もさせるCLIなど（任意） | 全GET、create / append / update |
| read | `VAULT_READ_KEYS=codex:<key>` | Codex、Claude Code | 全GET |

ClaudeとChatGPTのアプリは固定のキーを使わず、OAuthで接続します（下の「OAuth」を参照）。

- `VAULT_REQUIRE_HTTPS=true`（既定）のとき、HTTPのリクエストは `403 https_required` で拒否します
- `:` の前のクライアント名が、アクセスログと書き込み履歴の `client` に記録されます

## 共通エラー形式

```json
{"error": {"code": "invalid_path", "message": "relative segments are not allowed"}}
```

| HTTP | code | 意味 |
|---|---|---|
| 400 | `invalid_path` / `invalid_query` / `invalid_json` / `invalid_payload` / `invalid_content` / `invalid_base_hash` | 入力が不正 |
| 401 | `unauthorized` | キーがない、または違う |
| 403 | `forbidden` / `https_required` | スコープが足りない / HTTPで呼ばれた |
| 404 | `not_found` | ノートやURLがない |
| 405 | `method_not_allowed` | メソッドが違う |
| 409 | `exists` / （update の競合） | 同じパスのノートがある / 読んだ後に更新されていた |
| 413 | `too_large` / `batch_too_large` / `request_too_large` | サイズや件数の上限を超えた |
| 415 | `unsupported_media_type` | Content-Typeがapplication/jsonでない |
| 422 | `frontmatter_removed` | 更新でフロントマターが消える |

日時はISO 8601形式で、サーバーのタイムゾーン（既定 `Asia/Tokyo`）で返します。

## パスの規則

- Vault相対で、区切りは `/`（`\` は `/` に変換します）。Unicode NFCに正規化します。`.md` のみ
- 次のパスは拒否します（400）
  - `..` と `.` のセグメント、絶対パス、ドライブ文字、UNC
  - URLエンコードしたtraversal（多重エンコードを含む）
  - `.` で始まる名前（`.obsidian`、`.env`）
  - `secrets*`、`password*`、`*.key`、`*.pem`、`*.crt`、`*.pfx`
  - Windowsで使えない文字、制御文字、末尾の `.` や空白、予約デバイス名
- **書き込みはさらに制限があります**: パスの最初のフォルダが `00_INBOX`、`01_Projects`、`02_Ideas`、`03_Knowledge`、`04_AI-Chats`、`05_Prompts`、`06_Decisions`、`07_DevLogs` のいずれかであること
  - `99_Archive`、`08_Templates`、ルート直下のファイル（`00_INBOX.md`、`AGENTS.md`）には書けません

---

## 読み取り

### GET search/?q=

| パラメータ | 必須 | 説明 |
|---|---|---|
| `q` | ○ | 検索語。空白（全角可）で区切るとAND検索。200文字まで |
| `limit` | | 1〜100（既定20） |
| `folder` | | このフォルダ配下（サブフォルダを含む）に限定 |

```json
{
  "mode": "fts",
  "count": 1,
  "results": [
    {"path": "03_Knowledge/Business/2026-10-07_履歴整理_全体マップ.md", "title": "2026-10-07_履歴整理_全体マップ",
     "folder": "03_Knowledge/Business", "updated_at": "2026-10-07T12:00:00+09:00",
     "snippet": "…Claudeの会話履歴を整理した全体マップ。…"}
  ]
}
```

`mode` は、全語が3文字以上なら `fts`（FTS5 trigram）、2文字以下の語を含む・FTSで0件・FTS非対応DBのときは `like` です。

### GET note/?path=

| パラメータ | 必須 | 説明 |
|---|---|---|
| `path` | ○ | ノートのパス |
| `offset` | | 何文字目から返すか（既定0） |
| `max_chars` | | 最大文字数（1〜200000）。省略時は全文 |

```json
{
  "path": "01_Projects/MiseMiru/project.md",
  "title": "project",
  "folder": "01_Projects/MiseMiru",
  "updated_at": "2026-10-07T12:00:00+09:00",
  "content": "---\ntags: [project]\n---\n# ミセミル\n...",
  "content_hash": "9b2a91bb...",
  "created_at": "2026-10-01T18:00:00+09:00",
  "last_synced_at": "2026-10-07T12:00:03+09:00",
  "total_chars": 3906,
  "offset": 0,
  "truncated": false,
  "next_offset": null
}
```

- `content` はObsidianのMarkdownそのものです（フロントマター、`[[link]]`、`![[embed]]`、`#tag` を含む）
- `content_hash` は分割して読んだ場合でも常に全文のhashで、`update` の `base_hash` に使います

### GET list/

`folder`（任意）配下のノート一覧を、パス順で返します。

```json
{"count": 2, "results": [{"path": "01_Projects/A/project.md", "title": "project", "folder": "01_Projects/A", "updated_at": "..."}]}
```

### GET tree/

`folder`（任意）を根にしたツリーを返します。フォルダが先、ノートが後で、名前順です。同期済みの `.md` を含むフォルダだけが出ます。

```json
{"type": "folder", "name": "", "path": "", "children": [
  {"type": "folder", "name": "01_Projects", "path": "01_Projects", "children": [...]},
  {"type": "note", "name": "00_INBOX.md", "path": "00_INBOX.md", "title": "00_INBOX", "updated_at": "..."}
]}
```

### GET recent/

`limit`（1〜100、既定20）と `folder`（任意）を指定でき、`updated_at`（ファイルの最終更新日時）の新しい順に返します。形式は `list/` と同じです。

---

## 書き込み（Phase 3、writeキー）

書き込みは次の流れで処理されます。

1. サーバーのコピーをすぐに更新し、AIが直後に読めば反映されている
2. 同時に「PC反映待ち」として記録する
3. WindowsクライアントがそれをVaultのファイルに反映する（既定30秒ごとに確認）

PCが起動していない間は反映待ちのまま残り、次に起動したときに反映されます。

共通のレスポンス:

```json
{
  "status": "queued",
  "path": "04_AI-Chats/2026-10-07_料金案.md",
  "content_hash": "…",
  "updated_at": "2026-10-07T13:20:00+09:00",
  "change_id": 12,
  "applied_to_pc": false
}
```

### POST note/create/ → 201

```json
{"folder": "04_AI-Chats", "title": "2026-10-07_料金案", "content": "---\nsource: chatgpt\n---\n# 料金案\n..."}
```

- `title` からファイル名を作ります。`\ / : * ? " < > | # ^ [ ]` は `_` に置き換え、120文字までにします
- `folder` と `title` の代わりに `path` を直接指定することもできます
- 同じパスのノートがあれば `409 exists` を返し、上書きはしません

### POST note/append/ → 200

```json
{"path": "01_Projects/MiseMiru/ideas.md", "content": "## 2026-10-07 ChatGPTより\n- 案A"}
```

- 末尾に別の行として追加します。ノートの改行コード（LF / CRLF）に合わせます
- フロントマターや既存の本文には触れません
- PC側では、その時点のファイルの末尾に追加します。PCでの未同期の編集も消えません

### POST note/update/ → 200 / 409 / 422

```json
{
  "path": "01_Projects/MiseMiru/requirements.md",
  "content": "---\ntags: [...]\n---\n# 新しい全文",
  "base_hash": "readNote の content_hash",
  "base_updated_at": "readNote の updated_at（任意）"
}
```

- `base_hash` は必須です。サーバー上のノートが読んだ後に変わっていれば、**上書きせず** `409` を返します。このとき送った本文は `<元の名前> (conflict YYYY-MM-DD HHMMSS).md` として別に保存します（`status: "conflict"`、`original_path` に元のパス）
- 元のノートにフロントマターがあり、新しい本文にない場合は `422 frontmatter_removed` を返します
- PC側でも、ファイルのhashが `base_hash` と一致する場合だけ置き換えます。一致しなければ、同じ形式の競合ファイルを作り、元のファイルは変更しません

---

## Windowsクライアント用（syncキー）

### POST sync/

最大 `VAULT_MAX_BATCH_NOTES` 件（既定100件）をまとめて送れます。1件ごとに独立して処理します。

```json
{"notes": [{"path": "01_Projects/A/project.md", "content": "…", "content_hash": "sha256(任意)",
            "created_at": "2026-10-01T09:00:00+00:00", "modified_at": "2026-10-07T03:00:00+00:00"}]}
```

```json
{"results": [{"path": "01_Projects/A/project.md", "status": "created"},
             {"path": "../x.md", "status": "rejected", "error": "invalid_path"}],
 "summary": {"created": 1, "updated": 0, "unchanged": 0, "rejected": 1}}
```

`status` は次のいずれかです。

| status | 意味 |
|---|---|
| `created` | 新規登録 |
| `updated` | 内容が変わったので更新（PCが正） |
| `unchanged` | hashが同じ |
| `rejected` | 拒否。理由は `error` に入る（`invalid_path` / `invalid_content` / `hash_mismatch` / `too_large` / `invalid_item` / `conflict_retry`） |

### GET changes/

PC反映待ちの書き込みを、古い順に最大50件返します。

```json
{"count": 1, "changes": [{"id": 12, "op": "append", "path": "02_Ideas/a.md", "content": "追記", "base_hash": "…", "created_at": "…"}]}
```

### POST changes/ack/

```json
{"results": [{"id": 12, "status": "applied"},
             {"id": 13, "status": "conflict", "conflict_path": "02_Ideas/a (conflict 2026-10-07 132000).md", "detail": "changed_on_pc"},
             {"id": 14, "status": "failed", "detail": "not_writable"}]}
```

同じ報告を再送しても問題ありません。

---

## MCP: POST mcp

URLは `https://<host>/api/vault/mcp` です（末尾のスラッシュなし）。

- 形式: MCPのStreamable HTTPをステートレスで実装しています。1回のPOSTでJSON-RPCのメッセージを1つ受け取り、JSONで1つ返します（SSEストリームとセッションは使いません）
- 対応バージョン: `2025-11-25`、`2025-06-18`、`2025-03-26`、`2024-11-05`
- メソッド: `initialize`、`ping`、`tools/list`、`tools/call`
  - 通知（`id` のないメッセージ）には202を返します
  - それ以外のメソッドには `-32601` を返します
- 認証: `Authorization: Bearer <OAuthのアクセストークン または APIキー>`
  - トークンがなければ `401` と `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource/api/vault/mcp", scope="vault:read vault:write"` を返します
- `initialize` の `instructions` に、Vaultの構成と保存ルール（会話は04_AI-Chats、確定事項はdecisions.mdなど）を入れています。AI側で指示文を設定する必要はありません

ツール（中身はREST APIと同じサービス層を通るので、パスの検証も書き込みの制限も同じです）:

| ツール | 必要な権限 | 引数 |
|---|---|---|
| `search_notes` | read | `query`、`limit`、`folder` |
| `read_note` | read | `path`、`offset`、`max_chars`（既定20000。続きは `next_offset`） |
| `list_notes` | read | `folder`（最大500件） |
| `get_recent_notes` | read | `limit`、`folder` |
| `create_note` | write | `folder`、`title`、`content` |
| `append_note` | write | `path`、`content` |
| `update_note` | write | `path`、`content`、`base_hash`、`base_updated_at` |

- 書き込み権限がない接続では、`tools/list` に書き込み系のツールが出ません。呼んでもエラーになります
- 読み取り系には `readOnlyHint: true`、`update_note` には `destructiveHint: true` を付けています（AI側が実行前に確認する目安）
- ツールの失敗（不正なパス、ノートがない、など）は、JSON-RPCのエラーではなく `isError: true` の結果として返します

## OAuth（ClaudeとChatGPTのアプリ用）

| エンドポイント | 内容 |
|---|---|
| `GET /.well-known/oauth-protected-resource[/api/vault/mcp]` | RFC 9728。`resource`（MCPのURL）、`authorization_servers` |
| `GET /.well-known/oauth-authorization-server` | RFC 8414。PKCEはS256のみ、トークンエンドポイントの認証は `none`（公開クライアント）、`iss` パラメーターに対応 |
| `POST /oauth/register` | RFC 7591の動的クライアント登録。戻り先は `VAULT_OAUTH_REDIRECT_HOSTS`（既定は chatgpt.com と claude.ai）のhttpsと、Claude Code用のループバック（localhost / 127.0.0.1）だけを許可 |
| `GET/POST /oauth/authorize` | 「接続の許可」画面。持ち主のパスワード（`VAULT_OWNER_PASSWORD_HASH`）で許可し、書き込み権限を付けるか選ぶ。CSRF対策あり。15分間に10回失敗するとロック |
| `POST /oauth/token` | `authorization_code`（PKCE必須、コードは1回限りで5分有効）と `refresh_token`（使うたびに新しいトークンに入れ替え） |

- スコープは `vault:read` と `vault:write`
- アクセストークンの有効期間は1時間、リフレッシュトークンは60日です
- トークンは `resource`（このサーバーのMCPのURL）に結び付けて発行し、ハッシュにしてから保存します

## GET openapi.json

AI向けの7操作を記述したOpenAPI 3.1のスキーマです。sync と changes は含みません。MCPを使う場合は不要で、HTTPのAPIを直接使うツール向けに残しています。サーバーのURLには `VAULT_PUBLIC_URL` を使います。

---

## curl の例

```bash
KEY=...
curl -G -H "Authorization: Bearer $KEY" https://vault.example.com/api/vault/search/ --data-urlencode "q=契約決済"
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"folder":"00_INBOX","title":"テスト","content":"メモ"}' https://vault.example.com/api/vault/note/create/
```

> Windowsの Git Bash では、curlに渡す日本語引数がcp932に変換されて正しく送られないことがあります。PowerShellかPythonで確認してください。

## AIツール名との対応

| ツール | 実装 | HTTP |
|---|---|---|
| `search_notes(query)` | `services/notes.search_notes` | `GET search/`（searchNotes） |
| `read_note(path)` | `services/notes.read_note` | `GET note/`（readNote） |
| `list_notes(folder)` | `services/notes.list_notes` | `GET list/`（listNotes） |
| `get_recent_notes(limit)` | `services/notes.get_recent_notes` | `GET recent/`（getRecentNotes） |
| `create_note` | `services/writes.create_note` | `POST note/create/`（createNote） |
| `append_note` | `services/writes.append_note` | `POST note/append/`（appendNote） |
| `update_note` | `services/writes.update_note` | `POST note/update/`（updateNote） |
