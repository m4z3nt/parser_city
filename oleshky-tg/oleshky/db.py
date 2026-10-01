"""SQLite: схема, міграції, upsert-и. Дати зберігаються в UTC (ISO 8601)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Кожен елемент — одна міграція; user_version = кількість застосованих.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE channels (
        id               INTEGER PRIMARY KEY,
        source_platform  TEXT NOT NULL DEFAULT 'telegram',
        username         TEXT COLLATE NOCASE,
        title            TEXT,
        type             TEXT,              -- channel | megagroup
        participants     INTEGER,
        about            TEXT,
        linked_chat_id   INTEGER,
        found_via        TEXT,              -- через кому: seed,search,hashtag,recommendation,snowball
        depth            INTEGER NOT NULL DEFAULT 0,
        auto_relevant    INTEGER,           -- NULL = ще невідомо
        auto_local       INTEGER,
        auto_side        TEXT,
        inaccessible     INTEGER NOT NULL DEFAULT 0,
        first_seen_at    TEXT NOT NULL,
        updated_at       TEXT,
        full_fetched_at  TEXT
    );
    CREATE UNIQUE INDEX ix_channels_username ON channels(username);

    CREATE TABLE posts (
        source_platform  TEXT NOT NULL DEFAULT 'telegram',
        channel_id       INTEGER NOT NULL,
        msg_id           INTEGER NOT NULL,
        date             TEXT NOT NULL,
        edit_date        TEXT,
        text             TEXT,
        views            INTEGER,
        forwards         INTEGER,
        replies_count    INTEGER,
        media_type       TEXT,
        grouped_id       INTEGER,
        album_msg_ids    TEXT,
        fwd_from_peer    TEXT,              -- channel:<id> | user (id автора не зберігаємо)
        fwd_from_msg_id  INTEGER,
        link             TEXT,
        matched_terms    TEXT,
        matched_by       TEXT,              -- search|hashtag|local_channel|snowball (через кому)
        text_hash        TEXT,
        repost_group     TEXT,              -- ключ першоджерела кластера: <channel_id>:<msg_id>
        sender_kind      TEXT,              -- channel | user | anonymous
        sender_hash      TEXT,
        is_comment       INTEGER NOT NULL DEFAULT 0,
        parent_channel_id INTEGER,
        parent_msg_id    INTEGER,
        relevance_rule   TEXT,              -- yes | unsure | no
        collected_at     TEXT NOT NULL,
        updated_at       TEXT,
        PRIMARY KEY (source_platform, channel_id, msg_id)
    );
    CREATE INDEX ix_posts_hash ON posts(text_hash);
    CREATE INDEX ix_posts_group ON posts(repost_group);
    CREATE INDEX ix_posts_date ON posts(date);
    CREATE INDEX ix_posts_album ON posts(channel_id, grouped_id);

    CREATE TABLE classifications (
        text_hash        TEXT PRIMARY KEY,
        llm              TEXT,
        model            TEXT,
        relevant         TEXT,              -- true | false | unsure
        type             TEXT,
        blockade_related INTEGER,
        side             TEXT,
        summary_uk       TEXT,
        evidence         TEXT,
        evidence_ok      INTEGER,
        raw              TEXT,
        created_at       TEXT NOT NULL
    );

    CREATE TABLE sync_state (
        channel_id   INTEGER PRIMARY KEY,
        last_msg_id  INTEGER NOT NULL,
        last_run_at  TEXT NOT NULL,
        mode         TEXT
    );

    CREATE TABLE reported_posts (
        post_key     TEXT PRIMARY KEY,      -- <channel_id>:<msg_id>
        report_date  TEXT NOT NULL
    );

    CREATE TABLE reported_channels (
        channel_id   INTEGER PRIMARY KEY,
        report_date  TEXT NOT NULL
    );

    -- кеш резолву username → щоб не витрачати ліміт ResolveUsername повторно.
    -- Ключ — sha256(username), сам username приватних акаунтів не зберігаємо.
    CREATE TABLE resolve_cache (
        key          TEXT PRIMARY KEY,
        kind         TEXT NOT NULL,         -- channel | not_public | not_found | error
        resolved_at  TEXT NOT NULL
    );
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_iso(dt) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.commit()


def post_key(channel_id: int, msg_id: int) -> str:
    return f"{channel_id}:{msg_id}"


def _merge_csv(a: str | None, b: str | None) -> str:
    out: list[str] = []
    for part in (a or "").split(",") + (b or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return ",".join(out)


# ---------------------------------------------------------------- channels

def upsert_channel(conn, ch: dict) -> bool:
    """ch: id, username, title, type, participants, found_via, depth. Повертає True, якщо новий."""
    row = conn.execute("SELECT * FROM channels WHERE id=?", (ch["id"],)).fetchone()
    now = utcnow()
    if row is None:
        # username міг перейти до іншого id — звільняємо
        conn.execute("UPDATE channels SET username=NULL WHERE username=? AND id<>?",
                     (ch["username"], ch["id"]))
        conn.execute(
            """INSERT INTO channels (id, username, title, type, participants, found_via, depth,
                                     first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ch["id"], ch["username"], ch.get("title"), ch.get("type"), ch.get("participants"),
             ch.get("found_via"), ch.get("depth", 0), now, now))
        return True
    conn.execute(
        """UPDATE channels SET username=?, title=COALESCE(?, title), type=COALESCE(?, type),
                  participants=COALESCE(?, participants), found_via=?, depth=MIN(depth, ?),
                  inaccessible=0, updated_at=?
           WHERE id=?""",
        (ch["username"], ch.get("title"), ch.get("type"), ch.get("participants"),
         _merge_csv(row["found_via"], ch.get("found_via")), ch.get("depth", 0), now, ch["id"]))
    return False


def get_channels(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM channels WHERE username IS NOT NULL ORDER BY id").fetchall()


def effective_channel(row, labels: dict) -> dict:
    """Ручні мітки з channel_labels.csv перекривають автоматичні."""
    d = dict(row)
    lab = labels.get((d.get("username") or "").lower(), {})
    auto_rel, auto_loc = d.get("auto_relevant"), d.get("auto_local")
    d["relevant"] = lab["relevant"] if lab.get("relevant") is not None else (
        None if auto_rel is None else bool(auto_rel))
    d["local"] = lab["local"] if lab.get("local") is not None else bool(auto_loc)
    d["side"] = lab.get("side") or d.get("auto_side") or "unknown"
    d["label_note"] = lab.get("note", "")
    return d


def public_usernames(conn) -> set[str]:
    return {r[0].lower() for r in conn.execute("SELECT username FROM channels WHERE username IS NOT NULL")}


# ---------------------------------------------------------------- posts

POST_FIELDS = ("channel_id", "msg_id", "date", "edit_date", "text", "views", "forwards",
               "replies_count", "media_type", "grouped_id", "album_msg_ids", "fwd_from_peer",
               "fwd_from_msg_id", "link", "matched_terms", "matched_by", "text_hash",
               "sender_kind", "sender_hash", "is_comment", "parent_channel_id", "parent_msg_id")


def upsert_post(conn, rec: dict) -> str:
    """Вставляє/оновлює пост. Повертає 'new' | 'updated' | 'same'.

    Альбом (той самий grouped_id у каналі) зливається в уже наявний запис.
    Відредагований пост (новіша edit_date) — оновлюється текст і скидається класифікація.
    """
    platform = rec.get("source_platform", "telegram")
    existing = conn.execute(
        "SELECT * FROM posts WHERE source_platform=? AND channel_id=? AND msg_id=?",
        (platform, rec["channel_id"], rec["msg_id"])).fetchone()
    if existing is None and rec.get("grouped_id"):
        existing = conn.execute(
            "SELECT * FROM posts WHERE source_platform=? AND channel_id=? AND grouped_id=?",
            (platform, rec["channel_id"], rec["grouped_id"])).fetchone()
    now = utcnow()

    if existing is None:
        cols = POST_FIELDS + ("source_platform", "collected_at", "updated_at")
        vals = [rec.get(c) for c in POST_FIELDS] + [platform, now, now]
        conn.execute(f"INSERT INTO posts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return "new"

    status = "same"
    upd = {
        "views": rec.get("views") if rec.get("views") is not None else existing["views"],
        "forwards": rec.get("forwards") if rec.get("forwards") is not None else existing["forwards"],
        "replies_count": rec.get("replies_count") if rec.get("replies_count") is not None else existing["replies_count"],
        "matched_by": _merge_csv(existing["matched_by"], rec.get("matched_by")),
        "matched_terms": _merge_csv(existing["matched_terms"], rec.get("matched_terms")),
        "album_msg_ids": _merge_csv(existing["album_msg_ids"], rec.get("album_msg_ids")),
    }
    new_edit, old_edit = rec.get("edit_date"), existing["edit_date"]
    text_changed = (rec.get("text") or "") != (existing["text"] or "")
    newer = bool(new_edit) and (not old_edit or new_edit > old_edit)
    # альбом: новий шматок підпису з іншого елемента альбому
    album_merge = rec["msg_id"] != existing["msg_id"] and rec.get("text") and \
        rec["text"] not in (existing["text"] or "")
    if album_merge:
        upd["text"] = ((existing["text"] or "") + "\n\n" + rec["text"]).strip()
    elif text_changed and newer and rec["msg_id"] == existing["msg_id"]:
        upd["text"] = rec.get("text")
        upd["edit_date"] = new_edit
    if "text" in upd:
        from .filters import text_hash
        upd["text_hash"] = text_hash(upd["text"])
        upd["relevance_rule"] = None
        status = "updated"
    if upd["matched_by"] != (existing["matched_by"] or "") or upd["matched_terms"] != (existing["matched_terms"] or ""):
        upd["relevance_rule"] = None
        status = "updated" if status == "same" else status
    upd["updated_at"] = now
    sets = ",".join(f"{k}=?" for k in upd)
    conn.execute(f"UPDATE posts SET {sets} WHERE source_platform=? AND channel_id=? AND msg_id=?",
                 list(upd.values()) + [platform, existing["channel_id"], existing["msg_id"]])
    return status


# ---------------------------------------------------------------- sync state

def get_sync_state(conn, channel_id: int):
    return conn.execute("SELECT * FROM sync_state WHERE channel_id=?", (channel_id,)).fetchone()


def set_sync_state(conn, channel_id: int, last_msg_id: int, mode: str):
    conn.execute(
        """INSERT INTO sync_state (channel_id, last_msg_id, last_run_at, mode) VALUES (?,?,?,?)
           ON CONFLICT(channel_id) DO UPDATE SET last_msg_id=MAX(last_msg_id, excluded.last_msg_id),
                  last_run_at=excluded.last_run_at, mode=excluded.mode""",
        (channel_id, last_msg_id, utcnow(), mode))


# ---------------------------------------------------------------- repost clusters

def recompute_repost_groups(conn) -> int:
    """Кластери однакових постів у різних каналах: за fwd_from, інакше за text_hash.

    repost_group = ключ найранішого поста кластера (першоджерело).
    """
    rows = conn.execute(
        "SELECT channel_id, msg_id, date, fwd_from_peer, fwd_from_msg_id, text_hash, text "
        "FROM posts WHERE source_platform='telegram'").fetchall()
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    from .filters import normalize_text
    dates = {}
    for r in rows:
        k = post_key(r["channel_id"], r["msg_id"])
        dates[k] = r["date"]
        find(k)
        peer = r["fwd_from_peer"] or ""
        if peer.startswith("channel:") and r["fwd_from_msg_id"]:
            union(k, post_key(int(peer.split(":", 1)[1]), r["fwd_from_msg_id"]))
        # короткі тексти («Олешки», «#олешки») не кластеризуємо — забагато хибних збігів
        if r["text_hash"] and len(normalize_text(r["text"] or "")) >= 40:
            union(k, "h:" + r["text_hash"])

    groups: dict[str, list[str]] = {}
    for k in dates:
        groups.setdefault(find(k), []).append(k)
    changed = 0
    for members in groups.values():
        head = min(members, key=lambda k: (dates[k], k))
        for k in members:
            ch, mid = k.split(":")
            cur = conn.execute(
                "UPDATE posts SET repost_group=? WHERE channel_id=? AND msg_id=? "
                "AND (repost_group IS NULL OR repost_group<>?)", (head, int(ch), int(mid), head))
            changed += cur.rowcount
    conn.commit()
    return changed
