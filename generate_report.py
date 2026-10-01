"""
generate_report.py — Репорт ТОЛЬКО по НОВЫМ лидам Google Ads.

Логика:
  - Читает all_leads.csv (глобальный файл из анализатора)
  - Фильтрует лиды, которые уже попали в прошлые репорты (reported_lead_ids.txt)
  - Генерирует HTML только с новыми лидами
  - После генерации сохраняет их user_id в reported_lead_ids.txt

Файлы:
  all_leads.csv          — все найденные лиды (ведёт анализатор)
  reported_lead_ids.txt  — user_id уже включённых в прошлые репорты
  report_new_leads_ДАТА.html — выходной файл

Использование:
  python generate_report.py              — новые лиды с прошлого репорта
  python generate_report.py --all        — все лиды (игнорирует reported)
  python generate_report.py --preview    — только показывает сколько новых, не сохраняет
"""

import csv
import json
import os
import sys
import re
import glob
from datetime import datetime
from collections import Counter

# ===== ФАЙЛЫ =====
ALL_LEADS_FILE    = "all_leads.csv"
REPORTED_IDS_FILE = "reported_lead_ids.txt"


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_json_safe(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def safe(val) -> str:
    return "" if val is None else str(val).strip()


def load_reported_ids() -> set:
    if not os.path.exists(REPORTED_IDS_FILE):
        return set()
    with open(REPORTED_IDS_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_reported_ids(ids: set):
    existing = load_reported_ids()
    all_ids = existing | ids
    with open(REPORTED_IDS_FILE, "w", encoding="utf-8") as f:
        for uid in sorted(all_ids):
            f.write(uid + "\n")


def get_chat_info(source_folder: str) -> dict:
    """Загружает chat_info.json из папки чата."""
    ci_path = os.path.join(source_folder, "chat_info.json")
    return load_json_safe(ci_path)


def detect_geo(chat_info: dict, leads: list[dict]) -> str:
    title    = safe(chat_info.get("title", "")).lower()
    username = safe(chat_info.get("username", "")).lower()
    geo_hints = {
        "brasil": "Brazil / LATAM", "hotmart": "LATAM",
        "afiliado": "LATAM", "español": "LATAM (Spanish)",
        "indonesia": "Indonesia", "komunitas": "Indonesia",
        "india": "India", "hindi": "India",
        "arab": "MENA", "iran": "Iran",
        "titan": "CIS / Russian-speaking",
        "арбитраж": "CIS / Russian-speaking",
        "google ads": "CIS / Russian-speaking",
    }
    for hint, geo in geo_hints.items():
        if hint in title or hint in username:
            return geo
    contexts = " ".join([safe(u.get("context", "")) for u in leads[:10]]).lower()
    if any(w in contexts for w in ["реквизит", "платёж", "крипт", "бан", "аккаунт"]):
        return "CIS / Russian-speaking"
    return "Global"


# ===== HTML BUILDERS =====

CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Manrope:wght@400;600;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Manrope',sans-serif;background:#f7f7f7;color:#1a1a1a;padding:32px 48px;line-height:1.6;max-width:1100px;margin:0 auto}
h1{font-size:2rem;font-weight:800;margin-bottom:4px}
h2{font-size:1.25rem;font-weight:800;color:#111;margin:0}
h3{font-size:0.95rem;font-weight:700;color:#444;margin:18px 0 8px}
.subtitle{color:#666;font-size:0.9rem;margin-bottom:24px}
.badge-new{display:inline-block;background:#00a67d;color:#fff;font-size:0.7rem;font-weight:700;padding:2px 8px;border-radius:20px;letter-spacing:1px;text-transform:uppercase;margin-left:10px;vertical-align:middle}
.summary-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0 28px}
.card{background:#fff;border:2px solid #e0e0e0;border-radius:10px;padding:18px;text-align:center}
.card .num{font-size:2rem;font-weight:800;font-family:'JetBrains Mono',monospace;color:#111}
.card .lbl{font-size:0.7rem;color:#999;text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.card.accent{border-color:#00a67d;background:#f0fdf8}
.card.accent .num{color:#00a67d}
.toc{background:#fff;border:1px solid #ddd;border-radius:10px;padding:18px 24px;margin-bottom:28px}
.toc ul{list-style:none;columns:2;gap:24px}
.toc li{padding:3px 0;font-size:0.83rem}
.toc a{color:#00a67d;text-decoration:none;font-weight:600}
.toc a:hover{text-decoration:underline}
.source-block{background:#fff;border:1px solid #ddd;border-radius:12px;padding:24px 28px;margin-bottom:22px}
.source-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}
.source-meta{color:#888;font-size:0.82rem;margin-top:4px}
.leads-count{font-family:'JetBrains Mono',monospace;font-size:1.5rem;font-weight:800;color:#00a67d}
.leads-label{font-size:0.7rem;color:#999;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:0.82rem}
th{background:#f5f5f5;text-align:left;padding:8px 10px;font-weight:700;color:#333;border-bottom:2px solid #ddd}
td{padding:7px 10px;border-bottom:1px solid #eee;vertical-align:top}
tr:hover{background:#f9fff9}
.lead-row{background:#f0fdf8!important;border-left:3px solid #00a67d}
.conf{font-family:'JetBrains Mono',monospace;font-weight:700;text-align:center}
.conf.hot{color:#d32f2f} .conf.warm{color:#f57c00} .conf.cool{color:#1976d2}
.tg-link{color:#00a67d;font-weight:600}
.quote{font-size:0.78rem;color:#444;font-style:italic;max-width:380px}
.ctx{font-size:0.78rem;color:#888;max-width:380px}
.id-col{font-family:'JetBrains Mono',monospace;font-size:0.72rem;color:#aaa;text-align:center}
.empty-state{text-align:center;padding:60px 20px;color:#999;font-size:1.1rem}
.footer{text-align:center;color:#bbb;font-size:0.78rem;margin-top:36px;padding:16px}
"""


def build_lead_row(i: int, lead: dict) -> str:
    uid    = safe(lead.get("user_id"))
    uname  = safe(lead.get("username"))
    name   = safe(lead.get("name"))
    conf   = int(safe(lead.get("confidence")) or "0")
    quote  = safe(lead.get("exact_quote"))[:160]
    ctx    = safe(lead.get("context"))[:120]
    msg_id = safe(lead.get("evidence_msg_id"))
    folder = safe(lead.get("source_folder"))

    # TG ссылка
    if uname:
        user_cell = f'<a class="tg-link" href="https://t.me/{uname}" target="_blank">@{uname}</a>'
    else:
        user_cell = f'<span style="color:#aaa">{uid}</span>'
    if name:
        user_cell += f'<br><small style="color:#999">{name}</small>'

    # Confidence цвет
    conf_cls = "hot" if conf >= 90 else "warm" if conf >= 70 else "cool"

    # Цитата или контекст
    if quote and quote.lower() not in ("none", ""):
        content_cell = f'<div class="quote">«{quote}»</div>'
    elif ctx:
        content_cell = f'<div class="ctx">{ctx}</div>'
    else:
        content_cell = "—"

    # Источник
    source_name = folder.replace("tg_dump_", "").replace("_", " ")[:40] if folder else "—"

    return f"""
    <tr class="lead-row">
        <td>{i}</td>
        <td>{user_cell}</td>
        <td class="conf {conf_cls}">{conf}</td>
        <td class="id-col">{msg_id or '—'}</td>
        <td>{content_cell}</td>
        <td><small style="color:#aaa">{source_name}</small></td>
    </tr>"""


def build_source_section(source_folder: str, leads: list[dict]) -> str:
    chat_info  = get_chat_info(source_folder)
    title      = safe(chat_info.get("title")) or source_folder.replace("tg_dump_", "").replace("_", " ")
    username   = safe(chat_info.get("username"))
    chat_link  = f'<a href="https://t.me/{username}" target="_blank">@{username}</a> · ' if username else ""
    chat_type  = "Канал" if chat_info.get("broadcast") else "Группа" if chat_info else "Чат"
    geo        = detect_geo(chat_info, leads)

    rows = "".join(build_lead_row(i + 1, lead) for i, lead in enumerate(leads))
    anchor = re.sub(r'[^a-zA-Z0-9_]', '_', source_folder)

    return f"""
<div class="source-block" id="{anchor}">
    <div class="source-header">
        <div>
            <h2>{title}</h2>
            <div class="source-meta">{chat_link}{chat_type} · {geo}</div>
        </div>
        <div style="text-align:right">
            <div class="leads-count">{len(leads)}</div>
            <div class="leads-label">лидов</div>
        </div>
    </div>
    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>Пользователь</th>
                <th>Conf</th>
                <th>msg_id</th>
                <th>Цитата / контекст</th>
                <th>Источник</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
</div>"""


def generate_report(all_mode: bool = False, preview: bool = False) -> str | None:
    # Загружаем все лиды
    all_leads = load_csv(ALL_LEADS_FILE)
    if not all_leads:
        print(f"❌ Файл {ALL_LEADS_FILE} не найден или пуст.")
        print("   Сначала запусти анализатор: python interest_analysis_v4.py --all")
        return None

    # Фильтруем новые
    reported_ids = set() if all_mode else load_reported_ids()
    new_leads = [l for l in all_leads if safe(l.get("user_id")) not in reported_ids]

    print(f"Всего лидов в all_leads.csv: {len(all_leads)}")
    print(f"Уже в прошлых репортах:       {len(reported_ids)}")
    print(f"Новых лидов для репорта:       {len(new_leads)}")

    if not new_leads:
        print("\n✅ Новых лидов нет — репорт не нужен.")
        print("   (Используй --all чтобы сгенерировать полный репорт)")
        return None

    if preview:
        print("\nПревью новых лидов:")
        for lead in new_leads[:10]:
            uname = safe(lead.get("username")) or safe(lead.get("user_id"))
            conf  = safe(lead.get("confidence"))
            src   = safe(lead.get("source_folder", "")).replace("tg_dump_", "")[:30]
            print(f"  @{uname} | conf={conf} | {src}")
        if len(new_leads) > 10:
            print(f"  ... и ещё {len(new_leads) - 10}")
        return None

    # Группируем по источнику
    by_source: dict[str, list] = {}
    for lead in new_leads:
        src = safe(lead.get("source_folder")) or "unknown"
        by_source.setdefault(src, []).append(lead)

    # Сортируем источники по числу лидов
    sorted_sources = sorted(by_source.items(), key=lambda x: -len(x[1]))

    # TOC
    toc_items = []
    for src, leads in sorted_sources:
        chat_info = get_chat_info(src)
        title = safe(chat_info.get("title")) or src.replace("tg_dump_", "").replace("_", " ")
        anchor = re.sub(r'[^a-zA-Z0-9_]', '_', src)
        toc_items.append(f'<li><a href="#{anchor}">{title}</a> — <strong>{len(leads)} лидов</strong></li>')
    toc_html = "\n".join(toc_items)

    # Секции
    sections = "".join(build_source_section(src, leads) for src, leads in sorted_sources)

    # Статистика
    confs = [int(safe(l.get("confidence")) or "0") for l in new_leads]
    avg_conf = round(sum(confs) / len(confs), 1) if confs else 0
    hot_count = sum(1 for c in confs if c >= 90)

    mode_label = "Все лиды" if all_mode else "Новые лиды"
    date_str   = datetime.now().strftime("%d.%m.%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{mode_label} Google Ads — {datetime.now():%d.%m.%Y}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{mode_label} — Google Ads <span class="badge-new">{'ALL' if all_mode else 'NEW'}</span></h1>
<p class="subtitle">
  Сгенерировано: {date_str} ·
  {'Полный репорт (все лиды)' if all_mode else 'Только лиды, не попавшие в предыдущие репорты'}
</p>

<div class="summary-cards">
  <div class="card accent"><div class="num">{len(new_leads)}</div><div class="lbl">{'Всего' if all_mode else 'Новых'} лидов</div></div>
  <div class="card"><div class="num">{len(sorted_sources)}</div><div class="lbl">Источников</div></div>
  <div class="card"><div class="num">{hot_count}</div><div class="lbl">Горячих (≥90)</div></div>
  <div class="card"><div class="num">{avg_conf}</div><div class="lbl">Avg confidence</div></div>
</div>

<div class="toc">
  <h3>Источники</h3>
  <ul>{toc_html}</ul>
</div>

{sections}

<div class="footer">
  Телеграм-парсер + AI-анализатор (gemma3:12b / gpt-4o-mini) · {date_str}
</div>
</body>
</html>"""

    return html


if __name__ == "__main__":
    all_mode = "--all" in sys.argv
    preview  = "--preview" in sys.argv

    html = generate_report(all_mode=all_mode, preview=preview)

    if html is None:
        sys.exit(0)

    # Имя файла
    date_tag = datetime.now().strftime("%Y%m%d_%H%M")
    prefix   = "report_all_leads" if all_mode else "report_new_leads"
    out_file = f"{prefix}_{date_tag}.html"

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)

    # Сохраняем user_id в reported (только если не --all, чтобы не "засорить" историю)
    if not all_mode:
        all_leads_data = load_csv(ALL_LEADS_FILE)
        reported_ids   = load_reported_ids()
        new_ids = {
            safe(l.get("user_id"))
            for l in all_leads_data
            if safe(l.get("user_id")) not in reported_ids
        }
        save_reported_ids(new_ids)
        print(f"\n✅ Репорт сохранён: {out_file}")
        print(f"   Помечено как 'отрепорчено': {len(new_ids)} лидов")
        print(f"   Следующий запуск покажет только НОВЫХ после этого момента")
    else:
        print(f"\n✅ Полный репорт: {out_file}")
        print(f"   (режим --all: reported_lead_ids.txt НЕ обновлён)")

    # Открыть в браузере (Windows)
    try:
        os.startfile(out_file)
    except Exception:
        pass
