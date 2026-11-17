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
python -m pytest             # テスト
```

## 配布物の作成

Windowsで次を実行すると、`dist/Remi/`(実行ファイル一式)と `dist/Remi-<バージョン>-win64.zip` ができます。

```powershell
python tools/build.py
```

## 構成

| 場所 | 内容 |
| --- | --- |
| `src/remi/api/` | Redmine REST APIクライアント、接続テスト |
| `src/remi/store/` | SQLiteへの保存(キャッシュ、既読、下書き、設定) |
| `src/remi/sync/` | 差分同期エンジンと、サーバーごとの同期スレッド |
| `src/remi/render/` | 本文(Markdown / Textile)のレンダリング、変更履歴の文章化 |
| `src/remi/server/` | 画面向けのローカルAPIサーバー(RPC、SSE) |
| `src/remi/web/` | 画面(HTML / CSS / JavaScript) |
| `src/remi/i18n/` | 翻訳カタログ(`en.json` / `ja.json`) |
| `src/remi/system/` | Windows固有の処理、トレイ、通知、多重起動の防止 |
| `tools/` | デモ(`demo.py`)、配布物の作成(`build.py`) |

## 翻訳

翻訳はキー方式のJSONカタログ(`src/remi/i18n/en.json`、`ja.json`)で管理します。
画面とPythonで同じカタログを使います。キーを追加したら、両方のファイルに追加してください
(キーのそろい具合と差し込み値の一致はテストで確認します)。

## バージョン

[セマンティックバージョニング](https://semver.org/lang/ja/)に従います。バージョンは
`src/remi/__init__.py` の `__version__` だけで定義します。ひとまずの完成までは `0.y.z` です。
