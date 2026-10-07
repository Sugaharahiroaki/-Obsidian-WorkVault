# アーキテクチャ

## 全体像

```
Windows PC                                      VPS (さくら 2G など)
┌────────────────────────────┐   HTTPS   ┌──────────────────────────────────┐
│ Obsidian WorkVault (*.md)  │  Bearer   │ nginx (TLS, レート制限)            │
│                            │ ────────▶ │  └ gunicorn + Django (vault_api)  │
│ windows_client/sync.py     │ POST sync │     ├ VaultNote      本文のコピー   │
│  watchdog → debounce       │           │     ├ vault_note_fts  FTS5 trigram │
│  → 除外/秘密スキャン        │ ◀──────── │     ├ PendingChange  PC反映待ち     │
│  → sha256差分 → 送信        │ GET       │     └ AccessLog      監査ログ       │
│ windows_client/applier.py  │ changes   │     └ OAuth (DCR+PKCE)  接続の許可 │
│  AIの書き込みを安全に反映    │ + ack     └──────────────▲───────────────────┘
└────────────────────────────┘                         │ MCP  /api/vault/mcp
        ▲                                              │ (OAuth トークン / APIキー)
        │ Obsidian Sync（変更なし）          Claude（iPhone含む）/ ChatGPT（Web）
     iPhone                                  の普通のチャット、Claude Code、Codex
```

- Obsidian Sync（PC↔iPhone）はそのまま使う。AIはObsidian Syncに触れない
- **PCが正（source of truth）**。サーバーは検索・閲覧用のコピーと、AIからの書き込み依頼の受付窓口
- サーバーはPCのファイルシステムに一切アクセスしない。AIの書き込みはPCのクライアントが取りに行き、PC上で安全確認してから反映する

## コンポーネント

### windows_client/

| ファイル | 役割 |
|---|---|
| `config.py` | `.env` と環境変数の読み込み・検証。APIキーは `repr` に出さない。https以外への送信を拒否（localhostのみ例外） |
| `filters.py` | 送ってよいかの判定（パス、隠し属性、symlink、内容の秘密情報スキャン） |
| `watcher.py` | watchdogで監視し、パスごとにdebounce（2.5秒） |
| `sync.py` | スキャン、状態キャッシュ、バッチ送信、リトライ、AIの書き込みの取得と完了報告、CLI |
| `applier.py` | AIの書き込み（create / append / update）をVaultのファイルに反映する。競合時は別ファイルに保存する |

PC → サーバー（同期）:

1. 監視を始めてからフルスキャンする
2. 変更のあったパスは、最後のイベントから2.5秒静かになってから処理する
3. 次の場合は送らない
   - 除外ルールに該当する
   - mtimeとサイズが前回と同じ
   - sha256が前回と同じ
   - 秘密情報らしき内容を含む
4. 最大50件 / 4MBのバッチで `POST sync/` に送る
5. サーバーが受理したものだけ状態ファイルに記録する。失敗分は60秒後に再試行し、10分ごとのフルスキャンでも拾う

サーバー → PC（Phase 3）:

1. 30秒ごとに `GET changes/` で反映待ちの書き込みを取る
2. 古い順に `applier.apply_change` で反映する
3. 結果をPC側の `applied_changes.json` に保存してから `POST changes/ack/` で報告する。報告が失われて同じ依頼が再び届いても、二重には反映しない
4. 反映したファイルはすぐに同期して戻す。競合した場合は、PC側の版を強制的に送り直し、サーバーのコピーをPCの内容に戻す

### vault_api/

| ファイル | 役割 |
|---|---|
| `config/settings.py` | すべて環境変数で設定する。既定はDEBUG=false、HTTPS必須 |
| `vault/models.py` | `VaultNote`、`PendingChange`、`AccessLog` |
| `vault/security.py` | パスの正規化と検証、書き込み許可フォルダ、題名からのファイル名生成、競合ファイル名 |
| `vault/auth.py` | Bearer APIキー（スコープは sync / write / read） |
| `vault/search.py` | FTS5 trigram検索とLIKEフォールバック |
| `vault/services/notes.py` | 読み取り（search / read / list / recent / tree） |
| `vault/services/writes.py` | 書き込み（create / append / update）と競合処理 |
| `vault/services/changes.py` | PC反映待ちの取得と完了報告 |
| `vault/services/sync.py` | PCからの同期のupsert |
| `vault/services/audit.py` | アクセスログ |
| `vault/mcp.py` | MCPサーバー（Streamable HTTP、ステートレス）。7つのツールとVaultの使い方の説明（instructions） |
| `vault/oauth.py` / `oauth_views.py` / `oauth_urls.py` | MCPクライアント用のOAuth 2.1サーバー（メタデータ、動的クライアント登録、パスワードでの許可画面、PKCE、トークンの入れ替え） |
| `vault/openapi.py` | HTTPのAPIを直接使うツール向けのOpenAPIスキーマ（MCPには不要） |
| `vault/management/commands/` | `vault_owner_password`（パスワードのハッシュ作成）、`vault_revoke_tokens`（全接続の無効化） |
| `vault/views.py` | HTTP層（認証、HTTPS強制、エラー整形、ログ） |

ビューは薄く保ち、パス検証と書き込み可否の判定はサービス層で行う。HTTP以外の入口を足しても、同じ検証が必ずかかる。

### deploy/

| ファイル | 役割 |
|---|---|
| `setup_server.sh` | Ubuntuへの初回セットアップ。専用ユーザー、venv、`.env`（キーを自動生成）、migrate、systemd、nginx、Let's Encryptまで行う |
| `update.sh` | 更新（git pull → pip → migrate → 再起動） |
| `vault-api.service` / `nginx-vault-api.conf` | systemdとnginxのテンプレート |

同じVPSにHPやサンプルアプリを置いても干渉しないよう、Vault APIは専用ユーザー `vaultapi` で動かす。書き込めるのは `/var/lib/vault_api` だけで、メモリ上限は400MBにしている。

## データモデル

### VaultNote

| フィールド | 内容 |
|---|---|
| `path` | Vault相対パス（`/` 区切り、NFC）。unique |
| `title` | ファイル名から `.md` を除いたもの |
| `content` | 本文そのまま（フロントマター、リンク、埋め込み、タグ、改行コード、BOMも保持） |
| `folder` | 親フォルダ（ルート直下は `""`） |
| `content_hash` | 本文のUTF-8バイト列のsha256（ファイルのsha256と一致する） |
| `created_at` / `updated_at` | ファイルの作成・更新日時（PCが報告した値）。AIが書き込んだ場合はその時刻 |
| `last_synced_at` | サーバーが最後に受理した日時 |

### OAuthClient / OAuthCode / OAuthToken

- `OAuthClient`: 動的に登録されたMCPクライアント。公開クライアントのみ
- `OAuthCode`: 認可コード。ハッシュにして保存し、5分間有効で1回限り
- `OAuthToken`: アクセストークンとリフレッシュトークンの組。ハッシュにして保存し、`resource` とスコープを持つ。リフレッシュするたびに古い組を無効にする

### PendingChange（AIの書き込み依頼）

`op`（create / append / update）、`path`、`content`（createとupdateは全文、appendは追加分）、`base_hash`、`status`（pending / applied / conflict / failed）、`client`、`created_at`、`applied_at`、`conflict_path`、`detail`。

### AccessLog

`timestamp, action, path, client, success, ip, detail`。本文、検索クエリ、APIキーは保存しない。

## 検索

- SQLiteでは外部コンテンツ方式のFTS5テーブル `vault_note_fts(title, path, content)` を使う。tokenizerは `trigram`。トリガーで自動同期し、順位付けは `bm25(10, 5, 1)`
- 次の場合はLIKEにフォールバックする
  - 2文字以下の語を含む（日本語では頻出）
  - FTSで0件だった
  - FTS非対応（SQLite 3.34未満、PostgreSQL）
- 語は空白（全角空白を含む）で区切ってAND検索する。各語を引用符付きのフレーズにするので、FTS5の構文は注入できない

## AIとの接続（MCP + OAuth）

```
Claude / ChatGPT                         Vault API
  │ POST /api/vault/mcp（トークンなし）  ─▶ 401 + WWW-Authenticate: resource_metadata=...
  │ GET /.well-known/oauth-protected-resource/api/vault/mcp
  │ GET /.well-known/oauth-authorization-server
  │ POST /oauth/register（動的登録。戻り先は chatgpt.com / claude.ai のみ）
  │ ブラウザで /oauth/authorize ─▶ 「接続の許可」画面で持ち主がパスワードを入力し、書き込み可否を選ぶ
  │                              ─▶ 302 で戻り先へ（code, state, iss）
  │ POST /oauth/token（code + PKCEのverifier）─▶ アクセストークン（1時間）+ リフレッシュトークン（60日）
  │ POST /api/vault/mcp（Bearer）─▶ initialize / tools/list / tools/call
```

- ツールは `services/notes.py` と `services/writes.py` を直接呼ぶ。RESTのAPIと検証ルールが完全に同じになる
- `instructions` にVaultの構成と保存ルールを入れているので、AI側で指示文を設定しなくてよい
- 書き込みを許可していない接続には、書き込み系のツール自体を見せない
- Claude CodeとCodexは、`.env` の固定キーを `Authorization` ヘッダーで送ってもよい

## Phase 3: 書き込みと競合の扱い

| 操作 | サーバーでの判定 | PCでの判定（applier） |
|---|---|---|
| create | 同じパスがあれば409。上書きしない | ファイルがあれば競合ファイルを作る。同じ内容なら反映済み扱い |
| append | ノートがなければ404 | その時点のファイルの末尾に追加する（PCでの未同期の編集も消えない）。ファイルがなければ競合ファイルを作る |
| update | `base_hash`（と `base_updated_at`）が一致しなければ、上書きせず競合ノートとして保存して409。フロントマターが消える場合は422 | ファイルのhashが `base_hash` と一致する場合だけ置き換える。一致しなければ競合ファイルを作る |

- 競合ファイル名は `<元の名前> (conflict YYYY-MM-DD HHMMSS).md`。元のノートと同じフォルダに作る
- 書き込めるのは `00_INBOX/`、`01_Projects/` 〜 `07_DevLogs/` の中だけ。サーバー、PCの両方で判定する
  - `00_INBOX` はユーザーの決定により新しいフォルダとして扱い、既存の `00_INBOX.md` には触れない
- ファイルの置き換えは一時ファイル（先頭が `.` の名前で、監視対象外）を書いてから `os.replace` する。新規作成は排他的に作るので、既存ファイルを上書きしない
- 削除は、サーバーにもクライアントにも存在しない

## 設計判断

| 判断 | 理由 |
|---|---|
| DRFを使わず素のDjangoにする | エンドポイントが少なく、依存を最小にできる。既存プロジェクトに組み込みやすい |
| admin / auth を入れない | 攻撃面を減らすため |
| キーを3種類に分ける | PC、ChatGPT、Codexで権限を分け、個別に失効させられる |
| サーバーはPCに直接書かない（PCが取りに行く方式） | PCを外部に公開しないで済む。最終的な安全確認をPC上で行える |
| サーバーのコピーを先に更新する（PCへの反映は後） | AIが自分の書いた内容をすぐ読み返せる。PCとずれた場合は、PCの版を送り直して戻す |
| AIとはMCPでつなぐ（GPTsは使わない） | ユーザーの希望で、普通のチャットから使えることを優先した。Claude（iPhone含む）、ChatGPT（Web）、Claude Code、Codexで同じ接続口を使える |
| OAuthは自前で最小限に実装する | 外部の認証サービスに依存しない。利用者は持ち主1人なので、ユーザー管理ではなくパスワード1つで許可する |

## 既知の制限

- **削除・リネームは伝搬しない**。PCで消したノートはサーバーに残る
- AIの書き込みがPCに反映されるのは、PCで同期クライアントが動いているときだけ（最大30秒ほど遅れる）
- 空フォルダや添付ファイルはサーバーに出ない
- Windowsはファイル名の大文字小文字を区別しないが、サーバーは区別する
- UTF-8以外の `.md` は送らない
- AIが書いたノートが、PC側の `VAULT_EXTRA_EXCLUDES` の対象パスだった場合、PCには反映されるが、以後の変更はサーバーへ同期されない
