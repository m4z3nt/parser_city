"""
tg_search_channels.py — Автопоиск Telegram каналов/групп по ключевикам.

Использование:
  python tg_search_channels.py                    # дефолтные ключевики
  python tg_search_channels.py "google ads" "mcc" # свои ключевики

Результат: targets_auto.csv — список живых каналов для парсинга.
Уже спарсенные папки (tg_dump_*) автоматически исключаются.
"""

import asyncio
import csv
import os
import sys
import glob
import json
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat
from telethon.errors import FloodWaitError

# === Настройки ===
API_ID = 33667515
API_HASH = "015c4b0a519a296a489515512e57a2c1"
SESSION = "my_session"

DEFAULT_KEYWORDS = [
    # Google Ads прямые
    "google ads",
    "google adwords",
    "гугл адс",
    "гугл эдс",
    "google ads account",
    "google ads аккаунт",
    "MCC google",
    "агентский кабинет google",

    # PPC / арбитраж
    "PPC chat",
    "PPC маркетинг",
    "арбитраж трафика",
    "арбітраж трафіку",
    "media buying",
    "медиабаинг",

    # Антидетект / фарм
    "антидетект браузер",
    "antidetect browser",
    "adspower",
    "dolphin anty",
    "gologin",
    "multilogin",

    # Affiliate / CPA
    "affiliate marketing",
    "CPA сеть",
    "CPA network",
    "партнерская программа",
    "партнерка",

    # Ads general
    "facebook ads",
    "tiktok ads",
    "контекстная реклама",

    # Прокси / инструменты
    "proxy арбитраж",
    "residential proxy",
    "mobile proxy",

    # Аккаунты
    "аккаунты google",
    "buy ads account",
    "sell ads account",
    "фарм аккаунтов",
    "agency account",

    # Клоакинг
    "cloaking",
    "клоакинг",

    # Вертикали
    "gambling traffic",
    "dating traffic",
    "nutra traffic",
    "sweepstakes",
    "ecommerce ads",
    "google shopping",
]

MIN_PARTICIPANTS = 50


async def search_channels(client, keywords):
    """Поиск каналов/групп по ключевикам через Telegram API."""
    found = {}

    for i, keyword in enumerate(keywords):
        print(f"  [{i+1}/{len(keywords)}] \"{keyword}\"", end="", flush=True)

        try:
            result = await client(SearchRequest(
                q=keyword,
                limit=100,
            ))

            count = 0
            for chat in result.chats:
                if not isinstance(chat, (Channel, Chat)):
                    continue
                if chat.id in found:
                    continue

                participants = getattr(chat, 'participants_count', 0) or 0
                if participants < MIN_PARTICIPANTS:
                    continue

                username = getattr(chat, 'username', '') or ''
                if not username:
                    continue

                title = getattr(chat, 'title', '') or ''
                is_broadcast = getattr(chat, 'broadcast', False)
                is_megagroup = getattr(chat, 'megagroup', False)
                chat_type = "channel" if is_broadcast else "megagroup" if is_megagroup else "group"

                found[chat.id] = {
                    "id": chat.id,
                    "username": username,
                    "title": title,
                    "type": chat_type,
                    "participants": participants,
                    "link": f"https://t.me/{username}",
                }
                count += 1

            print(f" → {count} новых")

        except FloodWaitError as e:
            print(f" ⏳ FloodWait {e.seconds}с...")
            await asyncio.sleep(e.seconds + 5)
            try:
                result = await client(SearchRequest(q=keyword, limit=100))
                count = 0
                for chat in result.chats:
                    if not isinstance(chat, (Channel, Chat)):
                        continue
                    if chat.id in found:
                        continue
                    participants = getattr(chat, 'participants_count', 0) or 0
                    if participants < MIN_PARTICIPANTS:
                        continue
                    username = getattr(chat, 'username', '') or ''
                    if not username:
                        continue
                    title = getattr(chat, 'title', '') or ''
                    is_broadcast = getattr(chat, 'broadcast', False)
                    is_megagroup = getattr(chat, 'megagroup', False)
                    chat_type = "channel" if is_broadcast else "megagroup" if is_megagroup else "group"
                    found[chat.id] = {
                        "id": chat.id,
                        "username": username,
                        "title": title,
                        "type": chat_type,
                        "participants": participants,
                        "link": f"https://t.me/{username}",
                    }
                    count += 1
                print(f" → {count} новых")
            except Exception as e2:
                print(f" ❌ {e2}")

        except Exception as e:
            print(f" ❌ {e}")

        await asyncio.sleep(3)

    return found


def get_already_parsed():
    """Возвращает set юзернеймов уже спарсенных чатов."""
    parsed = set()
    for folder in glob.glob("tg_dump_*"):
        if not os.path.isdir(folder):
            continue
        ci_path = os.path.join(folder, "chat_info.json")
        if os.path.exists(ci_path):
            try:
                with open(ci_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                uname = info.get("username", "")
                if uname:
                    parsed.add(uname.lower())
            except Exception:
                pass
    return parsed


async def main():
    keywords = DEFAULT_KEYWORDS

    custom = [a for a in sys.argv[1:] if not a.startswith("--")]
    if custom:
        keywords = custom

    print(f"╔{'═'*52}╗")
    print(f"║  Telegram Channel Search — Google Ads Leads        ║")
    print(f"╚{'═'*52}╝")
    print(f"  Ключевиков: {len(keywords)}")
    print(f"  Мин. участников: {MIN_PARTICIPANTS}")

    already = get_already_parsed()
    print(f"  Уже спарсено: {len(already)} чатов")

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"  Логин: @{me.username} ({me.first_name})\n")

        print(f"🔍 Поиск по {len(keywords)} ключевикам...")
        found = await search_channels(client, keywords)

    print(f"\n  Найдено всего: {len(found)} каналов/групп")

    new_channels = {k: v for k, v in found.items() if v["username"].lower() not in already}
    skipped = len(found) - len(new_channels)

    print(f"  Уже спарсено (пропуск): {skipped}")
    print(f"  Новых для парсинга: {len(new_channels)}")

    if not new_channels:
        print("\n  Нет новых каналов. Попробуй другие ключевики.")
        return

    sorted_channels = sorted(new_channels.values(), key=lambda x: -x["participants"])

    # CSV для парсинга (только ссылки)
    out_csv = "targets_auto.csv"
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        for ch in sorted_channels:
            f.write(f"{ch['link']}\n")

    # Полный отчет
    out_full = "channels_found.csv"
    with open(out_full, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["link", "title", "username", "type", "participants", "id"])
        writer.writeheader()
        for ch in sorted_channels:
            writer.writerow(ch)

    print(f"\n✅ {out_csv} — {len(sorted_channels)} ссылок для парсинга")
    print(f"✅ {out_full} — полный список с деталями")

    print(f"\n  Топ-20 по участникам:")
    for i, ch in enumerate(sorted_channels[:20]):
        t = "📢" if ch["type"] == "channel" else "👥"
        print(f"    {i+1}. {t} {ch['title'][:40]} — @{ch['username']} ({ch['participants']:,} уч.)")

    print(f"\n  Дальше:")
    print(f"    python pipeline.py targets_auto.csv")


if __name__ == "__main__":
    asyncio.run(main())
