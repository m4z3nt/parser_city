"""collect/discover проти фейкового Telegram-клієнта (без мережі)."""
import asyncio
from datetime import datetime, timezone

from telethon.tl import types

from oleshky import db
from oleshky.collect import CollectOptions, run_collect
from oleshky.config import load_keywords
from oleshky.discover import DiscoverOptions, Discoverer

KW = load_keywords()
SALT = "test-salt-0123456789"
CH = types.Channel(id=100, title="Олешки News", photo=types.ChatPhotoEmpty(), date=None,
                   broadcast=True, username="oleshky_news", participants_count=12, access_hash=1)


def tmsg(mid, text, day, grouped_id=None):
    return types.Message(id=mid, peer_id=types.PeerChannel(100), date=datetime(2026, 9, day, tzinfo=timezone.utc),
                         message=text, post=True, views=mid * 10, grouped_id=grouped_id)


class FakeClient:
    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda m: -m.id)
        self.calls = []

    async def get_entity(self, x):
        return CH

    async def get_messages(self, entity, limit=None, ids=None):
        if ids is not None:
            return [m for m in self.messages if m.id in set(ids)]
        return self.messages[:limit]

    async def iter_messages(self, entity, search=None, min_id=0, offset_date=None, limit=None, reply_to=None):
        self.calls.append(search)
        for m in self.messages:
            if m.id <= min_id:
                continue
            if search and search.lower() not in m.message.lower():
                continue
            yield m


class FakePool:
    def __init__(self, client):
        self.c = client

    async def run(self, fn, label=""):
        return await fn(self.c)


def test_collect_incremental(tmp_path, monkeypatch):
    import oleshky.collect as col
    monkeypatch.setattr(col, "DELAY_BETWEEN_TERMS", 0)
    monkeypatch.setattr(col, "DELAY_BETWEEN_CHANNELS", 0)
    conn = db.connect(tmp_path / "t.db")
    db.upsert_channel(conn, {"id": 100, "username": "oleshky_news", "title": "Олешки News",
                             "type": "channel", "found_via": "seed"})
    msgs = [tmsg(1, "Старий пост про Олешки", 1), tmsg(2, "Погода", 2),
            tmsg(3, "", 3, grouped_id=9), tmsg(4, "В Олешках фото з міста", 3, grouped_id=9),
            tmsg(5, "Алешки сегодня без света", 4)]
    client = FakeClient(msgs)
    opts = CollectOptions(since=datetime(2026, 9, 2, tzinfo=timezone.utc), refresh_days=0)
    st = asyncio.run(run_collect(conn, FakePool(client), KW, {}, SALT, opts))
    keys = sorted(r[0] for r in conn.execute("SELECT msg_id FROM posts"))
    assert keys == [3, 5], keys          # 1 — до since, 2 — без збігу, 3+4 — альбом
    album = conn.execute("SELECT * FROM posts WHERE msg_id=3").fetchone()
    assert album["album_msg_ids"] == "3,4" and "Олешках" in album["text"]
    assert db.get_sync_state(conn, 100)["last_msg_id"] == 5
    # повторний запуск — 0 нових
    st2 = asyncio.run(run_collect(conn, FakePool(client), KW, {}, SALT, opts))
    assert st2["new"] == 0 and conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    # канал отримав auto_relevant + local (ключ у назві)
    ch = conn.execute("SELECT auto_relevant, auto_local FROM channels").fetchone()
    assert ch["auto_relevant"] == 1 and ch["auto_local"] == 1


def test_discover_add_entity_skips_private(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    d = Discoverer(conn, None, KW, {}, SALT, DiscoverOptions())
    private = types.Channel(id=5, title="Приватний", photo=types.ChatPhotoEmpty(), date=None, broadcast=True)
    user = types.User(id=6, username="someone")
    assert d.add_entity(CH, "search") is True
    assert d.add_entity(private, "search") is False
    assert d.add_entity(user, "search") is False
    assert [r["username"] for r in db.get_channels(conn)] == ["oleshky_news"]
