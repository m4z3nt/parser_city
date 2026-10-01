"""
interest_analysis_v4.py — Evidence-based lead detection (v2 — no-repeat + global dedup)

Что нового:
  - Пропускает папки, где уже есть all_users_analyzed.csv (кроме --rerun)
  - Пропускает пользователей, которые уже есть в all_users_analyzed.csv этой папки
  - Глобальная дедупликация лидов: один user_id = один лид в итоговом all_leads.csv
  - Собирает глобальный all_leads.csv без дублей

Использование:
  python interest_analysis_v4.py tg_dump_Google_Ads/
  python interest_analysis_v4.py --all
  python interest_analysis_v4.py --range 80-168
  python interest_analysis_v4.py --rerun        # перезапустить даже готовые
"""

import json
import os
import re
import sys
import glob
import csv
import pandas as pd
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ===== CONFIG =====
OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "gemma3:12b"

# Переключение на OpenAI: USE_OPENAI=true в .env или установить вручную
USE_OPENAI = os.getenv("USE_OPENAI", "false").lower() == "true"

if USE_OPENAI:
    from openai import OpenAI
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Файл для глобальной дедупликации лидов
GLOBAL_LEADS_FILE = "all_leads.csv"
GLOBAL_SEEN_FILE  = "seen_lead_ids.txt"   # user_id-шники уже найденных лидов


# ===== SYSTEM PROMPT =====
SYSTEM_PROMPT = """Ты — аналитик продаж. Твоя задача — определить, можно ли этому человеку ПРОДАТЬ аккаунты Google Ads.

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:

1. Человек является лидом ТОЛЬКО если он ЯВНО говорит о Google Ads, гугл адс, MCC, агентских аккаунтах Google, рекламных кабинетах Google Ads, фарме Google Ads, банах в Google Ads, апелляциях Google Ads.

2. Слова "бан", "кабинет", "реквизиты", "аккаунт", "лимит", "апелляция" БЕЗ привязки к Google Ads — НЕ являются доказательством. В финтех-чатах эти слова используются про банки, платежки, крипту.

3. "Ищу реквизиты" = ищет банковские реквизиты, НЕ Google Ads.
   "Ищу платежное решение" = ищет PSP/банк, НЕ Google Ads.
   "Нужен аккаунт" без слов Google/Ads = НЕ Google Ads.

4. Ты ОБЯЗАН процитировать ТОЧНОЕ сообщение пользователя, которое доказывает интерес к Google Ads. Если такого сообщения нет — verdict: no.

5. НЕ ДОДУМЫВАЙ. НЕ ИНТЕРПРЕТИРУЙ. Только прямые доказательства."""


def ask_llm(prompt: str) -> str:
    if USE_OPENAI:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
        )
        return response.choices[0].message.content
    else:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.0},
            },
            timeout=300,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


def build_prompt(user_label: str, numbered_messages: str) -> str:
    return f"""Ниже — все сообщения одного пользователя из Telegram-чата.

Определи: есть ли ПРЯМОЕ доказательство того, что этому человеку можно продать аккаунты Google Ads?

ПОМНИ: слова "бан", "кабинет", "реквизиты" обычно НЕ про Google Ads.

Пользователь: {user_label}

Сообщения:
{numbered_messages}

---

Ответь СТРОГО в формате:

verdict: <yes или no>
evidence_msg_id: <msg_id из квадратных скобок САМОГО РЕЛЕВАНТНОГО сообщения (даже если verdict=no), или none>
exact_quote: <точная цитата из САМОГО РЕЛЕВАНТНОГО сообщения (даже если verdict=no), или none>
context: <1 предложение — о чём говорил пользователь>
confidence: <0-100, где 100 = явно написал "ищу Google Ads аккаунты">
category: <одна из: google_ads | payments | crypto | services | chat | other>

Если нет прямого доказательства про Google Ads — ставь verdict: no."""


def safe_str(x) -> str:
    return "" if x is None else str(x).strip()


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_llm_result(text: str) -> dict:
    result = {}
    for key in ("verdict", "evidence_msg_id", "exact_quote", "context", "confidence", "category"):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        result[key] = m.group(1).strip() if m else ""
    return result


def validate_quote(quote: str, messages: list[str]) -> bool:
    if not quote or quote.lower() in ("none", ""):
        return False
    quote_clean = quote.lower().strip()
    for msg in messages:
        if quote_clean in msg.lower():
            return True
    # Partial match (>50% words match)
    words = [w for w in quote_clean.split() if len(w) > 3]
    if not words:
        return False
    hits = sum(1 for w in words if any(w in m.lower() for m in messages))
    return hits / len(words) >= 0.6


def format_user_label(user_id: str, user_map: dict) -> str:
    info = user_map.get(user_id, {})
    username = info.get("username", "")
    name     = info.get("name", "")
    parts    = [f"id={user_id}"]
    if username:
        parts.append(f"@{username}")
    if name:
        parts.append(name)
    return " | ".join(parts)


def build_user_map(data_dir: str) -> dict:
    user_map = {}
    for filename in ["participants.json", "recent_senders.json", "comment_users.json", "reaction_users.json"]:
        filepath = os.path.join(data_dir, filename)
        if not os.path.exists(filepath):
            continue
        try:
            data = load_json(filepath)
            for p in data:
                uid = safe_str(p.get("user_id"))
                if not uid or uid in user_map:
                    continue
                username   = safe_str(p.get("username"))
                first_name = safe_str(p.get("first_name"))
                last_name  = safe_str(p.get("last_name"))
                full_name  = " ".join([x for x in [first_name, last_name] if x]).strip()
                user_map[uid] = {"username": username, "name": full_name}
        except Exception:
            pass
    return user_map


# ===== GLOBAL DEDUP =====

def load_global_seen() -> set:
    """Загружает user_id лидов, которые уже были найдены ранее."""
    seen = set()
    if os.path.exists(GLOBAL_SEEN_FILE):
        with open(GLOBAL_SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                uid = line.strip()
                if uid:
                    seen.add(uid)
    return seen


def save_global_seen(seen: set):
    with open(GLOBAL_SEEN_FILE, "w", encoding="utf-8") as f:
        for uid in sorted(seen):
            f.write(uid + "\n")


def append_global_leads(new_leads: list[dict], global_seen: set, source_folder: str):
    """Добавляет новые уникальные лиды в all_leads.csv и обновляет seen."""
    if not new_leads:
        return 0

    added = 0
    fieldnames = [
        "user_id", "username", "name", "source_folder",
        "messages_count", "verdict", "confidence", "category",
        "exact_quote", "context", "evidence_msg_id",
    ]

    file_exists = os.path.exists(GLOBAL_LEADS_FILE)
    with open(GLOBAL_LEADS_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for lead in new_leads:
            uid = safe_str(lead.get("user_id"))
            if uid in global_seen:
                continue  # уже есть
            global_seen.add(uid)
            row = dict(lead)
            row["source_folder"] = source_folder
            writer.writerow(row)
            added += 1

    return added


# ===== FOLDER SELECTION =====

def select_folders() -> list[str]:
    args = sys.argv[1:]
    force_rerun = "--rerun" in args
    args = [a for a in args if a != "--rerun"]

    if not args:
        folders = sorted(glob.glob("tg_dump_*/"))
        # По умолчанию предлагаем только нераспарсенные / неполные
        return [f.rstrip("/") for f in folders]

    if args[0] == "--all":
        return sorted(f.rstrip("/") for f in glob.glob("tg_dump_*/"))

    if args[0].startswith("--range"):
        # --range 10-50
        val = args[0].split("=")[-1] if "=" in args[0] else (args[1] if len(args) > 1 else "")
        if "-" in val:
            a, b = val.split("-", 1)
            all_folders = sorted(glob.glob("tg_dump_*/"))
            return [f.rstrip("/") for f in all_folders[int(a):int(b)+1]]

    # Конкретная папка или несколько
    result = []
    for a in args:
        if os.path.isdir(a):
            result.append(a.rstrip("/"))
        else:
            result.extend(f.rstrip("/") for f in glob.glob(f"{a}*/"))
    return result


# ===== ANALYZE FOLDER =====

def load_already_analyzed(data_dir: str) -> set:
    """Возвращает множество user_id, которые уже есть в all_users_analyzed.csv."""
    path = os.path.join(data_dir, "all_users_analyzed.csv")
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        if "user_id" in df.columns:
            return set(df["user_id"].dropna().astype(str).tolist())
    except Exception:
        pass
    return set()


def analyze_folder(data_dir: str, global_seen: set, force: bool = False) -> dict:
    print(f"\n  Папка: {data_dir}")

    # Проверяем наличие messages.json
    messages_path = os.path.join(data_dir, "messages.json")
    comments_path = os.path.join(data_dir, "comments.json")

    if not os.path.exists(messages_path):
        print(f"  ⏭  Нет messages.json — пропускаю")
        return {"folder": data_dir, "skipped": True, "reason": "no messages.json"}

    # Уже полностью проанализировано?
    analyzed_path = os.path.join(data_dir, "all_users_analyzed.csv")
    already_analyzed_ids = set()
    if os.path.exists(analyzed_path) and not force:
        # Проверяем: если файл содержит данные — частично или полностью готово
        already_analyzed_ids = load_already_analyzed(data_dir)
        if already_analyzed_ids:
            print(f"  ♻  Частично готово ({len(already_analyzed_ids)} юзеров). Продолжаю с непроанализированных...")
        # Если leads тоже есть — грузим их для global_seen
        leads_path = os.path.join(data_dir, "google_ads_leads_verified.csv")
        if os.path.exists(leads_path):
            try:
                df_leads = pd.read_csv(leads_path, encoding="utf-8-sig", dtype=str)
                for uid in df_leads.get("user_id", pd.Series()).dropna():
                    global_seen.add(str(uid))
            except Exception:
                pass

    user_map = build_user_map(data_dir)
    results: list[dict] = []
    google_ads_leads: list[dict] = []

    # Загружаем уже сохранённые результаты (чтобы не потерять)
    if already_analyzed_ids and os.path.exists(analyzed_path):
        try:
            existing_df = pd.read_csv(analyzed_path, encoding="utf-8-sig", dtype=str)
            results = existing_df.to_dict("records")
            google_ads_leads_path = os.path.join(data_dir, "google_ads_leads_verified.csv")
            if os.path.exists(google_ads_leads_path):
                existing_leads_df = pd.read_csv(google_ads_leads_path, encoding="utf-8-sig", dtype=str)
                google_ads_leads = existing_leads_df.to_dict("records")
        except Exception:
            results = []
            google_ads_leads = []

    # Загружаем сообщения
    try:
        messages_data = load_json(messages_path)
        if isinstance(messages_data, dict) and "messages" in messages_data:
            messages_data = messages_data["messages"]
    except Exception as e:
        print(f"  ❌ Ошибка чтения messages.json: {e}")
        return {"folder": data_dir, "error": str(e)}

    comments_data = []
    if os.path.exists(comments_path):
        try:
            comments_data = load_json(comments_path)
            if isinstance(comments_data, dict) and "messages" in comments_data:
                comments_data = comments_data["messages"]
        except Exception:
            pass

    # Собираем сообщения по user_id
    rows = []
    for msg in messages_data:
        uid  = safe_str(msg.get("from_id") or msg.get("user_id") or "")
        text = safe_str(msg.get("text") or msg.get("message") or "")
        mid  = safe_str(msg.get("message_id") or msg.get("id") or "")
        if uid and text.strip():
            rows.append({"user_id": uid, "message_id": mid, "text": text})

    for msg in comments_data:
        uid  = safe_str(msg.get("from_id") or msg.get("user_id") or "")
        text = safe_str(msg.get("text") or msg.get("message") or "")
        mid  = safe_str(msg.get("message_id") or msg.get("id") or "")
        if uid and text.strip():
            rows.append({"user_id": uid, "message_id": mid, "text": text})

    if not rows:
        print(f"  Нет текстовых сообщений")
    else:
        df = pd.DataFrame(rows)
        grouped = df.groupby("user_id").apply(
            lambda g: list(zip(g["message_id"].tolist(), g["text"].tolist())),
            include_groups=False,
        ).reset_index(name="msg_pairs")

        # Фильтруем тех, кто уже проанализирован
        new_users = grouped[~grouped["user_id"].isin(already_analyzed_ids)]
        print(f"  Сообщений: {len(df)}, юзеров: {len(grouped)}, новых: {len(new_users)}")

        for idx, row in new_users.iterrows():
            user_id   = row["user_id"]
            msg_pairs = row["msg_pairs"]
            messages_list = [text.strip() for _, text in msg_pairs if safe_str(text).strip()]

            if not messages_list:
                continue

            numbered = "\n".join(
                [f"[msg_id={mid}] {text.strip()}" for mid, text in msg_pairs if safe_str(text).strip()]
            )
            numbered = numbered[:6000]

            user_label = format_user_label(user_id, user_map)
            prompt     = build_prompt(user_label, numbered)

            if (idx + 1) % 20 == 0:
                print(f"  [{datetime.now():%H:%M:%S}] Обработано +{idx+1}...")

            try:
                llm_raw = ask_llm(prompt)
                parsed  = parse_llm_result(llm_raw)
            except Exception as e:
                parsed = {
                    "verdict": "error", "evidence_msg_id": "", "exact_quote": "",
                    "context": f"Ошибка: {e}", "confidence": "0", "category": "error",
                }
                llm_raw = str(e)

            quote       = parsed.get("exact_quote", "")
            quote_valid = validate_quote(quote, messages_list)

            claimed_msg_id = parsed.get("evidence_msg_id", "").strip()
            real_msg_ids   = {str(mid) for mid, _ in msg_pairs}
            msg_id_valid   = claimed_msg_id in real_msg_ids

            verdict = parsed.get("verdict", "").lower().strip()
            if verdict == "yes" and not quote_valid:
                parsed["verdict"] = "no (hallucination)"
                parsed["confidence"] = "0"

            conf_str    = safe_str(parsed.get("confidence", "0"))
            conf_match  = re.search(r"\d+", conf_str)
            confidence_num = int(conf_match.group()) if conf_match else 0

            record = {
                "user_id":         user_id,
                "username":        user_map.get(user_id, {}).get("username", ""),
                "name":            user_map.get(user_id, {}).get("name", ""),
                "messages_count":  len(messages_list),
                "verdict":         parsed.get("verdict", ""),
                "confidence":      confidence_num,
                "category":        parsed.get("category", ""),
                "exact_quote":     quote if (quote_valid or quote.lower() != "none") else "",
                "quote_validated": "yes" if quote_valid else "no",
                "context":         parsed.get("context", ""),
                "evidence_msg_id": claimed_msg_id if msg_id_valid else "",
                "msg_id_validated":"yes" if msg_id_valid else "no",
                "raw_response":    llm_raw,
            }
            results.append(record)

            if parsed.get("verdict", "").lower() == "yes" and quote_valid and confidence_num >= 50:
                google_ads_leads.append(record)

    # Reaction users (без текста)
    analyzed_user_ids = {r["user_id"] for r in results}
    reaction_count = 0
    ru_path = os.path.join(data_dir, "reaction_users.json")
    if os.path.exists(ru_path):
        try:
            reaction_data = load_json(ru_path)
            for ru in reaction_data:
                uid = safe_str(ru.get("user_id"))
                if not uid or uid in analyzed_user_ids:
                    continue
                reactions = ru.get("reactions", [])
                if isinstance(reactions, list) and reactions:
                    reactions_str = "; ".join(
                        [f"{r.get('emoji','')} msg={r.get('msg_id','')} [{r.get('post_context','')[:60]}]"
                         for r in reactions[:5]]
                    )
                else:
                    reactions_str = safe_str(ru.get("reacted_to_msg_id", ""))

                results.append({
                    "user_id":         uid,
                    "username":        safe_str(ru.get("username")),
                    "name":            " ".join([safe_str(ru.get("first_name")), safe_str(ru.get("last_name"))]).strip(),
                    "messages_count":  0,
                    "verdict":         "no",
                    "confidence":      0,
                    "category":        "reaction_only",
                    "exact_quote":     "",
                    "quote_validated": "no",
                    "context":         reactions_str,
                    "evidence_msg_id": "",
                    "msg_id_validated":"no",
                    "raw_response":    "",
                })
                analyzed_user_ids.add(uid)
                reaction_count += 1
        except Exception:
            pass

    if reaction_count > 0:
        print(f"  + {reaction_count} юзеров из реакций")

    # Сохраняем результаты папки
    out_df = pd.DataFrame(results)
    out_df.to_csv(os.path.join(data_dir, "all_users_analyzed.csv"),
                  index=False, encoding="utf-8-sig")

    leads_df = pd.DataFrame(google_ads_leads)
    if not leads_df.empty:
        leads_df = leads_df.sort_values(by="confidence", ascending=False)
    leads_df.to_csv(os.path.join(data_dir, "google_ads_leads_verified.csv"),
                    index=False, encoding="utf-8-sig")

    # Добавляем новые лиды в глобальный файл (без дублей)
    new_leads_for_global = [r for r in google_ads_leads if safe_str(r.get("user_id")) not in global_seen]
    added_global = append_global_leads(new_leads_for_global, global_seen, data_dir)

    cats = out_df["category"].value_counts(dropna=False).to_dict() if not out_df.empty else {}
    print(f"  [{datetime.now():%H:%M:%S}] ГОТОВО:")
    print(f"    Всего юзеров: {len(out_df) - reaction_count} анализ + {reaction_count} реакции")
    print(f"    Google Ads лидов в папке: {len(leads_df)}")
    print(f"    Новых в глобальный all_leads.csv: {added_global}")
    print(f"    Категории: {cats}")
    if not leads_df.empty:
        for _, lead in leads_df.head(3).iterrows():
            print(f"      @{lead.get('username') or lead.get('user_id')} | conf={lead.get('confidence')}")

    return {
        "folder": data_dir,
        "users":  len(out_df),
        "leads":  len(leads_df),
        "new_global_leads": added_global,
        "categories": cats,
    }


# ===== MAIN =====
if __name__ == "__main__":
    force_rerun = "--rerun" in sys.argv
    folders = select_folders()

    if not folders:
        print("Нечего анализировать.")
        sys.exit(0)

    print(f"\nПапок для анализа: {len(folders)}")
    print(f"Режим: {'ПОЛНЫЙ ПЕРЕЗАПУСК' if force_rerun else 'только новые/частичные'}")
    print(f"Для остановки: Ctrl+C (прогресс сохраняется)\n")

    # Загружаем глобальный список уже известных лидов
    global_seen = load_global_seen()
    print(f"Уже известных лидов (глобально): {len(global_seen)}\n")

    all_stats = []

    for i, folder in enumerate(folders):
        print(f"\n{'='*60}")
        print(f"  [{i+1}/{len(folders)}] {folder}")
        print(f"{'='*60}")

        try:
            stats = analyze_folder(folder, global_seen, force=force_rerun)
            all_stats.append(stats)
            # Сохраняем seen после каждой папки (на случай прерывания)
            save_global_seen(global_seen)
        except KeyboardInterrupt:
            print(f"\n\n⚠ Прервано! Прогресс сохранён.")
            save_global_seen(global_seen)
            break
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
            all_stats.append({"folder": folder, "error": str(e)})

    save_global_seen(global_seen)

    # Итоговый отчёт
    ok      = [s for s in all_stats if "error" not in s and not s.get("skipped")]
    skipped = [s for s in all_stats if s.get("skipped")]
    errors  = [s for s in all_stats if "error" in s]
    total_leads     = sum(s.get("leads", 0) for s in ok)
    total_new_global= sum(s.get("new_global_leads", 0) for s in ok)

    print(f"\n{'='*60}")
    print(f"  ИТОГО: ✓ {len(ok)} обработано, ⏭ {len(skipped)} пропущено, ❌ {len(errors)} ошибок")
    print(f"  Лидов найдено: {total_leads} (уникальных новых в all_leads.csv: {total_new_global})")
    print(f"  Всего уникальных лидов в all_leads.csv: {len(global_seen)}")
    print(f"{'='*60}")
    for s in ok:
        print(f"  ✓ {s['folder']} — {s['users']} юзеров, {s['leads']} лидов GA (+{s.get('new_global_leads',0)} глоб.)")
    for s in errors:
        print(f"  ❌ {s['folder']}: {s['error']}")
