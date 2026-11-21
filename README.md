# Remi(レミ)

Redmineのチケットを、チャットアプリのように監視・コメントできるWindows向けデスクトップアプリです。
画面はHTML/CSSで作り、Windows標準の WebView2 で表示します。

- 設計書: [docs/design.md](docs/design.md)
- 変更履歴: [CHANGELOG.md](CHANGELOG.md)

## 開発環境

- Python 3.14
- pip(venv)
- Windows 10/11(WebView2 ランタイム)。画面の開発だけなら、どのOSでも `--browser` で動かせます

```powershell
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m remi               # 起動(WebView2のウィンドウ)
python -m remi --browser     # 開発モード: 既定のブラウザで画面を開く
python -m playwright install chromium   # 画面(E2E)のテスト用のブラウザ(初回のみ)
python -m pytest             # テスト
```

## 配布物の作成

Windowsで次を実行すると、`dist/Remi/`(実行ファイル一式)と `dist/Remi-<バージョン>-win64.zip` ができます。

```powershell
python tools/build.py
```

## 配置先を SVN・Git で管理して更新する

ビルド済みのファイル(`dist/Remi/` の中身)を SVN や Git で管理している配置先では、`Remi-update.bat` で
「Remiの終了 → ファイルの更新(`svn update` / `git pull`)→ Remiの再起動」をまとめて行えます。
バッチは、ビルドすると `Remi.exe` と同じ場所(`dist/Remi/` と zip の中)にできます。配置先の `Remi.exe` の隣に置いて使います。

```
Remi-update.bat            # 更新する(エラーのときは、キーを押すまで待つ)
Remi-update.bat /nopause   # 待たない(タスクスケジューラーなどから動かすとき)
```

- SVN か Git かは自動で見分けます。両方のときなどは、バッチの先頭の `VCS`(`git` / `svn` / `none`)で指定します
- 再起動のときにトレイに隠したまま起動するには、先頭の `START_ARGS` を `--background` にします
- 設定(`VCS`・`START_ARGS`・`WAIT_SECONDS`・`QUIT_ARGS`)は、環境変数で渡すこともできます(渡した値が優先されます)
- 終了は `Remi.exe --quit` で行います(×ではトレイに格納されるため)。Remiが起動していなければ、何もせず終わります
- 更新に失敗しても、いまのファイルでRemiを起動し直します(エラーの内容は画面に出ます)

## 構成

| 場所 | 内容 |
| --- | --- |
| `src/remi/api/` | Redmine REST APIクライアント、接続テスト |
| `src/remi/store/` | SQLiteへの保存(キャッシュ、既読、下書き、設定) |
| `src/remi/sync/` | 差分同期エンジンと、サーバーごとの同期スレッド |
| `src/remi/render/` | 本文(Markdown / Textile)のレンダリング、変更履歴の文章化 |
| `src/remi/server/` | 画面向けのローカルAPIサーバー(RPC、SSE) |
| `src/remi/web/` | 画面(HTML / CSS / JavaScript)。バックエンドとの間のデータの形は `js/types.js` |
| `src/remi/i18n/` | 翻訳カタログ(`en.json` / `ja.json`) |
| `src/remi/system/` | Windows固有の処理、トレイ、通知、多重起動の防止 |
| `packaging/` | 配布用のファイル(PyInstallerのフック、更新用のバッチ `Remi-update.bat`) |
| `tools/` | デモ(`demo.py`)、配布物の作成(`build.py`) |

## 翻訳

翻訳はキー方式のJSONカタログ(`src/remi/i18n/en.json`、`ja.json`)で管理します。
画面とPythonで同じカタログを使います。キーを追加したら、両方のファイルに追加してください
(キーのそろい具合と差し込み値の一致はテストで確認します)。

## バージョン

[セマンティックバージョニング](https://semver.org/lang/ja/)に従います。バージョンは
`src/remi/__init__.py` の `__version__` だけで定義します。ひとまずの完成までは `0.y.z` です。
