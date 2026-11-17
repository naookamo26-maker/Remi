# Remi(レミ)

Redmineのチケットを、チャットアプリのように監視・コメントできるWindows向けデスクトップアプリです。

- 設計書: [docs/design.md](docs/design.md)
- 変更履歴: [CHANGELOG.md](CHANGELOG.md)

## 開発環境

- Python 3.14
- pip(venv)

```powershell
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python tools/i18n.py build   # 翻訳ファイル(.qm)を生成
python -m remi               # 起動
python -m pytest             # テスト
```

## 翻訳

ソースコード内の文字列は英語で書き、日本語訳は `src/remi/i18n/remi_ja.ts` で管理します。

```powershell
python tools/i18n.py update   # ソースから文字列を抽出して .ts を更新
pyside6-linguist src/remi/i18n/remi_ja.ts   # 翻訳を編集
python tools/i18n.py build    # .qm を生成
python tools/i18n.py check    # 未翻訳の文字列がないか確認
```

## バージョン

[セマンティックバージョニング](https://semver.org/lang/ja/)に従います。バージョンは
`src/remi/__init__.py` の `__version__` だけで定義します。ひとまずの完成までは `0.y.z` です。
