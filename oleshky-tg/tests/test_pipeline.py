"""Конвеєр без Telegram: фейкові повідомлення → БД → правила → звіт."""
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl import types

from oleshky import db
from oleshky.classify import apply_rules, normalize_result, validate_quote
from oleshky.collect import merge_album, message_to_record
from oleshky.config import load_keywords
from oleshky.report import run_report

KW = load_keywords()
SALT = "test-salt-0123456789"
CHAN = {"id": 100, "username": "oleshky_news", "type": "channel"}
GROUP = {"id": 200, "username": "kherson_chat", "type": "megagroup"}


def msg(mid, text, *, day=10, from_id=None, grouped_id=None, fwd=None, views=10, post=True, edit=None):
    return SimpleNamespace(
        id=mid, message=text, date=datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc), edit_date=edit,
        views=views, forwards=1, replies=None, media=None, grouped_id=grouped_id, fwd_from=fwd,
        from_id=from_id, post=post)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    for ch, typ in ((CHAN, "channel"), (GROUP, "megagroup")):
        db.upsert_channel(c, {**ch, "title": ch["username"], "type": typ, "found_via": "seed"})
    c.commit()
    return c


def test_record_has_no_personal_ids():
    m = msg(5, "В Олешках немає води", from_id=types.PeerUser(user_id=987654321), post=False)
    rec = message_to_record(m, GROUP, SALT, KW, "search")
    assert rec["sender_kind"] == "user"
    assert rec["sender_hash"] and len(rec["sender_hash"]) == 16
    assert "987654321" not in repr(rec)
    assert rec["link"] == "https://t.me/kherson_chat/5"
    assert rec["matched_terms"] == "олешках"


def test_channel_post_sender_is_channel():
    rec = message_to_record(msg(1, "Олешки"), CHAN, SALT, KW, "search")
    assert rec["sender_kind"] == "channel" and rec["sender_hash"] is None


def test_forward_from_user_is_not_stored():
    fwd = SimpleNamespace(from_id=types.PeerUser(user_id=42), channel_post=None)
    rec = message_to_record(msg(1, "Олешки", fwd=fwd), CHAN, SALT, KW, "search")
    assert rec["fwd_from_peer"] == "user" and rec["fwd_from_msg_id"] is None


def test_album_merge():
    recs = [message_to_record(msg(10, "", grouped_id=7), CHAN, SALT, KW, "search"),
            message_to_record(msg(11, "Олешки: фото з міста", grouped_id=7), CHAN, SALT, KW, "search"),
            message_to_record(msg(12, "", grouped_id=7), CHAN, SALT, KW, "search")]
    merged = merge_album(recs)
    assert len(merged) == 1
    assert merged[0]["msg_id"] == 10 and merged[0]["album_msg_ids"] == "10,11,12"
    assert "Олешки" in merged[0]["text"] and merged[0]["media_type"] == "album"


def test_repeat_collect_no_duplicates(conn):
    recs = [message_to_record(msg(i, f"В Олешках новина {i}"), CHAN, SALT, KW, "search") for i in range(1, 6)]
    assert [db.upsert_post(conn, r) for r in recs] == ["new"] * 5
    assert [db.upsert_post(conn, r) for r in recs] == ["same"] * 5
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 5


def test_edit_updates_text(conn):
    db.upsert_post(conn, message_to_record(msg(1, "В Олешках немає води"), CHAN, SALT, KW, "search"))
    edited = msg(1, "В Олешках немає води і світла", edit=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert db.upsert_post(conn, message_to_record(edited, CHAN, SALT, KW, "search")) == "updated"
    assert conn.execute("SELECT text FROM posts").fetchone()[0].endswith("світла")


def test_repost_groups(conn):
    long = "В Олешках третій день немає води, люди просять допомоги з гуманітаркою"
    db.upsert_post(conn, message_to_record(msg(1, long, day=1), CHAN, SALT, KW, "search"))
    fwd = SimpleNamespace(from_id=types.PeerChannel(channel_id=100), channel_post=1)
    db.upsert_post(conn, message_to_record(msg(50, long, day=2, fwd=fwd, post=False,
                                               from_id=types.PeerUser(user_id=1)), GROUP, SALT, KW, "search"))
    db.upsert_post(conn, message_to_record(msg(60, long + "!!! 😢", day=3, post=False,
                                               from_id=types.PeerUser(user_id=2)), GROUP, SALT, KW, "search"))
    db.recompute_repost_groups(conn)
    groups = {r[0] for r in conn.execute("SELECT repost_group FROM posts")}
    assert groups == {"100:1"}


def test_validate_quote():
    text = "В Олешках третій день немає води, ціни на хліб зросли вдвічі"
    assert validate_quote("третій день немає води", text)
    assert not validate_quote("у Херсоні обстріл", text)
    res = normalize_result({"relevant": True, "type": "humanitarian", "evidence": "вигадана цитата якої нема"}, text)
    assert res["relevant"] == "unsure"


def test_report_offline(conn, tmp_path):
    posts = [
        (1, "В Олешках третій день без води. Телефон волонтерів +380 67 123 45 67"),
        (2, "Подарунок для Алешки"),
        (3, "Ситуация в Алешках"),
        (4, "Олешківська громада повідомляє про блокаду. Пишіть @private_user"),
    ]
    for mid, text in posts:
        db.upsert_post(conn, message_to_record(msg(mid, text, day=mid), CHAN, SALT, KW, "search"))
    conn.execute("""INSERT INTO classifications (text_hash, llm, model, relevant, type, blockade_related, side,
                    summary_uk, evidence, evidence_ok, created_at)
                    SELECT text_hash,'ollama','m','true','appeal',1,'ua','Звернення про воду','без води',1,'x'
                    FROM posts WHERE msg_id=1""")
    conn.commit()
    counts = apply_rules(conn, KW, {})
    assert counts == {"yes": 2, "unsure": 1, "no": 1}

    out = run_report(conn, {}, out_dir=tmp_path / "exp")
    html = (out / "report.html").read_text(encoding="utf-8")
    assert not re.search(r"<(script|link)\b", html, re.I)
    assert "@import" not in html and "src=\"http" not in html
    assert "+380 67 123 45 67" not in html and "@private_user" not in html
    assert "https://t.me/oleshky_news/1" in html
    csv_text = (out / "posts.csv").read_text(encoding="utf-8-sig")
    assert "+380" not in csv_text and "Подарунок" not in csv_text
    assert (out / "posts.xlsx").exists() and (out / "channels.csv").exists()
    md = list((out / "notebooklm").glob("*.md"))
    assert md and "## 2026-09-01" in md[0].read_text(encoding="utf-8")

    # --since-last: друге звітування без нових постів — порожнє
    out2 = run_report(conn, {}, since_last=True, out_dir=tmp_path / "exp2")
    lines = (out2 / "posts.csv").read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 1
