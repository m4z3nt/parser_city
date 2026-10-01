"""
main.py — Telegram chat parser (v2 — dual API + no-repeat)

Поддержка 2 Telegram API аккаунтов для обхода лимитов.
Чаты, которые уже распарсены (есть messages.json), пропускаются.

Настройка в .env:
  TG_API_ID_1=...
  TG_API_HASH_1=...
  TG_SESSION_1=my_session_1   (опционально, по умолчанию session_1)

  TG_API_ID_2=...
  TG_API_HASH_2=...
  TG_SESSION_2=my_session_2   (опционально, по умолчанию session_2)

Использование:
  python main.py                      — ищет targets.csv
  python main.py links.csv            — читает линки из файла
  python main.py https://t.me/chat    — парсит один чат
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError
from telethon.tl.functions.messages import GetMessageReactionsListRequest
from telethon.tl.types import Channel, Chat, User, PeerChannel, PeerChat, PeerUser

load_dotenv()

# =========================
# НАСТРОЙКИ — из .env
# =========================
ACCOUNTS = []
for i in (1, 2):
    api_id   = os.getenv(f"TG_API_ID_{i}")
    api_hash = os.getenv(f"TG_API_HASH_{i}")
    session  = os.getenv(f"TG_SESSION_{i}", f"session_{i}")
    if api_id and api_hash:
        ACCOUNTS.append({"api_id": int(api_id), "api_hash": api_hash, "session": session})

# Fallback: старые переменные / хардкод
if not ACCOUNTS:
    ACCOUNTS = [
        {"api_id": int(os.getenv("TG_API_ID", "33667515")),
         "api_hash": os.getenv("TG_API_HASH", "8ebcfd3fef65327abe7e8cfc0921d902"),
         "session": os.getenv("TG_SESSION", "my_session")},
    ]

message_limit      = 1000
participants_limit = 10000
dialogs_limit      = 500

# Задержки (секунды)
DELAY_BETWEEN_CHATS  = 5    # между чатами
DELAY_BETWEEN_BATCH  = 1    # каждые 20 запросов внутри одного чата
MIN_FLOOD_SLEEP      = 10   # минимум при FloodWait

# =========================


def load_targets() -> list[str]:
    source = sys.argv[1] if len(sys.argv) > 1 else "targets.csv"
    if source.startswith("https://") or source.startswith("@") or source.startswith("-"):
        return [source]
    if not os.path.exists(source):
        print(f"❌ Файл не найден: {source}")
        sys.exit(1)
    targets = []
    with open(source, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip().strip(",").strip('"').strip("'")
            if line and not line.startswith("#"):
                targets.append(line)
    print(f"Загружено таргетов: {len(targets)}")
    return targets


def sanitize_folder_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'[\s,]+', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    if len(name) > 80:
        name = name[:80].rstrip('_')
    return name or "unknown_chat"


def ensure_dir(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_text(value) -> str:
    return "" if value is None else str(value)


def write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_csv(path: str, rows: list[dict], fieldnames: list[str]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def get_chat_title(entity) -> str:
    if hasattr(entity, "title") and entity.title:
        return entity.title
    if hasattr(entity, "first_name"):
        parts = [entity.first_name or "", entity.last_name or ""]
        return " ".join(p for p in parts if p).strip() or str(entity.id)
    return str(entity.id)


async def resolve_entity(client, target_value):
    return await client.get_entity(target_value)


# ============================================================
# EXPORT FUNCTIONS (same logic as original, cleaned up)
# ============================================================

async def export_chat_info(client, entity, out_dir: str) -> dict:
    info = {
        "id": entity.id,
        "type": type(entity).__name__,
        "title": get_chat_title(entity),
        "username": getattr(entity, "username", None),
        "participants_count": getattr(entity, "participants_count", None),
        "scam": getattr(entity, "scam", False),
        "fake": getattr(entity, "fake", False),
        "verified": getattr(entity, "verified", False),
        "broadcast": getattr(entity, "broadcast", False),
        "megagroup": getattr(entity, "megagroup", False),
    }
    write_json(f"{out_dir}/chat_info.json", info)
    return info


async def export_dialogs_snapshot(client, out_dir: str) -> dict:
    rows = []
    async for dialog in client.iter_dialogs(limit=dialogs_limit):
        e = dialog.entity
        rows.append({
            "id": e.id,
            "type": type(e).__name__,
            "title": get_chat_title(e),
            "username": getattr(e, "username", None),
            "unread_count": dialog.unread_count,
            "message_date": safe_text(dialog.date),
        })
    write_json(f"{out_dir}/dialogs.json", rows)
    if rows:
        write_csv(f"{out_dir}/dialogs.csv", rows, list(rows[0].keys()))
    return {"dialogs_exported": len(rows)}


async def export_messages(client, entity, out_dir: str) -> dict:
    messages, comments = [], []
    links, usernames_set = [], set()
    url_re = re.compile(r"https?://\S+")
    username_re = re.compile(r"@([A-Za-z0-9_]{3,})")
    count = 0

    async for msg in client.iter_messages(entity, limit=message_limit):
        text = safe_text(msg.text or msg.message or "")
        row = {
            "message_id": msg.id,
            "date": safe_text(msg.date),
            "from_id": safe_text(getattr(msg.from_id, "user_id", None) or
                                  getattr(msg.from_id, "channel_id", None) or
                                  getattr(msg.from_id, "chat_id", None)),
            "from": safe_text(msg.sender.first_name if msg.sender and hasattr(msg.sender, "first_name") else
                               msg.sender.title if msg.sender and hasattr(msg.sender, "title") else ""),
            "text": text,
            "reply_to_msg_id": safe_text(msg.reply_to_msg_id),
            "views": safe_text(getattr(msg, "views", None)),
            "forwards": safe_text(getattr(msg, "forwards", None)),
            "replies": safe_text(getattr(getattr(msg, "replies", None), "replies", None)),
        }
        messages.append(row)

        for url in url_re.findall(text):
            links.append({"message_id": msg.id, "url": url, "date": safe_text(msg.date)})
        for uname in username_re.findall(text):
            usernames_set.add(uname)

        # Comments (replies)
        if getattr(msg, "replies", None) and msg.replies and msg.replies.replies:
            try:
                async for reply in client.iter_messages(entity, reply_to=msg.id, limit=200):
                    rtext = safe_text(reply.text or reply.message or "")
                    rid = getattr(reply.from_id, "user_id", None)
                    comments.append({
                        "message_id": reply.id,
                        "reply_to_msg_id": msg.id,
                        "date": safe_text(reply.date),
                        "from_id": safe_text(rid),
                        "from": safe_text(reply.sender.first_name if reply.sender and hasattr(reply.sender, "first_name") else ""),
                        "text": rtext,
                    })
            except Exception:
                pass

        count += 1
        if count % 200 == 0:
            await asyncio.sleep(0.5)

    write_json(f"{out_dir}/messages.json", messages)
    if messages:
        write_csv(f"{out_dir}/messages.csv", messages, list(messages[0].keys()))

    write_json(f"{out_dir}/comments.json", comments)
    if comments:
        write_csv(f"{out_dir}/comments.csv", comments, list(comments[0].keys()))

    write_json(f"{out_dir}/links_found.json", links)
    write_json(f"{out_dir}/usernames_found.json", list(usernames_set))

    return {"messages_exported": len(messages), "comments_exported": len(comments)}


async def export_recent_senders(client, entity, out_dir: str) -> dict:
    seen, rows = set(), []
    async for msg in client.iter_messages(entity, limit=500):
        if msg.from_id is None:
            continue
        uid = getattr(msg.from_id, "user_id", None)
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        sender = msg.sender
        rows.append({
            "user_id": uid,
            "username": safe_text(getattr(sender, "username", None)),
            "first_name": safe_text(getattr(sender, "first_name", None)),
            "last_name": safe_text(getattr(sender, "last_name", None)),
            "phone": safe_text(getattr(sender, "phone", None)),
            "bot": getattr(sender, "bot", False),
            "scam": getattr(sender, "scam", False),
        })
        if len(rows) % 50 == 0:
            await asyncio.sleep(0.3)

    write_json(f"{out_dir}/recent_senders.json", rows)
    if rows:
        write_csv(f"{out_dir}/recent_senders.csv", rows, list(rows[0].keys()))
    return {"recent_senders_exported": len(rows)}


async def export_participants(client, entity, out_dir: str) -> dict:
    rows = []
    err = None
    try:
        async for p in client.iter_participants(entity, limit=participants_limit):
            rows.append({
                "user_id": p.id,
                "username": safe_text(getattr(p, "username", None)),
                "first_name": safe_text(getattr(p, "first_name", None)),
                "last_name": safe_text(getattr(p, "last_name", None)),
                "phone": safe_text(getattr(p, "phone", None)),
                "bot": getattr(p, "bot", False),
                "scam": getattr(p, "scam", False),
                "premium": getattr(p, "premium", False),
            })
            if len(rows) % 100 == 0:
                await asyncio.sleep(0.3)
    except Exception as e:
        err = str(e)

    write_json(f"{out_dir}/participants.json", rows)
    if rows:
        write_csv(f"{out_dir}/participants.csv", rows, list(rows[0].keys()))
    return {"participants_exported": len(rows), "participants_error": err}


async def export_admins(client, entity, out_dir: str) -> dict:
    rows = []
    err = None
    try:
        async for p in client.iter_participants(entity, filter=None, limit=200):
            if hasattr(p, "participant") and hasattr(p.participant, "admin_rights"):
                rows.append({
                    "user_id": p.id,
                    "username": safe_text(getattr(p, "username", None)),
                    "first_name": safe_text(getattr(p, "first_name", None)),
                    "last_name": safe_text(getattr(p, "last_name", None)),
                })
    except Exception as e:
        err = str(e)

    write_json(f"{out_dir}/admins.json", rows)
    if rows:
        write_csv(f"{out_dir}/admins.csv", rows, list(rows[0].keys()))
    return {"admins_exported": len(rows), "admins_error": err}


async def export_reaction_users(client, entity, out_dir: str) -> dict:
    rows, seen = [], set()
    total_reactions = 0
    errors = 0
    try:
        async for msg in client.iter_messages(entity, limit=message_limit):
            if not getattr(msg, "reactions", None):
                continue
            if not msg.reactions or not msg.reactions.results:
                continue
            try:
                offset = ""
                while True:
                    r = await client(GetMessageReactionsListRequest(
                        peer=entity, id=msg.id, reaction=None,
                        offset=offset, limit=100,
                    ))
                    if not r.reactions:
                        break
                    for reaction in r.reactions:
                        u = reaction.peer_id
                        uid = getattr(u, "user_id", None)
                        if uid is None:
                            continue
                        total_reactions += 1
                        emoji = ""
                        if hasattr(reaction, "reaction"):
                            emoji = getattr(reaction.reaction, "emoticon", "")
                        if uid not in seen:
                            seen.add(uid)
                            rows.append({
                                "user_id": uid,
                                "username": "",
                                "first_name": "",
                                "last_name": "",
                                "reactions": [{"emoji": emoji, "msg_id": msg.id,
                                               "post_context": safe_text(msg.text or "")[:100]}],
                            })
                        else:
                            for existing in rows:
                                if existing["user_id"] == uid:
                                    existing["reactions"].append(
                                        {"emoji": emoji, "msg_id": msg.id,
                                         "post_context": safe_text(msg.text or "")[:100]}
                                    )
                                    break
                    if not r.next_offset:
                        break
                    offset = r.next_offset
                    await asyncio.sleep(0.3)
            except FloodWaitError as e:
                wait = max(e.seconds, MIN_FLOOD_SLEEP)
                print(f"    ⏳ FloodWait {wait}с (реакции)...")
                await asyncio.sleep(wait)
            except Exception:
                errors += 1
            if len(rows) % 20 == 0 and rows:
                await asyncio.sleep(0.5)
    except Exception:
        pass

    write_json(f"{out_dir}/reaction_users.json", rows)
    if rows:
        flat = [{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in r.items()} for r in rows]
        write_csv(f"{out_dir}/reaction_users.csv", flat, list(flat[0].keys()))

    return {"reaction_users_exported": len(rows), "total_reactions": total_reactions, "errors": errors}


async def export_comment_users(client, entity, out_dir: str) -> dict:
    rows, seen = [], set()
    errors = 0
    comments_path = f"{out_dir}/comments.json"
    if not os.path.exists(comments_path):
        write_json(f"{out_dir}/comment_users.json", [])
        return {"comment_users_exported": 0}

    try:
        comments = load_json(comments_path)
    except Exception:
        write_json(f"{out_dir}/comment_users.json", [])
        return {"comment_users_exported": 0}

    user_ids = list({str(c.get("from_id", "")) for c in comments if c.get("from_id")})

    for i, uid_str in enumerate(user_ids):
        if not uid_str or uid_str in seen:
            continue
        try:
            uid = int(uid_str)
            user = await client.get_entity(uid)
            seen.add(uid_str)
            rows.append({
                "user_id": uid,
                "username": safe_text(getattr(user, "username", None)),
                "first_name": safe_text(getattr(user, "first_name", None)),
                "last_name": safe_text(getattr(user, "last_name", None)),
                "phone": safe_text(getattr(user, "phone", None)),
                "bot": getattr(user, "bot", False),
                "scam": getattr(user, "scam", False),
            })
        except FloodWaitError as e:
            wait = max(e.seconds, MIN_FLOOD_SLEEP)
            print(f"    ⏳ FloodWait {wait}с (comment_users)...")
            await asyncio.sleep(wait)
        except Exception:
            errors += 1

        if (i + 1) % 20 == 0:
            await asyncio.sleep(1)

    write_json(f"{out_dir}/comment_users.json", rows)
    if rows:
        write_csv(f"{out_dir}/comment_users.csv", rows, list(rows[0].keys()))
    return {"comment_users_exported": len(rows), "errors": errors}


# ============================================================
# MAIN PROCESS LOGIC
# ============================================================

async def process_target(client, target_value, dialogs_exported: bool) -> dict:
    print(f"\n{'='*60}")
    print(f"  Таргет: {target_value}")

    try:
        entity = await resolve_entity(client, target_value)
    except (ChannelPrivateError, UsernameNotOccupiedError) as e:
        print(f"  ❌ Недоступен: {e}")
        return {"target": str(target_value), "error": str(e)}

    chat_title = get_chat_title(entity)
    folder_name = f"tg_dump_{sanitize_folder_name(chat_title)}"
    print(f"  Чат: {chat_title}  →  {folder_name}/")

    # === Уже спарсено — пропускаем или дообогащаем ===
    if os.path.exists(f"{folder_name}/messages.json"):
        need_comment_users = not os.path.exists(f"{folder_name}/comment_users.json")
        need_reactions = True
        ru_path = f"{folder_name}/reaction_users.json"
        if os.path.exists(ru_path):
            try:
                ru_data = load_json(ru_path)
                if not ru_data or (isinstance(ru_data, list) and ru_data and "reactions" in ru_data[0]):
                    need_reactions = False
            except Exception:
                pass

        if not need_reactions and not need_comment_users:
            print(f"  ⏭  Уже готово — пропускаю")
            return {"target": str(target_value), "chat_title": chat_title,
                    "folder": folder_name, "skipped": True}

        print(f"  ♻  Дообогащение...")
        summary = {"target": str(target_value), "chat_title": chat_title,
                   "folder": folder_name, "enriched": True}
        if need_reactions:
            r = await export_reaction_users(client, entity, folder_name)
            summary["reaction_users"] = r
            print(f"  ✓ reaction_users ({r.get('reaction_users_exported', 0)} юзеров)")
        if need_comment_users:
            cu = await export_comment_users(client, entity, folder_name)
            summary["comment_users"] = cu
            print(f"  ✓ comment_users ({cu.get('comment_users_exported', 0)} юзеров)")
        return summary

    # === Новый чат ===
    ensure_dir(folder_name)
    summary = {"target": str(target_value), "chat_title": chat_title, "folder": folder_name}

    await export_chat_info(client, entity, folder_name)
    print(f"  ✓ chat_info.json")

    if not dialogs_exported:
        d = await export_dialogs_snapshot(client, folder_name)
        summary["dialogs"] = d
        print(f"  ✓ dialogs ({d['dialogs_exported']} шт)")

    msg = await export_messages(client, entity, folder_name)
    summary["messages"] = msg
    print(f"  ✓ messages ({msg['messages_exported']}), comments ({msg['comments_exported']})")

    rs = await export_recent_senders(client, entity, folder_name)
    summary["recent_senders"] = rs
    print(f"  ✓ recent_senders ({rs['recent_senders_exported']})")

    p = await export_participants(client, entity, folder_name)
    summary["participants"] = p
    if p["participants_error"]:
        print(f"  ⚠ participants: {p['participants_error']}")
    else:
        print(f"  ✓ participants ({p['participants_exported']})")

    a = await export_admins(client, entity, folder_name)
    summary["admins"] = a

    r = await export_reaction_users(client, entity, folder_name)
    summary["reaction_users"] = r
    print(f"  ✓ reaction_users ({r.get('reaction_users_exported', 0)})")

    cu = await export_comment_users(client, entity, folder_name)
    summary["comment_users"] = cu
    print(f"  ✓ comment_users ({cu.get('comment_users_exported', 0)})")

    write_json(f"{folder_name}/summary.json", summary)
    return summary


# ============================================================
# DUAL-ACCOUNT RUNNER
# ============================================================

async def run_with_account(account: dict, targets: list[str],
                           results: list, dialogs_done: list):
    """Запускает парсинг списка таргетов на одном аккаунте."""
    print(f"\n🔑 Аккаунт: {account['session']}")
    async with TelegramClient(account["session"], account["api_id"], account["api_hash"]) as client:
        me = await client.get_me()
        print(f"   Логин: @{me.username or me.id} ({me.first_name})")

        dialogs_exported = len(dialogs_done) > 0

        for i, target in enumerate(targets):
            print(f"\n  [{account['session']}] [{i+1}/{len(targets)}]", end="")
            try:
                summary = await process_target(client, target, dialogs_exported)
                results.append(summary)
                if not summary.get("skipped") and not summary.get("enriched") and not summary.get("error"):
                    dialogs_done.append(True)
                    dialogs_exported = True
                if i < len(targets) - 1 and not summary.get("skipped"):
                    pause = 3 if summary.get("enriched") else DELAY_BETWEEN_CHATS
                    print(f"  ⏳ Пауза {pause}с...")
                    await asyncio.sleep(pause)
            except FloodWaitError as e:
                wait = max(e.seconds + 3, MIN_FLOOD_SLEEP)
                print(f"\n  ⏳ FloodWait {wait}с — жду...")
                await asyncio.sleep(wait)
                try:
                    summary = await process_target(client, target, dialogs_exported)
                    results.append(summary)
                except Exception as e2:
                    print(f"\n  ❌ Повтор ошибка {target}: {e2}")
                    results.append({"target": str(target), "error": str(e2)})
            except Exception as e:
                print(f"\n  ❌ Ошибка {target}: {e}")
                results.append({"target": str(target), "error": str(e)})


async def main():
    targets = load_targets()
    if not targets:
        print("❌ Список таргетов пустой!")
        return

    print(f"Аккаунтов: {len(ACCOUNTS)}, таргетов: {len(targets)}")

    results = []
    dialogs_done = []

    if len(ACCOUNTS) == 1:
        # Один аккаунт — всё последовательно
        await run_with_account(ACCOUNTS[0], targets, results, dialogs_done)
    else:
        # Два аккаунта — делим таргеты пополам (или round-robin)
        half = len(targets) // 2
        targets_1 = targets[:half or 1]
        targets_2 = targets[half or 1:]

        print(f"  Аккаунт 1: {len(targets_1)} чатов")
        print(f"  Аккаунт 2: {len(targets_2)} чатов")

        # Запускаем параллельно
        await asyncio.gather(
            run_with_account(ACCOUNTS[0], targets_1, results, dialogs_done),
            run_with_account(ACCOUNTS[1], targets_2, results, dialogs_done),
        )

    # Итог
    ok       = [s for s in results if "error" not in s and not s.get("skipped") and not s.get("enriched")]
    enriched = [s for s in results if s.get("enriched")]
    skipped  = [s for s in results if s.get("skipped")]
    fail     = [s for s in results if "error" in s]

    print(f"\n{'='*60}")
    print(f"  ✓ {len(ok)} новых  ♻ {len(enriched)} дообогащено  ⏭ {len(skipped)} пропущено  ❌ {len(fail)} ошибок")
    print(f"{'='*60}")
    for s in ok:
        msgs = s.get("messages", {}).get("messages_exported", 0)
        print(f"  ✓ {s['chat_title']} — {msgs} сообщений → {s['folder']}/")
    for s in fail:
        print(f"  ❌ {s['target']}: {s['error']}")


if __name__ == "__main__":
    asyncio.run(main())
