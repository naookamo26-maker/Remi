# PyInstaller の runtime hook: exe の起動直後、requests を import する前に truststore を有効にする(設計書 §2.3)
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - 失敗しても起動は続け、接続テストで原因を表示する
    pass
