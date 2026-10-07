# Vault API リファレンス（Phase 1）

ベースURL: `https://<host>/api/vault/`

## 認証

すべてのエンドポイントでヘッダーが必要です。

```
Authorization: Bearer <API_KEY>
```

| キーの種類 | 設定 | できること |
|---|---|---|
| sync | `VAULT_SYNC_KEYS=windows-pc:<key>` | `POST sync/` と全GET |
| read | `VAULT_READ_KEYS=chatgpt:<key>,codex:<key>` | GETのみ |

- `VAULT_REQUIRE_HTTPS=true`（既定）のとき、HTTPのリクエストは `403 https_required` で拒否します
- `:` の前のクライアント名がアクセスログの `client` 列に記録されます

## 共通エラー形式

```json
{"error": {"code": "invalid_path", "message": "relative segments are not allowed"}}
```

| HTTP | code | 意味 |
|---|---|---|
| 400 | `invalid_path` / `invalid_query` / `invalid_json` / `invalid_payload` | 入力が不正 |
| 401 | `unauthorized` | キーがない、または違う |
| 403 | `forbidden` | キーのスコープが足りない（readキーでsyncした場合など） |
| 403 | `https_required` | HTTPで呼ばれた |
| 404 | `not_found` | ノートやURLが存在しない |
| 405 | `method_not_allowed` | メソッドが違う |
| 413 | `batch_too_large` / `request_too_large` | 件数またはサイズの上限を超えた |
| 415 | `unsupported_media_type` | syncのContent-Typeがapplication/jsonでない |

日時はすべてISO 8601形式で、サーバーのタイムゾーン（既定 `Asia/Tokyo`）で返します。

## パスの規則

- Vault相対で、区切りは `/`（`\` は受け付けて `/` に変換します）。Unicode NFCに正規化します
- `.md` で終わるもののみ
- 次のパスは拒否します（400）
  - `..` と `.` のセグメント
  - 絶対パス（`/`、`C:`、UNC）
  - URLエンコードしたtraversal（`%2e%2e%2f`、`%252e...` の多重エンコードを含む）
  - `.` で始まる名前（`.obsidian`、`.env` など）
  - `secrets*`、`password*`、`*.key`、`*.pem`、`*.crt`、`*.pfx`
  - Windowsで使えない文字（`<>:"|?*`）、制御文字
  - 末尾が `.` や空白のセグメント、予約デバイス名（`CON` など）

---

## POST /sync/

Windowsクライアントからノートを登録・更新します。**syncキー専用**です。

リクエスト（`Content-Type: application/json`、最大 `VAULT_MAX_BATCH_NOTES` 件 = 既定100件）:

```json
{
  "notes": [
    {
      "path": "01_Projects/MiseMiru/project.md",
      "content": "---\ntags: [project]\n---\n# ミセミル\n[[requirements]] #project\n",
      "content_hash": "sha256の16進（任意。指定時は本文と一致しないとrejected）",
      "created_at": "2026-10-01T09:00:00+00:00",
      "modified_at": "2026-10-07T03:00:00+00:00"
    }
  ]
}
```

レスポンス（200。1件ごとに独立して処理し、一部がrejectedでも全体は200を返します）:

```json
{
  "results": [
    {"path": "01_Projects/MiseMiru/project.md", "status": "created"},
    {"path": "../x.md", "status": "rejected", "error": "invalid_path"}
  ],
  "summary": {"created": 1, "updated": 0, "unchanged": 0, "rejected": 1}
}
```

| status | 意味 |
|---|---|
| `created` | 新規登録 |
| `updated` | 内容が変わったので上書き（Phase 1はPCが正） |
| `unchanged` | hashが同じ。`last_synced_at` だけ更新 |
| `rejected` | `error` は `invalid_path` / `invalid_content` / `hash_mismatch` / `too_large` / `invalid_item` / `conflict_retry` のいずれか |

削除はできません。

---

## GET /search/?q=

| パラメータ | 必須 | 説明 |
|---|---|---|
| `q` | ○ | 検索語。空白（全角可）で区切るとAND検索。200文字まで |
| `limit` | | 1〜100（既定20） |
| `folder` | | このフォルダ配下（サブフォルダを含む）に限定 |

```
GET /api/vault/search/?q=履歴整理&limit=5
```

```json
{
  "mode": "fts",
  "count": 1,
  "results": [
    {
      "path": "03_Knowledge/Business/2026-10-07_履歴整理_全体マップ.md",
      "title": "2026-10-07_履歴整理_全体マップ",
      "folder": "03_Knowledge/Business",
      "updated_at": "2026-10-07T12:00:00+09:00",
      "snippet": "…Claudeの会話履歴を整理した全体マップ。…"
    }
  ]
}
```

- `mode`: `fts`（FTS5 trigram、全語が3文字以上のとき）、`like`（2文字以下の語を含む／FTSで0件／FTS非対応DB）
- 本文全体は返しません。必要なら `note/` で取得してください

---

## GET /note/?path=

```
GET /api/vault/note/?path=01_Projects/MiseMiru/project.md
```

```json
{
  "path": "01_Projects/MiseMiru/project.md",
  "title": "project",
  "folder": "01_Projects/MiseMiru",
  "updated_at": "2026-10-07T12:00:00+09:00",
  "content": "---\ntags: [project]\n---\n# ミセミル\n...",
  "content_hash": "9b2a91bb...",
  "created_at": "2026-10-01T18:00:00+09:00",
  "last_synced_at": "2026-10-07T12:00:03+09:00"
}
```

`content` はObsidianのMarkdownそのものです（Frontmatter、`[[wiki link]]`、`![[embed]]`、`#tag` を含む）。

---

## GET /tree/

| パラメータ | 説明 |
|---|---|
| `folder` | 省略時はVaultルート。指定するとそのフォルダを根にしたツリーを返す |

```json
{
  "type": "folder", "name": "", "path": "",
  "children": [
    {"type": "folder", "name": "01_Projects", "path": "01_Projects", "children": [
      {"type": "folder", "name": "MiseMiru", "path": "01_Projects/MiseMiru", "children": [
        {"type": "note", "name": "project.md", "path": "01_Projects/MiseMiru/project.md",
         "title": "project", "updated_at": "2026-10-07T12:00:00+09:00"}
      ]}
    ]},
    {"type": "note", "name": "00_INBOX.md", "path": "00_INBOX.md", "title": "00_INBOX", "updated_at": "..."}
  ]
}
```

フォルダが先、ノートが後で、それぞれ名前順に並びます。同期済みの `.md` を含むフォルダだけが出ます。

---

## GET /recent/

| パラメータ | 説明 |
|---|---|
| `limit` | 1〜100（既定20） |
| `folder` | このフォルダ配下に限定 |

```json
{
  "count": 2,
  "results": [
    {"path": "01_Projects/MiseMiru/project.md", "title": "project", "folder": "01_Projects/MiseMiru", "updated_at": "2026-10-07T12:00:00+09:00"},
    {"path": "02_Ideas/idea.md", "title": "idea", "folder": "02_Ideas", "updated_at": "2026-10-07T09:00:00+09:00"}
  ]
}
```

`updated_at`（ファイルの最終更新日時）の新しい順です。

---

## curl の例

```bash
KEY=...   # readキー
curl -G -H "Authorization: Bearer $KEY" https://vault.example.com/api/vault/search/ --data-urlencode "q=契約決済"
curl -G -H "Authorization: Bearer $KEY" https://vault.example.com/api/vault/note/ --data-urlencode "path=00_INBOX.md"
```

> Windowsの Git Bash では、curlに渡す日本語引数がcp932に変換されて検索できないことがあります。PowerShellかPython（requests）で確認してください。

## Phase 2 で想定するツールとの対応

| ツール | 実装（`vault/services/notes.py`） | HTTP |
|---|---|---|
| `search_notes(query)` | `search_notes(query, limit, folder)` | `GET search/` |
| `read_note(path)` | `read_note(path)` | `GET note/` |
| `list_notes(folder)` | `list_notes(folder)` | 未公開（`GET tree/` で代替） |
| `get_recent_notes(limit)` | `get_recent_notes(limit, folder)` | `GET recent/` |
