"""Локальні фільтри: збіг ключів, контекст, хибні спрацювання (ім'я «Алешка»), маскування PII."""
from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .config import Keywords

# Регекс з ТЗ: регістронезалежно, Unicode
CORE_RE = re.compile(r"\b(олешк|олешок|алешк|алёшк|алешек|oleshk|aleshk)\w*", re.I)
LEGACY_RE = re.compile(r"\b(цюрупинськ|цюрупинск|tsiurupynsk)\w*", re.I)

# Форми, що майже завжди є іменем Олексій (Алёшка) / прізвищем Олешко, а не містом
NAME_TOKENS = {
    "алешка", "алешке", "алешку", "алешкой", "алешкою", "алешенька", "алешеньки",
    "алешкин", "алешкина", "алешкину", "алешкиной", "алешкиного", "алешкины",
    "олешко", "олешка", "олешку", "олешком", "олешкові", "олешкова",
}
# «Алешки» = і місто (називний), і ім'я (родовий). Ім'я видають прийменники/контекст.
_NAME_PATTERNS = [
    re.compile(r"(?<!\w)(у|для|от|без|до|подарок|подарки|подарунок|подарунки|с\s+днем|з\s+днем)"
               r"\s+(нашей\s+|нашого\s+|маленькой\s+|маленького\s+)?алешк[иы]\b"),
    re.compile(r"\bалешк[иы]\s+(сегодня\s+|сьогодні\s+)?(день\s+рожден|день\s+народжен|др\b|исполнилось|виповнилось)"),
]


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def find_core(text: str, kw: Keywords | None = None) -> list[str]:
    """Унікальні знайдені форми (нижній регістр, ё→е) у порядку появи."""
    if not text:
        return []
    out: list[str] = []
    for m in CORE_RE.finditer(text):
        t = _norm(m.group(0))
        if t not in out:
            out.append(t)
    if kw is not None and kw.legacy_enabled:
        for m in LEGACY_RE.finditer(text):
            t = _norm(m.group(0))
            if t not in out:
                out.append(t)
    return out


def _context_re(kw: Keywords) -> re.Pattern:
    parts = [re.escape(c).replace(r"\ ", r"\s+") for c in kw.context]
    return re.compile(r"(?<!\w)(" + "|".join(parts) + r")", re.I)


_ctx_cache: dict[int, re.Pattern] = {}


def has_context(text: str, kw: Keywords) -> bool:
    key = id(kw)
    if key not in _ctx_cache:
        _ctx_cache[key] = _context_re(kw)
    return bool(text) and bool(_ctx_cache[key].search(_norm(text)))


def name_pattern_hit(text: str, tokens: Iterable[str]) -> bool:
    if any(t in NAME_TOKENS for t in tokens):
        return True
    low = _norm(text)
    return any(p.search(low) for p in _NAME_PATTERNS)


def _is_ru(token: str) -> bool:
    return token.startswith(("алешк", "алешек"))


def relevance_rule(text: str, kw: Keywords, *, channel_relevant: bool | None = None,
                   channel_local: bool | None = None) -> str:
    """yes | unsure | no — правило з розділів 5 і 9 ТЗ.

    * є українська / латинська форма (крім прізвища «Олешко») або старa назва → yes
    * лише російські форми / «Олешко»:
        - спрацював патерн імені → unsure з контекстом, інакше no
        - є контекстне слово або канал з міткою relevant → yes
        - інакше → unsure (на LLM, далі «Перевірити вручну»)
    * збігу немає: пост з локального каналу → yes (LLM вирішить), інакше no
    """
    tokens = find_core(text, kw)
    if not tokens:
        return "yes" if channel_local else "no"
    strong = [t for t in tokens if not _is_ru(t) and t not in NAME_TOKENS]
    if strong:
        return "yes"
    ctx = has_context(text, kw)
    if name_pattern_hit(text, tokens):
        return "unsure" if ctx else "no"
    if ctx or channel_relevant is True or channel_local is True:
        return "yes"
    return "unsure"


# ---------------------------------------------------------------- нормалізація / хеш

_URL_RE = re.compile(r"(?:https?://|www\.|(?<![\w@])t\.me/|(?<![\w@])telegram\.me/)\S+", re.I)


def normalize_text(text: str) -> str:
    """Для text_hash: нижній регістр, без URL, емодзі, пунктуації, зайвих пробілів."""
    t = _URL_RE.sub(" ", _norm(text or ""))
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:20]


def sender_hash(salt: str, sender_id: int | str) -> str:
    return hashlib.sha256(f"{salt}{sender_id}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- маскування PII

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d(?:[ \t\-().]*\d){8,14}(?![\w])")
_DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})|(\d{4})-(\d{2})-(\d{2})")
_AT_RE = re.compile(r"(?<![\w@/.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")

PHONE_MASK = "[телефон]"
EMAIL_MASK = "[email]"
USER_MASK = "@[користувач]"


def _looks_like_date(s: str) -> bool:
    for m in _DATE_RE.finditer(s):
        if m.group(1):
            d, mo = int(m.group(1)), int(m.group(2))
        else:
            mo, d = int(m.group(5)), int(m.group(6))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return True
    return False


def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    digits = re.sub(r"\D", "", s)
    if not 10 <= len(digits) <= 15 or _looks_like_date(s):
        return s
    return PHONE_MASK


def mask_pii(text: str, public_usernames: set[str] | None = None) -> str:
    """Маскує телефони, email і @username (крім відомих публічних каналів).

    Посилання (t.me/<канал>/…, https://…) не чіпаються.
    """
    if not text:
        return text or ""
    public = {u.lower() for u in (public_usernames or set())}
    urls: list[str] = []

    def _stash(m: re.Match) -> str:
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"

    t = _URL_RE.sub(_stash, text)
    t = _EMAIL_RE.sub(EMAIL_MASK, t)
    t = _PHONE_RE.sub(_mask_phone, t)
    t = _AT_RE.sub(lambda m: m.group(0) if m.group(1).lower() in public else USER_MASK, t)
    return re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], t)


# ---------------------------------------------------------------- витяг посилань (сніжний ком)

_TME_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])", re.I)
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "addlist", "boost",
             "contact", "setlanguage", "addtheme", "addemoji", "login", "invoice", "c"}


def extract_usernames(text: str) -> set[str]:
    """t.me/<username> і @username з тексту (без інвайтів і службових шляхів)."""
    found = set()
    if not text:
        return found
    for m in _TME_LINK_RE.finditer(text):
        u = m.group(1)
        if u.lower() not in _RESERVED:
            found.add(u)
    no_emails = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", text))
    for m in _AT_RE.finditer(no_emails):
        found.add(m.group(1))
    return found
