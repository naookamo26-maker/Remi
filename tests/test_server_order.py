"""サーバーの並べ替え(ツリー・設定のサーバー一覧のドラッグ&ドロップ)。"""

from fake_redmine import API_KEY
from test_server import app  # noqa: F401 - フィクスチャ

from remi.store.servers import Server


def _servers(app, fake, names=("A", "B", "C")):
    return [app.ctx.servers.save(Server(name=n, url=fake.base_url, sort_order=i), api_key=API_KEY).server_id
            for i, n in enumerate(names)]


def test_reorder_servers(app, fake):
    a, b, c = _servers(app, fake)
    events = []
    app.ctx.bus.on(events.append)
    out = app.rpc("reorderServers", serverIds=[c, a, b])
    assert [s["serverId"] for s in out] == [c, a, b]
    assert [s.server_id for s in app.ctx.servers.list()] == [c, a, b]
    assert any(e["type"] == "serversChanged" for e in events)
    # 変わらなければ、知らせない
    events.clear()
    app.rpc("reorderServers", serverIds=[c, a, b])
    assert not any(e["type"] == "serversChanged" for e in events)


def test_reorder_requires_every_server_exactly_once(app, fake):
    a, b, c = _servers(app, fake)
    for bad in ([a, b], [a, b, c, c], [a, b, 999], [a, b, True], "x"):
        assert app.rpc_error("reorderServers", serverIds=bad)["code"] == "invalid_input"
    assert [s.server_id for s in app.ctx.servers.list()] == [a, b, c]


def test_new_server_goes_last_even_after_a_deletion(app, fake):
    a, b, c = _servers(app, fake)
    app.rpc("reorderServers", serverIds=[c, b, a])
    app.ctx.servers.delete(b)
    assert app.ctx.servers.next_sort_order() == 3  # 番号が空いても、既存のものと重ならない
    d = app.ctx.servers.save(Server(name="D", url=fake.base_url, sort_order=app.ctx.servers.next_sort_order()),
                             api_key=API_KEY).server_id
    assert [s.server_id for s in app.ctx.servers.list()] == [c, a, d]
