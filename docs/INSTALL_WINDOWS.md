# Windows 同期クライアントのセットアップ

前提: Windows 10/11、Python 3.10以上、サーバー側のセットアップが完了していること（[INSTALL_SERVER.md](INSTALL_SERVER.md)）。

## 1. インストール

PowerShell で実行します。

```powershell
cd C:\Users\hsuga\obsidian-vault-bridge
python -m venv .venv
.\.venv\Scripts\pip install -r windows_client\requirements.txt
```

## 2. 設定

```powershell
Copy-Item windows_client\.env.example windows_client\.env
notepad windows_client\.env
```

| 変数 | 内容 |
|---|---|
| `VAULT_ROOT` | `C:\Users\hsuga\Documents\Obsidian\WorkVault` |
| `VAULT_API_URL` | `https://<サーバー>/api/vault`（https必須） |
| `VAULT_API_KEY` | サーバーの `VAULT_SYNC_KEYS` にある `windows-pc:` の後ろの値 |
| `VAULT_EXTRA_EXCLUDES` | 追加で送らないパス（カンマ区切り。`*` は `/` もまたぐ）。例: `04_AI-Chats/Claude_Export_*` |
| `VAULT_SECRET_SCAN_ALLOW` | 秘密情報スキャンの誤検知を許可するパス |
| `VAULT_APPLY_REMOTE_CHANGES` | AI（Claude / ChatGPT）が保存したノートをVaultに反映するか（既定 `true`）。`false` にするとVaultへは一切書き込まない |
| `VAULT_PULL_INTERVAL_SECONDS` | AIの保存をサーバーに確認しに行く間隔（既定30秒） |

`.env` は **Vaultの外**（このリポジトリ内）に置いてください。Git管理外です。

## 3. 送信内容を確認する（必須）

```powershell
.\.venv\Scripts\python windows_client\sync.py --dry-run
```

ネットワークには一切送らず、次の4つを一覧で表示します。

- `Excluded folders`: 除外フォルダ（`.obsidian` など）
- `Not synced`: ルールまたは秘密情報スキャンで送らないファイルと、その理由
- `Would send`: 送信予定のノート
- `Already synced`: 前回から変更がないノート

送りたくないものがあれば `VAULT_EXTRA_EXCLUDES` に追加して、もう一度確認します。

## 4. 接続確認と初回同期

```powershell
.\.venv\Scripts\python windows_client\sync.py --check   # URLとキーの確認
.\.venv\Scripts\python windows_client\sync.py --once    # 全件を1回同期して終了
```

## 5. 常駐させる

フォアグラウンドで動かす場合（Ctrl+Cで停止）:

```powershell
.\.venv\Scripts\python windows_client\sync.py
```

ログオン時に自動で起動する場合（タスクスケジューラ。ウィンドウを出さない `pythonw.exe` を使います）:

```powershell
$repo = "C:\Users\hsuga\obsidian-vault-bridge"
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" `
    -Argument "`"$repo\windows_client\sync.py`"" -WorkingDirectory "$repo\windows_client"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "ObsidianVaultSync" -Action $action -Trigger $trigger -Settings $settings
```

- 停止: `Stop-ScheduledTask -TaskName ObsidianVaultSync`
- 削除: `Unregister-ScheduledTask -TaskName ObsidianVaultSync`

二重起動はロックファイルで防いでいます。手動で起動しようとして「Another sync client is already running」と出た場合は、すでに常駐しています。

## 動作の概要

- 変更を検知してから2.5秒間変更がなければ送信します（Obsidianの自動保存で何度も送らないため）
- 内容のsha256が前回と同じなら送りません
- 10分ごとにフルスキャンし、取りこぼしたイベントを補います。送信に失敗した場合は60秒後に再試行します
- **削除・リネームはサーバーに反映されません**

AIが保存したノートの反映（Phase 3）:

- 30秒ごとにサーバーの「反映待ち」を確認し、Vaultに書き込みます
- 書き込めるのは `00_INBOX/`、`01_Projects/` 〜 `07_DevLogs/` の中だけです。それ以外への書き込み依頼は拒否してログに残します
- 次の場合は元のファイルを変更せず、同じフォルダに `<名前> (conflict YYYY-MM-DD HHMMSS).md` を作ります
  - PCで編集したばかりのノートをAIが更新しようとした
  - 新規作成しようとした名前のファイルが既にあった
- Vaultのファイルを削除することはありません
- ログ例
  - `Applied AI create: 04_AI-Chats/...`（反映した）
  - `Conflict on ...: AI text saved as ...`（競合ファイルに保存した）
  - `Refused AI create on 99_Archive/... (not_writable)`（拒否した）

## ファイルの場所

| 内容 | 場所 |
|---|---|
| ログ | `%LOCALAPPDATA%\obsidian-vault-bridge\sync.log`（1MB×4世代でローテーション） |
| 状態 | `%LOCALAPPDATA%\obsidian-vault-bridge\state.json`（パスごとのhash、mtime、サイズ。本文は含まない） |
| 反映済みのAIの書き込み | `%LOCALAPPDATA%\obsidian-vault-bridge\applied_changes.json`（二重反映を防ぐための記録。消さないこと） |

状態ファイルを削除すると、次回は全ノートを再評価します。サーバー側は内容が同じなら `unchanged` になるだけなので、害はありません。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `Configuration error: VAULT_API_URL must be an https:// URL` | URLを `https://` にする |
| `HTTP 401 unauthorized` | `VAULT_API_KEY` がサーバーの `VAULT_SYNC_KEYS` と一致しているか確認する |
| `HTTP 403 forbidden` | readキーを設定している。syncキーに変える |
| `HTTP 301 redirect` | URLの末尾が `/api/vault` になっているか、httpsか確認する |
| `Not synced: ... (not_utf8)` | ノートをUTF-8で保存し直す |
| `Not synced: ... (possible_secret:...)` | 中身を確認する。誤検知なら `VAULT_SECRET_SCAN_ALLOW` にパスを追加する |

## テスト

```powershell
cd windows_client
..\.venv\Scripts\python -m unittest discover -s tests -t .
```
