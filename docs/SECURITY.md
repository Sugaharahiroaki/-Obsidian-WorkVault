# セキュリティ

## 守るもの

- WorkVaultの本文（業務、個人、AIとの会話履歴を含む）
- APIキー（sync用とread用）
- PC上のVaultそのもの（Phase 1では、サーバーやAIからPCへ書き込む経路はない）

## 想定する脅威と対策

| 脅威 | 対策 | 実装箇所 |
|---|---|---|
| 秘密ファイルの送信 | `.obsidian/**`、`.env`、`*.key`、`*.pem`、`*.crt`、`*.pfx`、`secrets*`、`password*`、隠しファイル（先頭`.`、Windowsの隠し・システム属性）は送らない。パスのすべてのセグメントを大文字小文字を区別せずに判定する | `windows_client/filters.py` |
| ファイル名は無害だが中身に秘密がある | 内容スキャン: 秘密鍵、AWS / GitHub / OpenAI / Anthropic / Stripe / Slack / Google / NotionのキーとLuhnを満たすカード番号を検知したら送らない。ログにはパスとパターン名だけ書く | `filters.find_secret` |
| symlinkやjunctionでVault外のファイルが読まれる | リンクと再解析ポイントは辿らない。`resolve()` した結果がVault内かも確認する | `filters.fs_exclusion_reason`、`sync.exclusion_reason` |
| path traversal | サーバーで `..`、`.`、絶対パス、ドライブ文字、UNC、`:`（ADS）、制御文字、NUL、URLエンコード（多重を含む）、全角ドット、末尾の`.`や空白、予約デバイス名を拒否する。最後に仮想ルートからはみ出さないかも確認する | `vault_api/vault/security.py` |
| クライアントの不具合や改変で除外対象が届く | サーバーでも同じ除外ルールで拒否する（二重防御） | `security.EXCLUDED_NAME_PATTERNS` |
| APIキーの盗用 | 32文字以上を必須にする。sync / readのスコープ分離により、AI用キーではノートを書き換えられない。比較は定数時間で、キーの値はエラーにもログにも出さない | `vault/auth.py` |
| 平文通信でのキー漏洩 | サーバーは `VAULT_REQUIRE_HTTPS=true` でHTTPを拒否する。クライアントは `https://` 以外への送信を拒否し（localhostのみ例外）、証明書検証を常に有効にする。リダイレクトも辿らない | `views.api_endpoint`、`config.validate_api_url` |
| FTS5クエリの注入 | 各語を引用符付きのフレーズにエスケープする。SQLはすべてパラメータ化する | `vault/search.py` |
| 巨大リクエストによる負荷 | ノートは1件2MB、1リクエスト100件・10MBまで。nginxのレート制限も推奨（INSTALL_SERVER.md） | `settings.py` |
| ログからの情報漏洩 | アクセスログはメタデータのみ。本文、検索クエリ、APIキーは保存しない | `services/audit.py` |
| CSRFやクリックジャッキング | APIはCookieを使わないBearer認証（ビューは `csrf_exempt`）。CSRFとX-Frame-Optionsのミドルウェアは有効にしている | `settings.py` |
| 誤削除 | 削除APIはない。Phase 1ではサーバーからPCへの書き込み経路もない | — |

## 運用上の注意（必ず確認すること）

1. **最初の同期の前に `python sync.py --dry-run` を実行する。** 送信予定の一覧を見て、送りたくないフォルダがあれば `VAULT_EXTRA_EXCLUDES` に追加する。とくに `04_AI-Chats/Claude_Export_*` には会話履歴がそのまま入っている
2. **内容スキャンは完全ではない。** 形式が決まったキーやトークンしか検知できず、「パスワード: xxxx」のような自由記述は検知できない。AGENTS.mdのルール（Vaultに認証情報を置かない）が前提になる
3. **gunicornやnginxのアクセスログにはクエリ文字列が残る。** `?q=検索語` や `?path=` がそのまま記録されるので、INSTALL_SERVER.mdのクエリを含まないログ形式を使う
4. **サーバーのDBにはVault全体が平文で入る。** DBファイルとバックアップは権限 `600` で、専用ユーザーに限定する。可能ならディスク暗号化を使う
5. **キーのローテーション:** `.env` の値を差し替えてgunicornを再起動する。HTTPで送ってしまったキーは漏れたものとして扱い、すぐに交換する
6. **キーは用途ごとに分ける。** Windowsクライアントにはsyncキー、ChatGPTやCodexにはそれぞれ別のreadキーを渡す。ログでクライアントを識別でき、1つだけ失効させることもできる

## 残存リスク（Phase 2 / 3 で要検討）

- **削除の非伝搬:** PCで消したノート（秘密を書いてしまって消した場合を含む）はサーバーに残る。現状はサーバー上でSQLを使って手で消すしかない。管理コマンドを用意するかは未決定
- **AIへのデータ提供:** readキーを渡したAIサービス（ChatGPTなど）に本文が渡る。そのサービスのデータ保持ポリシーに従う
- **プロンプトインジェクション:** Webから取り込んだメモなどに、AIへの指示文が混ざっている可能性がある。Phase 3で書き込みツールを渡す場合は、許可フォルダの限定、削除なし、競合時に上書きしないといった制約が重要になる
- **Phase 3の許可フォルダ:** `00_INBOX` は現状フォルダではなく `00_INBOX.md` というファイル。書き込み許可ルールを設計するときに扱いを決める必要がある
