import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REMI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _fast_sync(monkeypatch):
    """テストでは、サーバーの負荷を抑えるための待ち時間をなくす(その挙動は個別のテストで確かめる)。"""
    from remi.sync import worker

    monkeypatch.setattr(worker, "JOURNAL_QUEUE_INTERVAL", 0.0)
    # Redmineへの通信の最小間隔(既定0.5秒)も、待たない。アプリを動かすテストは通信が数十回になり、これだけで10秒以上かかる
    from remi.api import client

    monkeypatch.setattr(client, "DEFAULT_MIN_INTERVAL", 0.0)


@pytest.fixture
def db(tmp_path):
    from remi.store.db import open_database

    d = open_database(tmp_path / "test.db")
    yield d
    d.close_all()


class _MemoryKeyring:
    """テスト用: OSの資格情報マネージャーを使わず、メモリに保存する。"""

    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        import keyring.errors

        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError(user)
        del self.store[(service, user)]


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    import keyring

    kr = _MemoryKeyring()
    monkeypatch.setattr(keyring, "get_password", kr.get_password)
    monkeypatch.setattr(keyring, "set_password", kr.set_password)
    monkeypatch.setattr(keyring, "delete_password", kr.delete_password)
    return kr


@pytest.fixture
def fake():
    from fake_redmine import FakeRedmine

    f = FakeRedmine()
    f.start()
    yield f
    f.stop()


@pytest.fixture
def client(fake):
    from fake_redmine import API_KEY

    from remi.api.client import RedmineClient

    c = RedmineClient(fake.base_url, API_KEY, min_interval=0)
    yield c
    c.close()
