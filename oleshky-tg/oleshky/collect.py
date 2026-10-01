"""Етап 2 — збір постів із публічних каналів/груп (інкрементально)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon.errors import (ChannelPrivateError, MsgIdInvalidError, RPCError,
                             UsernameInvalidError, UsernameNotOccupiedError)
from telethon.tl import types

from . import db
from .config import MEDIA_DIR, Keywords
from .filters import find_core, sender_hash, text_hash

log = logging.getLogger("oleshky.collect")

DELAY_BETWEEN_CHANNELS = 5
DELAY_BETWEEN_TERMS = 1.5
COMMENTS_PER_POST = 200


@dataclass
class CollectOptions:
    since: datetime
    until: datetime | None = None
    with_comments: bool = False
    refresh_days: int = 3          # перечитати свіжі пости: правки, перегляди
    recheck_days: int = 7          # канали з relevant=false (авто) перевіряти раз на N днів
    only_new_channels: bool = False


# ---------------------------------------------------------------- message → record

def media_type(msg) -> str | None:
    m = getattr(msg, "media", None)
    if m is None:
        return None
    if isinstance(m, types.MessageMediaPhoto):
        return "photo"
    if isinstance(m, types.MessageMediaDocument):
        for attr in ("video_note", "voice", "gif", "sticker", "video", "audio"):
            if getattr(msg, attr, None):
                return attr
        return "document"
    if isinstance(m, types.MessageMediaWebPage):
        return "webpage"
    if isinstance(m, types.MessageMediaPoll):
        return "poll"
    if isinstance(m, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        return "geo"
    if isinstance(m, types.MessageMediaContact):
        return "contact"
    return type(m).__name__.replace("MessageMedia", "").lower() or "other"


def sender_info(msg, chat_id: int, chat_type: str, salt: str) -> tuple[str, str | None]:
    """(sender_kind, sender_hash). Жодних id/імен/username людей не повертає."""
    if chat_type == "channel" and getattr(msg, "post", True):
        return "channel", None
    peer = getattr(msg, "from_id", None)
    if isinstance(peer, types.PeerUser):
        return "user", sender_hash(salt, f"u{peer.user_id}")
    if isinstance(peer, types.PeerChannel):
        if peer.channel_id == chat_id:
            return "anonymous", None
        return "channel", sender_hash(salt, f"c{peer.channel_id}")
    return "anonymous", None


def fwd_info(msg) -> tuple[str | None, int | None]:
    fwd = getattr(msg, "fwd_from", None)
    if not fwd:
        return None, None
    peer = getattr(fwd, "from_id", None)
    if isinstance(peer, types.PeerChannel):
        return f"channel:{peer.channel_id}", getattr(fwd, "channel_post", None)
    return "user", None   # репост від людини: хто саме — не зберігаємо


def message_to_record(msg, chan: dict, salt: str, kw: Keywords, matched_by: str, *,
                      is_comment: bool = False, parent: tuple[int, int] | None = None,
                      parent_username: str | None = None) -> dict:
    """chan: {id, username, type}. Для коментарів chan — discussion group."""
    text = msg.message or ""
    kind, shash = sender_info(msg, chan["id"], chan.get("type") or "", salt)
    fwd_peer, fwd_msg = fwd_info(msg)
    if is_comment and parent and parent_username:
        link = f"https://t.me/{parent_username}/{parent[1]}?comment={msg.id}"
    else:
        link = f"https://t.me/{chan['username']}/{msg.id}" if chan.get("username") else None
    replies = getattr(msg, "replies", None)
    return {
        "channel_id": chan["id"],
        "msg_id": msg.id,
        "date": db.to_iso(msg.date),
        "edit_date": db.to_iso(getattr(msg, "edit_date", None)),
        "text": text,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies_count": getattr(replies, "replies", None) if replies else None,
        "media_type": media_type(msg),
        "grouped_id": getattr(msg, "grouped_id", None),
        "album_msg_ids": str(msg.id) if getattr(msg, "grouped_id", None) else None,
        "fwd_from_peer": fwd_peer,
        "fwd_from_msg_id": fwd_msg,
        "link": link,
        "matched_terms": ",".join(find_core(text, kw)),
        "matched_by": matched_by,
        "text_hash": text_hash(text),
        "sender_kind": kind,
        "sender_hash": shash,
        "is_comment": 1 if is_comment else 0,
        "parent_channel_id": parent[0] if parent else None,
        "parent_msg_id": parent[1] if parent else None,
    }


def merge_album(records: list[dict]) -> list[dict]:
    """Елементи альбому (однаковий grouped_id) → один запис з найменшим msg_id."""
    singles, albums = [], {}
    for r in records:
        if r.get("grouped_id"):
            albums.setdefault((r["channel_id"], r["grouped_id"]), []).append(r)
        else:
            singles.append(r)
    for items in albums.values():
        items.sort(key=lambda r: r["msg_id"])
        head = dict(items[0])
        texts = [r["text"] for r in items if r["text"]]
        head["text"] = "\n\n".join(dict.fromkeys(texts))
        head["album_msg_ids"] = ",".join(str(r["msg_id"]) for r in items)
        head["media_type"] = "album"
        head["matched_terms"] = ",".join(dict.fromkeys(
            t for r in items for t in (r["matched_terms"] or "").split(",") if t))
        head["matched_by"] = ",".join(dict.fromkeys(
            t for r in items for t in (r["matched_by"] or "").split(",") if t))
        head["views"] = max((r["views"] or 0) for r in items) or None
        head["forwards"] = max((r["forwards"] or 0) for r in items) or None
        head["edit_date"] = max((r["edit_date"] or "" for r in items), default="") or None
        head["text_hash"] = text_hash(head["text"])
        singles.append(head)
    return singles


def chan_type(entity) -> str:
    return "channel" if getattr(entity, "broadcast", False) else "megagroup"


def entity_username(entity) -> str | None:
    if getattr(entity, "username", None):
        return entity.username
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", False):
            return u.username
    return None


# ---------------------------------------------------------------- per channel

async def _fetch_album_siblings(client, entity, msgs: dict):
    """У пошуку приходить лише елемент альбому з підписом — дотягуємо сусідів."""
    want = set()
    for m, _ in msgs.values():
        if m.grouped_id:
            want.update(range(max(1, m.id - 9), m.id + 10))
    want -= set(msgs)
    if not want:
        return
    groups = {m.grouped_id for m, _ in msgs.values() if m.grouped_id}
    by_group = {m.grouped_id: how for m, how in msgs.values() if m.grouped_id}
    ids = sorted(want)
    for i in range(0, len(ids), 100):
        for m in await client.get_messages(entity, ids=ids[i:i + 100]):
            if m and m.grouped_id in groups:
                msgs[m.id] = (m, by_group[m.grouped_id])


async def collect_channel(client, conn, ch: dict, kw: Keywords, salt: str, opts: CollectOptions) -> dict:
    stats = {"channel": ch["username"], "new": 0, "updated": 0, "comments": 0}
    try:
        entity = await client.get_entity(ch["username"])
    except (ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
        log.warning("@%s недоступний: %s", ch["username"], e)
        conn.execute("UPDATE channels SET inaccessible=1, updated_at=? WHERE id=?", (db.utcnow(), ch["id"]))
        conn.commit()
        stats["error"] = str(e)
        return stats
    if not isinstance(entity, types.Channel) or not entity_username(entity):
        log.warning("@%s — не публічний канал/група, пропуск", ch["username"])
        conn.execute("UPDATE channels SET inaccessible=1 WHERE id=?", (ch["id"],))
        conn.commit()
        return stats

    chan = {"id": entity.id, "username": entity_username(entity), "type": chan_type(entity)}
    state = db.get_sync_state(conn, entity.id)
    min_id = state["last_msg_id"] if state else 0
    top = await client.get_messages(entity, limit=1)
    top_id = top[0].id if top else 0
    mode = "local" if ch.get("local") else "search"

    msgs: dict[int, tuple] = {}
    if top_id > min_id:
        if mode == "local":
            async for m in client.iter_messages(entity, min_id=min_id, offset_date=opts.until):
                if m.date < opts.since:
                    break
                if isinstance(m, types.Message) and (m.message or m.grouped_id):
                    msgs[m.id] = (m, "local_channel")
        else:
            for term in kw.search_terms:
                async for m in client.iter_messages(entity, search=term, min_id=min_id,
                                                    offset_date=opts.until):
                    if m.date < opts.since:
                        break
                    if isinstance(m, types.Message):
                        msgs.setdefault(m.id, (m, "search"))
                await asyncio.sleep(DELAY_BETWEEN_TERMS)
        await _fetch_album_siblings(client, entity, msgs)

    records = merge_album([message_to_record(m, chan, salt, kw, how) for m, how in msgs.values()])
    for rec in records:
        if not rec["text"] and rec["matched_by"] == "local_channel":
            continue                       # фото без підпису: аналізувати нічого
        st = db.upsert_post(conn, rec)
        stats[st] = stats.get(st, 0) + 1

    # перечитуємо свіжі пости: правки + перегляди/репости
    if opts.refresh_days > 0:
        cutoff = db.to_iso(datetime.now(timezone.utc) - timedelta(days=opts.refresh_days))
        ids = [r[0] for r in conn.execute(
            "SELECT msg_id FROM posts WHERE channel_id=? AND is_comment=0 AND date>=?",
            (entity.id, cutoff))]
        ids = [i for i in ids if i not in msgs]
        for i in range(0, len(ids), 100):
            fresh = [m for m in await client.get_messages(entity, ids=ids[i:i + 100]) if m]
            for m in fresh:
                old = conn.execute(
                    "SELECT matched_by, album_msg_ids FROM posts WHERE channel_id=? AND msg_id=?",
                    (entity.id, m.id)).fetchone()
                rec = message_to_record(m, chan, salt, kw, old["matched_by"] if old else "search")
                rec["grouped_id"] = None
                if old and "," in (old["album_msg_ids"] or ""):
                    rec["edit_date"] = None   # текст альбому злитий — оновлюємо лише лічильники
                st = db.upsert_post(conn, rec)
                if st == "updated":
                    stats["updated"] += 1

    if opts.with_comments and chan["type"] == "channel":
        stats["comments"] = await collect_comments(client, conn, entity, chan, records, kw, salt)

    if opts.until is None:      # з --until не зсуваємо курсор, щоб не пропустити новіше
        db.set_sync_state(conn, entity.id, max(top_id, min_id), mode)
    conn.commit()
    return stats


async def collect_comments(client, conn, entity, chan, records, kw, salt) -> int:
    """Коментарі під новими постами каналу. Автори — лише sender_kind + sender_hash."""
    count = 0
    for rec in records:
        if not rec.get("replies_count"):
            continue
        try:
            async for c in client.iter_messages(entity, reply_to=rec["msg_id"], limit=COMMENTS_PER_POST):
                if not isinstance(c, types.Message) or not c.message:
                    continue
                group_id = getattr(c.peer_id, "channel_id", None)
                if group_id is None:
                    continue
                grp = {"id": group_id, "username": None, "type": "megagroup"}
                crec = message_to_record(c, grp, salt, kw, "comment", is_comment=True,
                                         parent=(chan["id"], rec["msg_id"]),
                                         parent_username=chan["username"])
                if db.upsert_post(conn, crec) == "new":
                    count += 1
        except (MsgIdInvalidError, ChannelPrivateError) as e:
            log.info("Коментарі до %s/%s недоступні: %s", chan["username"], rec["msg_id"], e)
        await asyncio.sleep(0.5)
    return count


# ---------------------------------------------------------------- stage

def channels_to_collect(conn, labels, opts: CollectOptions, only: str | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for row in db.get_channels(conn):
        ch = db.effective_channel(row, labels)
        if only:
            if ch["username"].lower() == only.lower():
                out.append(ch)
            continue
        if ch["inaccessible"]:
            continue
        state = db.get_sync_state(conn, ch["id"])
        if opts.only_new_channels and state:
            continue
        if ch["relevant"] is False:
            lab = labels.get(ch["username"].lower(), {})
            if lab.get("relevant") is False or not state:
                continue
            last = datetime.fromisoformat(state["last_run_at"])
            if now - last < timedelta(days=opts.recheck_days):
                continue
        out.append(ch)
    return out


async def run_collect(conn, pool, kw: Keywords, labels: dict, salt: str, opts: CollectOptions,
                      only: str | None = None) -> dict:
    chans = channels_to_collect(conn, labels, opts, only)
    log.info("Collect: %d каналів, since=%s until=%s", len(chans), opts.since.date(),
             opts.until.date() if opts.until else "—")
    total = {"channels": len(chans), "new": 0, "updated": 0, "comments": 0, "errors": 0}
    for i, ch in enumerate(chans, 1):
        try:
            st = await pool.run(lambda c, ch=ch: collect_channel(c, conn, ch, kw, salt, opts),
                                label=f"collect @{ch['username']}")
        except RPCError as e:
            log.error("@%s: %s", ch["username"], e)
            total["errors"] += 1
            continue
        for k in ("new", "updated", "comments"):
            total[k] += st.get(k, 0)
        total["errors"] += 1 if st.get("error") else 0
        log.info("[%d/%d] @%s: +%d нових, %d оновлено, %d коментарів", i, len(chans),
                 ch["username"], st.get("new", 0), st.get("updated", 0), st.get("comments", 0))
        if i < len(chans):
            await asyncio.sleep(DELAY_BETWEEN_CHANNELS)
    db.recompute_repost_groups(conn)
    refresh_channel_auto_labels(conn)
    return total


def refresh_channel_auto_labels(conn):
    """auto_relevant = ключ у назві/описі або ≥1 пост зі збігом; auto_local = ключ у назві/описі."""
    for row in conn.execute("SELECT id, title, about FROM channels").fetchall():
        kw_hit = bool(find_core(f"{row['title'] or ''}\n{row['about'] or ''}"))
        post_hit = conn.execute(
            "SELECT 1 FROM posts WHERE channel_id=? AND is_comment=0 AND matched_terms<>'' LIMIT 1",
            (row["id"],)).fetchone() is not None
        collected = db.get_sync_state(conn, row["id"]) is not None
        rel = 1 if (kw_hit or post_hit) else (0 if collected else None)
        conn.execute("UPDATE channels SET auto_relevant=?, auto_local=? WHERE id=?",
                     (rel, 1 if kw_hit else 0, row["id"]))
    conn.commit()


# ---------------------------------------------------------------- media (вручну)

async def download_media(pool, conn, post_key: str) -> str | None:
    """--download-media <channel_id:msg_id> — тільки для конкретного поста, вручну."""
    ch_id, msg_id = (int(x) for x in post_key.split(":"))
    row = conn.execute("SELECT username FROM channels WHERE id=?", (ch_id,)).fetchone()
    if not row:
        raise SystemExit(f"Канал {ch_id} не знайдено в БД")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    async def _dl(client):
        entity = await client.get_entity(row["username"])
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg or not msg.media:
            return None
        return await client.download_media(msg, file=str(MEDIA_DIR / f"{ch_id}_{msg_id}"))

    return await pool.run(_dl, label=f"media {post_key}")
