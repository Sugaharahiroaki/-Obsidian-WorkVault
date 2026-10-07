# アーキテクチャ

## 全体像

```
Windows PC                                   VPS
┌──────────────────────────┐   HTTPS   ┌──────────────────────────────┐
│ Obsidian WorkVault       │  Bearer   │ nginx (TLS終端, レート制限)    │
│  └ *.md                  │ ───────▶  │  └ gunicorn + Django          │
│                          │  POST     │     vault_api / vault アプリ   │
│ windows_client/sync.py   │  /sync/   │      ├ VaultNote  (SQLite)    │
│  watchdog → debounce     │           │      ├ vault_note_fts (FTS5)  │
│  → 除外/秘密スキャン      │           │      └ AccessLog              │
│  → sha256差分 → 送信      │           └──────────────▲───────────────┘
└──────────────────────────┘                          │ GET search/note/tree/recent
        ▲                                             │ (read キー)
        │ Obsidian Sync (変更なし)              ChatGPT / Codex
     iPhone
```

- Obsidian Sync（PC↔iPhone）はそのまま使う。AIクライアントはObsidian Syncに触れない。
- Phase 1の同期は **PC → サーバーの一方向**。PCが正（source of truth）で、サーバーはその読み取り用コピー。
- サーバーはVaultのファイルシステムに一切アクセスしない。DBに保存されたコピーだけを返す。

## コンポーネント

### windows_client/

| ファイル | 役割 |
|---|---|
| `config.py` | `.env` と環境変数の読み込み・検証。APIキーは `repr` に出さない。https以外のURLは拒否（localhostだけ例外） |
| `filters.py` | 送信してよいかの判定。パスルール（`.obsidian`、隠しファイル、`secrets*` など）、ファイルシステムルール（隠し属性、symlink、junction）、内容の秘密情報スキャン |
| `watcher.py` | watchdogによる監視と、パスごとのdebounce（既定2.5秒） |
| `sync.py` | スキャン、状態キャッシュ、バッチ送信、リトライ、CLI（`--once` / `--dry-run` / `--check`） |

同期の流れ:

1. 起動時に監視を開始してからフルスキャンする（スキャン中の編集も取りこぼさないため）
2. 変更イベントのたびに `Debouncer.touch(path)` を呼ぶ。最後のイベントから2.5秒静かになったパスだけを処理する
3. パスごとに判定する
   - 除外ルールに該当 → 送らない
   - mtimeとサイズが前回と同じ → 読まない
   - 内容のsha256が前回送信分と同じ → 送らない（mtimeだけ更新）
   - 秘密情報らしき内容を検知 → 送らずに警告ログ（パスとパターン名だけ）
4. 最大50件 / 4MBのバッチで `POST /api/vault/sync/` に送る
5. サーバーが受理したものだけ状態ファイルに記録する。失敗分は次回の再試行（60秒後）か定期スキャン（10分ごと）で再送する

状態ファイル（`%LOCALAPPDATA%\obsidian-vault-bridge\state.json`）には、パスごとにhash・mtime・サイズ・スキップ理由だけを記録する。本文は保存しない。送信先URL、Vaultの場所、除外ルールのいずれかが変わると自動でリセットされ、全ノートを再評価する。

### vault_api/

| ファイル | 役割 |
|---|---|
| `config/settings.py` | すべて環境変数で設定する。既定はDEBUG=false、HTTPS必須 |
| `vault/models.py` | `VaultNote`、`AccessLog` |
| `vault/security.py` | パス正規化と検証（traversal、URLエンコード、Windows特有の別名を拒否） |
| `vault/auth.py` | Bearer APIキーの認証。スコープは `sync` と `read` |
| `vault/search.py` | FTS5 trigram検索とLIKEフォールバック |
| `vault/services/notes.py` | 読み取り系の実装（Phase 2のAIツールIFになる） |
| `vault/services/sync.py` | 同期のupsert |
| `vault/services/audit.py` | アクセスログの記録 |
| `vault/views.py` | HTTP層（認証、HTTPS強制、エラー整形、ログ記録） |

ビューは薄く保ち、パス検証はサービス層で行う。HTTP以外の入口（Phase 2のMCPやツール呼び出し）から呼ばれても、同じ検証が必ずかかる。

## データモデル

### VaultNote

| フィールド | 内容 |
|---|---|
| `path` | Vault相対パス（`/` 区切り、NFC正規化）。unique |
| `title` | ファイル名から `.md` を除いたもの（Obsidianの表示名と同じ） |
| `content` | 本文そのまま。Frontmatter、`[[link]]`、`![[embed]]`、`#tag`、改行コード、BOMも保持する |
| `folder` | 親フォルダのパス（ルート直下は `""`） |
| `content_hash` | 本文のUTF-8バイト列のsha256。ファイルのバイト列のsha256と一致する |
| `created_at` | ファイルの作成日時（クライアントが報告した値） |
| `updated_at` | ファイルの最終更新日時（同上）。未来日時は受信時刻に丸める |
| `last_synced_at` | サーバーが最後に受理した日時 |

`updated_at` にファイルのmtimeを使うのは、初回の全件同期で「最近変更されたノート」が同期時刻で埋まらないようにするため。Phase 3の競合検出（`content_hash` と `updated_at` の比較）にもこの値を使う。

### AccessLog

`timestamp, action, path, client, success, ip, detail`。`detail` には理由コードや件数（例: `mode=fts hits=3`）だけを入れる。本文、検索クエリ、APIキーは記録しない。

## 検索

- SQLiteでは外部コンテンツ方式のFTS5テーブル `vault_note_fts(title, path, content)` を使う。tokenizerは `trigram`
  - trigramは3文字以上の任意の部分文字列にマッチするので、形態素解析なしで日本語を検索できる
  - トリガーで `vault_vaultnote` と自動同期する
  - 順位付けは `bm25(title=10, path=5, content=1)`
- 次の場合はLIKE（`icontains`）にフォールバックする
  - 2文字以下の語を含む（「税」「契約」など、日本語では頻出）
  - FTSで0件だった
  - FTS5やtrigramが使えない（SQLite 3.34未満、PostgreSQL）
- 語は空白（全角空白を含む）で区切り、AND検索する。各語は引用符で囲んだフレーズとして渡すので、FTS5の構文（`OR`、`NEAR`、`col:`）は注入できない
- レスポンスの `mode` に `fts` / `like` のどちらを使ったかが入る

## 設計判断

| 判断 | 理由 |
|---|---|
| DRFを使わず素のDjangoにする | エンドポイントが5つで依存を最小にできる。既存プロジェクトへの組み込みで衝突しにくい |
| `django.contrib.admin` / `auth` を入れない | 攻撃面を減らすため。閲覧はAPIで足りる |
| APIキーを `sync` と `read` に分ける | ChatGPTやCodexのキーが漏れても、サーバー上のノートは書き換えられない |
| サーバーでもパス検証と除外ルールを二重にかける | クライアントの不具合や改変への備え |
| 削除APIを作らない | 仕様どおり。PCで消したり名前を変えたりしたノートはサーバーに残る（下記「制限」） |

## Phase 2 / 3 への拡張ポイント

- **Phase 2（AIツール）**: `vault/services/notes.py` の `search_notes(query)`、`read_note(path)`、`list_notes(folder)`、`get_recent_notes(limit)` がJSON化できるdictを返す。MCPサーバーやOpenAPI（ChatGPT Actions）からは、これをそのまま呼べばよい。`list_notes` 用のHTTPエンドポイントは未実装で、現状は `tree/` で代替できる
- **Phase 3（書き込み）**: 次の対応が必要になる
  - `security.normalize_note_path` に「書き込み許可フォルダ」の判定を追加する。なお `00_INBOX` は現状**フォルダではなく `00_INBOX.md` というファイル**なので、許可ルールの設計時に要確認
  - 競合検出: 更新要求に `base_hash` を必須にし、サーバーの `content_hash` と一致しなければconflictとして別保存する
  - PCへの反映: クライアントに「サーバー→PC」方向の取り込みを追加する。PC側の現在のhashが `base_hash` と一致する場合だけ書き込み、一致しなければconflictファイルを作る

## 既知の制限（Phase 1）

- **削除・リネームは伝搬しない**。旧パスのノートはサーバーに残り、検索にも出る
- サーバーには `.md` を含むフォルダしか存在しない。空フォルダや添付ファイルはツリーに出ない
- Windowsはファイル名の大文字小文字を区別しないが、サーバーのpathは区別する。大文字小文字だけのリネームは別ノートとして残る
- UTF-8以外の `.md`（Shift_JISなど）は送らずに警告する
