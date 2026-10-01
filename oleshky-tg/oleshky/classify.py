"""Етап 3 — правила + LLM (локальний Ollama за замовчуванням)."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from . import config, db
from .filters import mask_pii, relevance_rule

log = logging.getLogger("oleshky.classify")

TYPES = ("appeal", "testimony", "humanitarian", "evacuation", "shelling",
         "occupation_admin", "propaganda", "news", "other")
SIDES = ("ua", "occupation", "neutral", "unknown")
MAX_TEXT = 4000
BATCH_COMMIT = 20

SYSTEM_PROMPT = """Ти — аналітик волонтерської інформаційної кампанії про окуповане місто Олешки
(Херсонська область, Україна; рос. «Алешки»). Тема кампанії — блокада Олешок у 2026 році.
Ти класифікуєш пости з публічних Telegram-каналів. Відповідай ЛИШЕ валідним JSON без пояснень.

Важливо: «Алешка/Алешки» — ще й зменшувальне від імені Олексій. Якщо пост про людину, а не про місто, —
relevant=false. Якщо не впевнений — relevant="unsure". Не вигадуй: усе має випливати з тексту."""

USER_TEMPLATE = """Канал: {channel}
Пост ({date}):
\"\"\"
{text}
\"\"\"

Поверни JSON рівно з такими полями:
{{
  "relevant": true | false | "unsure",   // чи пост про місто Олешки (а не про людину Алешка)
  "type": "appeal | testimony | humanitarian | evacuation | shelling | occupation_admin | propaganda | news | other",
  "blockade_related": true | false,      // чи стосується блокади / ізоляції міста
  "side": "ua | occupation | neutral | unknown",  // з чиєї позиції написано пост
  "summary_uk": "одне речення українською",
  "evidence": "дослівна цитата з тексту посту, до 20 слів"
}}

Типи:
- appeal — звернення / прохання про допомогу від мешканців чи родичів;
- testimony — свідчення з місця подій;
- humanitarian — гуманітарна ситуація: блокада, ціни, продукти, ліки, вода, світло, зв'язок;
- evacuation — виїзд, евакуація, коридори, фільтрація;
- shelling — обстріли, руйнування, жертви;
- occupation_admin — повідомлення окупаційної адміністрації;
- propaganda — окупаційна пропаганда / дезінформація;
- news — нейтральна новина;
- other — інше."""

CHANNEL_SIDE_TEMPLATE = """Визнач позицію Telegram-каналу щодо війни Росії проти України.
Назва: {title}
Опис: {about}
Останні пости:
{posts}

Поверни JSON: {{"side": "ua | occupation | neutral | unknown"}}
ua — український / проукраїнський; occupation — окупаційний / проросійський; neutral — нейтральний;
unknown — неможливо визначити."""


# ---------------------------------------------------------------- LLM

class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, kind: str):
        self.kind = kind
        if kind == "openai":
            from openai import OpenAI  # noqa: імпорт лише за прапорцем
            self.model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        else:
            self.model = config.ollama_model()
            self.url = config.ollama_url()

    @property
    def masks_input(self) -> bool:
        """У хмару тексти йдуть лише замасковані."""
        return self.kind == "openai"

    def check(self):
        if self.kind != "ollama":
            return
        base = self.url.rsplit("/api/", 1)[0]
        try:
            tags = requests.get(base + "/api/tags", timeout=10).json()
        except requests.RequestException as e:
            raise LLMError(f"Ollama недоступна за {base}: {e}\n   Запусти: ollama serve") from e
        names = {m.get("name") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise LLMError(f"Модель {self.model} не завантажена. Виконай: ollama pull {self.model}")

    def ask(self, system: str, user: str) -> str:
        if self.kind == "openai":
            r = self.client.chat.completions.create(
                model=self.model, temperature=0.0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            return r.choices[0].message.content
        for attempt in range(3):
            try:
                r = requests.post(self.url, timeout=300, json={
                    "model": self.model, "stream": False, "format": "json",
                    "options": {"temperature": 0.0},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                })
                r.raise_for_status()
                return r.json()["message"]["content"]
            except requests.RequestException as e:
                if attempt == 2:
                    raise LLMError(str(e)) from e
                time.sleep(5 * (attempt + 1))
        raise LLMError("unreachable")


# ---------------------------------------------------------------- parsing / validation

def parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "так")


def validate_quote(quote: str, text: str) -> bool:
    """Як у interest_analysis_v4: точний збіг або ≥60% значущих слів цитати є в тексті."""
    if not quote or quote.strip().lower() in ("none", "null", "-"):
        return False
    norm = lambda s: re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip(" \"'«»")
    q, t = norm(quote), norm(text)
    if q in t:
        return True
    words = [w.strip(".,!?:;\"'«»()") for w in q.split()]
    words = [w for w in words if len(w) > 3]
    if not words:
        return False
    return sum(1 for w in words if w in t) / len(words) >= 0.6


def normalize_result(raw: dict, text: str) -> dict:
    rel = raw.get("relevant")
    if isinstance(rel, str) and rel.strip().lower() == "unsure":
        relevant = "unsure"
    elif rel is None:
        relevant = "unsure"
    else:
        relevant = "true" if _to_bool(rel) else "false"
    typ = str(raw.get("type") or "other").strip().lower()
    side = str(raw.get("side") or "unknown").strip().lower()
    evidence = str(raw.get("evidence") or "").strip()
    ok = validate_quote(evidence, text)
    if relevant == "true" and not ok:
        relevant = "unsure"         # цитати немає в тексті → не довіряємо
    return {
        "relevant": relevant,
        "type": typ if typ in TYPES else "other",
        "blockade_related": 1 if _to_bool(raw.get("blockade_related", False)) else 0,
        "side": side if side in SIDES else "unknown",
        "summary_uk": str(raw.get("summary_uk") or "").strip()[:500],
        "evidence": evidence[:400],
        "evidence_ok": 1 if ok else 0,
    }


# ---------------------------------------------------------------- stage

def apply_rules(conn, kw, labels, rerun: bool = False) -> dict:
    chans = {r["id"]: db.effective_channel(r, labels) for r in conn.execute("SELECT * FROM channels")}
    where = "" if rerun else "WHERE relevance_rule IS NULL"
    rows = conn.execute(f"SELECT channel_id, msg_id, text, is_comment, parent_channel_id, matched_by "
                        f"FROM posts {where}").fetchall()
    counts = {"yes": 0, "unsure": 0, "no": 0}
    for r in rows:
        ch = chans.get(r["parent_channel_id"] if r["is_comment"] else r["channel_id"], {})
        local = bool(ch.get("local")) and "local_channel" in (r["matched_by"] or "")
        if r["is_comment"]:
            local = False
        rule = relevance_rule(r["text"] or "", kw, channel_relevant=ch.get("relevant"), channel_local=local)
        counts[rule] += 1
        conn.execute("UPDATE posts SET relevance_rule=? WHERE channel_id=? AND msg_id=?",
                     (rule, r["channel_id"], r["msg_id"]))
    conn.commit()
    return counts


def classify_posts(conn, llm: LLM, labels, rerun: bool = False, limit: int | None = None) -> dict:
    cached = "" if rerun else "AND p.text_hash NOT IN (SELECT text_hash FROM classifications)"
    rows = conn.execute(f"""
        SELECT p.text_hash, MIN(p.date) AS date, p.text, c.username, c.title
        FROM posts p LEFT JOIN channels c ON c.id = COALESCE(p.parent_channel_id, p.channel_id)
        WHERE p.relevance_rule IN ('yes','unsure') AND p.text <> '' {cached}
        GROUP BY p.text_hash ORDER BY date DESC""").fetchall()
    if limit:
        rows = rows[:limit]
    public = db.public_usernames(conn)
    log.info("LLM (%s/%s): %d унікальних текстів", llm.kind, llm.model, len(rows))
    stats = {"done": 0, "errors": 0}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        text = (r["text"] or "")[:MAX_TEXT]
        if llm.masks_input:
            text = mask_pii(text, public)
        prompt = USER_TEMPLATE.format(channel=f"@{r['username']} — {r['title']}" if r["username"] else "?",
                                      date=(r["date"] or "")[:10], text=text)
        try:
            answer = llm.ask(SYSTEM_PROMPT, prompt)
        except LLMError as e:
            log.error("LLM помилка: %s", e)
            stats["errors"] += 1
            if stats["errors"] >= 5 and stats["done"] == 0:
                raise
            continue
        res = normalize_result(parse_json(answer), text)
        conn.execute(
            """INSERT OR REPLACE INTO classifications (text_hash, llm, model, relevant, type, blockade_related,
                   side, summary_uk, evidence, evidence_ok, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["text_hash"], llm.kind, llm.model, res["relevant"], res["type"], res["blockade_related"],
             res["side"], res["summary_uk"], res["evidence"], res["evidence_ok"], answer[:4000], db.utcnow()))
        stats["done"] += 1
        if i % BATCH_COMMIT == 0:
            conn.commit()
            rate = (time.time() - t0) / i
            log.info("  %d/%d (≈%.1f с/пост, лишилось ≈%d хв)", i, len(rows), rate, rate * (len(rows) - i) / 60)
    conn.commit()
    return stats


def classify_channel_sides(conn, llm: LLM, labels, rerun: bool = False) -> int:
    """side каналу: LLM по назві + опису + 5 останніх постів (для релевантних без ручної мітки)."""
    n = 0
    public = db.public_usernames(conn)
    for row in db.get_channels(conn):
        ch = db.effective_channel(row, labels)
        lab = labels.get(ch["username"].lower(), {})
        if lab.get("side") or ch["relevant"] is not True:
            continue
        if row["auto_side"] and not rerun:
            continue
        posts = conn.execute("SELECT text FROM posts WHERE channel_id=? AND is_comment=0 AND text<>'' "
                             "ORDER BY date DESC LIMIT 5", (ch["id"],)).fetchall()
        sample = "\n---\n".join((p["text"] or "")[:600] for p in posts) or "(немає)"
        about = row["about"] or ""
        if llm.masks_input:
            sample, about = mask_pii(sample, public), mask_pii(about, public)
        try:
            ans = parse_json(llm.ask("Відповідай лише валідним JSON.", CHANNEL_SIDE_TEMPLATE.format(
                title=row["title"], about=about, posts=sample)))
        except LLMError as e:
            log.error("LLM (side @%s): %s", ch["username"], e)
            continue
        side = str(ans.get("side") or "unknown").lower()
        conn.execute("UPDATE channels SET auto_side=? WHERE id=?", (side if side in SIDES else "unknown", ch["id"]))
        n += 1
    conn.commit()
    return n


def run_classify(conn, kw, labels, llm_kind: str = "ollama", rerun: bool = False, limit: int | None = None) -> dict:
    rules = apply_rules(conn, kw, labels, rerun)
    log.info("Правила: yes=%d unsure=%d no=%d", rules["yes"], rules["unsure"], rules["no"])
    out = {"rules": rules}
    if llm_kind == "none":
        return out
    if llm_kind == "openai":
        log.warning("⚠️  --llm openai: тексти постів (замасковані) будуть відправлені в OpenAI. "
                    "Для чутливих даних використовуй локальний Ollama.")
        time.sleep(3)
    llm = LLM(llm_kind)
    llm.check()
    out["channels_side"] = classify_channel_sides(conn, llm, labels, rerun)
    out["posts"] = classify_posts(conn, llm, labels, rerun, limit)
    return out


def final_relevance(rule: str | None, llm_relevant: str | None) -> str:
    """true | false | unsure — підсумкова релевантність поста."""
    if rule == "no":
        return "false"
    if llm_relevant in ("true", "false", "unsure"):
        return llm_relevant
    return "true" if rule == "yes" else "unsure"
