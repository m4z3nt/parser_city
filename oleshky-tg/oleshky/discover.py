"""Етап 1 — пошук публічних каналів/груп про Олешки."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError, RPCError
from telethon.tl import types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from . import db
from .collect import chan_type, entity_username, message_to_record
from .config import Keywords, load_seeds
from .filters import extract_usernames, find_core

try:   # методи можуть бути відсутні в старих версіях Telethon
    from telethon.tl.functions.channels import SearchPostsRequest
except ImportError:  # pragma: no cover
    SearchPostsRequest = None
try:
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest
except ImportError:  # pragma: no cover
    GetChannelRecommendationsRequest = None

log = logging.getLogger("oleshky.discover")

DELAY = 2
HASHTAG_PAGES = 5           # до 5×100 постів на хештег за запуск
FULL_REFRESH_DAYS = 7


@dataclass
class DiscoverOptions:
    depth: int = 1
    recommendations: bool = True
    hashtags: bool = True
    search: bool = True
    snowball: bool = True
    fulltext: bool = False          # SearchPosts(query=…) — обмежено безкоштовними спробами
    min_participants: int = 0
    max_resolve: int = 50           # ліміт резолву username за запуск (Telegram жорстко лімітує)


def _ukey(username: str) -> str:
    return hashlib.sha256(username.lower().encode()).hexdigest()[:24]


class Discoverer:
    def __init__(self, conn, pool, kw: Keywords, labels: dict, salt: str, opts: DiscoverOptions):
        self.conn, self.pool, self.kw, self.labels, self.salt, self.opts = conn, pool, kw, labels, salt, opts
        self.stats = {"new_channels": 0, "hashtag_posts": 0, "resolved": 0}
        self.resolves_left = opts.max_resolve

    # -------------------------------------------------- helpers

    def add_entity(self, entity, via: str, depth: int = 0, force: bool = False) -> bool:
        """Канал/група з username → у channels. Приватні та люди — пропуск."""
        if not isinstance(entity, types.Channel):
            return False
        uname = entity_username(entity)
        if not uname:
            return False
        participants = getattr(entity, "participants_count", None)
        if not force and participants is not None and participants < self.opts.min_participants:
            return False
        is_new = db.upsert_channel(self.conn, {
            "id": entity.id, "username": uname, "title": entity.title, "type": chan_type(entity),
            "participants": participants, "found_via": via, "depth": depth})
        if is_new:
            self.stats["new_channels"] += 1
            log.info("  + @%s (%s, %s)", uname, entity.title, via)
        return is_new

    async def resolve(self, username: str, via: str, depth: int, force: bool = False):
        """Резолв username з кешем; люди/боти не зберігаються (лише хеш у кеші)."""
        row = self.conn.execute("SELECT id, found_via FROM channels WHERE username=?", (username,)).fetchone()
        if row:
            if force:
                self.conn.execute("UPDATE channels SET found_via=? WHERE id=?",
                                  (db._merge_csv(row["found_via"], via), row["id"]))
            return
        key = _ukey(username)
        cached = self.conn.execute("SELECT kind FROM resolve_cache WHERE key=?", (key,)).fetchone()
        if cached and not force:
            return
        if self.resolves_left <= 0:
            return
        self.resolves_left -= 1

        async def _do(client):
            try:
                return await client.get_entity(username)
            except FloodWaitError:
                raise
            except (ValueError, RPCError) as e:
                return e

        ent = await self.pool.run(_do, label=f"resolve @{username}")
        self.stats["resolved"] += 1
        if isinstance(ent, Exception):
            kind = "not_found"
        elif isinstance(ent, types.Channel) and entity_username(ent):
            kind = "channel"
            self.add_entity(ent, via, depth, force=force)
        else:
            kind = "not_public"
        self.conn.execute("INSERT OR REPLACE INTO resolve_cache (key, kind, resolved_at) VALUES (?,?,?)",
                          (key, kind, db.utcnow()))
        self.conn.commit()
        await asyncio.sleep(DELAY)

    # -------------------------------------------------- sources

    async def seeds(self):
        seeds = load_seeds()
        log.info("Seeds: %d", len(seeds))
        for uname, _note in seeds:
            await self.resolve(uname, "seed", 0, force=True)

    async def contacts_search(self):
        terms = self.kw.search_terms
        log.info("contacts.Search: %d форм", len(terms))
        for term in terms:
            try:
                res = await self.pool.run(lambda c, t=term: c(SearchRequest(q=t, limit=100)),
                                          label=f"search {term}")
            except RPCError as e:
                log.warning("Search '%s': %s", term, e)
                continue
            for chat in res.chats:
                self.add_entity(chat, "search", 0)
            self.conn.commit()
            await asyncio.sleep(DELAY)

    async def search_posts(self, *, hashtag: str | None = None, query: str | None = None) -> bool:
        """Глобальний пошук постів. Платний режим (зірки) не вмикаємо НІКОЛИ.

        Повертає False, якщо метод недоступний/ліміт вичерпано (далі не пробувати).
        """
        if SearchPostsRequest is None:
            log.warning("SearchPostsRequest відсутній у цій версії Telethon — пропуск")
            return False
        label = f"#{hashtag}" if hashtag else f"'{query}'"
        offset_rate, offset_peer, offset_id = 0, types.InputPeerEmpty(), 0
        for page in range(HASHTAG_PAGES):
            req = SearchPostsRequest(offset_rate=offset_rate, offset_peer=offset_peer,
                                     offset_id=offset_id, limit=100, hashtag=hashtag, query=query)
            try:
                res = await self.pool.run(lambda c, r=req: c(r), label=f"SearchPosts {label}")
            except RPCError as e:
                log.warning("SearchPosts %s: %s — пропуск (ліміт/premium/недоступно)", label, e)
                return False
            flood = getattr(res, "search_flood", None)
            if flood is not None and query:
                log.info("SearchPosts: безкоштовних запитів лишилось %s/%s", flood.remains, flood.total_daily)
            chats = {c.id: c for c in res.chats}
            for chat in res.chats:
                self.add_entity(chat, "hashtag" if hashtag else "fulltext", 0)
            for m in res.messages:
                if not isinstance(m, types.Message) or not isinstance(m.peer_id, types.PeerChannel):
                    continue
                ent = chats.get(m.peer_id.channel_id)
                uname = entity_username(ent) if ent else None
                if not uname:
                    continue
                chan = {"id": ent.id, "username": uname, "type": chan_type(ent)}
                rec = message_to_record(m, chan, self.salt, self.kw, "hashtag" if hashtag else "search")
                if db.upsert_post(self.conn, rec) == "new":
                    self.stats["hashtag_posts"] += 1
            self.conn.commit()
            next_rate = getattr(res, "next_rate", None)
            if not res.messages or not next_rate:
                break
            last = res.messages[-1]
            offset_rate, offset_peer, offset_id = next_rate, last.peer_id, last.id
            await asyncio.sleep(DELAY)
        return True

    async def hashtags(self):
        for tag in self.kw.hashtags:
            if not await self.search_posts(hashtag=tag):
                break
            await asyncio.sleep(DELAY)

    async def fulltext(self):
        for term in self.kw.core_uk[:1] + self.kw.core_ru[:1] + self.kw.core_lat[:1]:
            if not await self.search_posts(query=term):
                break

    async def recommendations(self):
        if GetChannelRecommendationsRequest is None:
            log.warning("GetChannelRecommendationsRequest відсутній у цій версії Telethon — пропуск")
            return
        rows = [db.effective_channel(r, self.labels) for r in db.get_channels(self.conn)]
        rel = [r for r in rows if r["relevant"] is True and r["type"] == "channel" and not r["inaccessible"]
               and r["depth"] < self.opts.depth]
        log.info("Рекомендації для %d релевантних каналів", len(rel))
        for ch in rel:
            async def _rec(client, u=ch["username"]):
                return await client(GetChannelRecommendationsRequest(channel=await client.get_input_entity(u)))
            try:
                res = await self.pool.run(_rec, label=f"recommendations @{ch['username']}")
            except (RPCError, ValueError) as e:
                log.warning("Рекомендації @%s: %s", ch["username"], e)
                continue
            for chat in res.chats:
                self.add_entity(chat, "recommendation", ch["depth"] + 1)
            self.conn.commit()
            await asyncio.sleep(DELAY)

    async def snowball(self):
        """fwd_from, t.me/<username>, @username з постів каналів глибини < depth."""
        rows = self.conn.execute(
            """SELECT p.text, p.fwd_from_peer, c.depth FROM posts p JOIN channels c ON c.id=p.channel_id
               WHERE p.is_comment=0 AND p.matched_terms<>'' AND c.depth < ?""", (self.opts.depth,)).fetchall()
        known_ids = {r[0] for r in self.conn.execute("SELECT id FROM channels")}
        fwd_ids: dict[int, int] = {}
        names: dict[str, int] = {}
        for r in rows:
            d = r["depth"] + 1
            peer = r["fwd_from_peer"] or ""
            if peer.startswith("channel:"):
                cid = int(peer.split(":", 1)[1])
                if cid not in known_ids:
                    fwd_ids[cid] = min(d, fwd_ids.get(cid, d))
            for u in extract_usernames(r["text"] or ""):
                names[u] = min(d, names.get(u, d))
        log.info("Сніжний ком: %d джерел репостів, %d username", len(fwd_ids), len(names))

        for cid, d in fwd_ids.items():
            async def _get(client, cid=cid):
                try:
                    return await client.get_entity(types.PeerChannel(cid))
                except FloodWaitError:
                    raise
                except (ValueError, RPCError):
                    return None
            ent = await self.pool.run(_get, label=f"fwd {cid}")
            if ent is not None:
                self.add_entity(ent, "snowball", d)
        self.conn.commit()
        for u, d in names.items():
            await self.resolve(u, "snowball", d)

    async def enrich(self):
        """about / participants / linked_chat_id через GetFullChannel (раз на 7 днів)."""
        cutoff = db.to_iso(datetime.now(timezone.utc) - timedelta(days=FULL_REFRESH_DAYS))
        rows = self.conn.execute(
            "SELECT id, username FROM channels WHERE username IS NOT NULL AND inaccessible=0 "
            "AND (full_fetched_at IS NULL OR full_fetched_at < ?)", (cutoff,)).fetchall()
        log.info("Оновлення опису для %d каналів", len(rows))
        for r in rows:
            async def _full(client, u=r["username"]):
                return await client(GetFullChannelRequest(channel=await client.get_input_entity(u)))
            try:
                full = await self.pool.run(_full, label=f"full @{r['username']}")
            except (RPCError, ValueError) as e:
                log.warning("GetFullChannel @%s: %s", r["username"], e)
                self.conn.execute("UPDATE channels SET full_fetched_at=? WHERE id=?", (db.utcnow(), r["id"]))
                continue
            fc = full.full_chat
            self.conn.execute(
                "UPDATE channels SET about=?, participants=COALESCE(?, participants), linked_chat_id=?, "
                "full_fetched_at=? WHERE id=?",
                (fc.about, getattr(fc, "participants_count", None), getattr(fc, "linked_chat_id", None),
                 db.utcnow(), r["id"]))
            self.conn.commit()
            await asyncio.sleep(DELAY)

    def auto_labels(self):
        """relevant/local за назвою+описом; пости враховує collect.refresh_channel_auto_labels."""
        for row in self.conn.execute("SELECT id, title, about, auto_relevant FROM channels").fetchall():
            hit = bool(find_core(f"{row['title'] or ''}\n{row['about'] or ''}", self.kw))
            if hit:
                self.conn.execute("UPDATE channels SET auto_relevant=1, auto_local=1 WHERE id=?", (row["id"],))
        self.conn.commit()


async def run_discover(conn, pool, kw, labels, salt, opts: DiscoverOptions, *, snowball_only=False) -> dict:
    d = Discoverer(conn, pool, kw, labels, salt, opts)
    if not snowball_only:
        await d.seeds()
        if opts.search:
            await d.contacts_search()
        if opts.hashtags:
            await d.hashtags()
        if opts.fulltext:
            await d.fulltext()
    await d.enrich()
    d.auto_labels()
    if opts.recommendations and not snowball_only and opts.depth > 0:
        await d.recommendations()
    if opts.snowball and opts.depth > 0:
        await d.snowball()
    await d.enrich()
    d.auto_labels()
    db.recompute_repost_groups(conn)
    return d.stats
