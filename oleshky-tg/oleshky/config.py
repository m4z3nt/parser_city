"""Шляхи, .env, keywords.yaml, seeds.csv, channel_labels.csv."""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent          # oleshky-tg/
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")
load_dotenv()  # .env з поточної папки — як запасний варіант

DATA_DIR = Path(os.getenv("OLESHKY_DATA_DIR") or ROOT / "data")
DB_PATH = DATA_DIR / "oleshky.db"
EXPORTS_DIR = DATA_DIR / "exports"
LOGS_DIR = DATA_DIR / "logs"
MEDIA_DIR = DATA_DIR / "media"

KYIV_TZ_NAME = "Europe/Kyiv"


# ---------------------------------------------------------------- keywords

@dataclass
class Keywords:
    core_uk: list[str] = field(default_factory=list)
    core_ru: list[str] = field(default_factory=list)
    core_lat: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    legacy_enabled: bool = False
    legacy_terms: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)

    @property
    def search_terms(self) -> list[str]:
        """Форми для пошуку в Telegram (без дублів, порядок збережено)."""
        terms = self.core_uk + self.core_ru + self.core_lat
        if self.legacy_enabled:
            terms += self.legacy_terms
        seen, out = set(), []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out


def load_keywords(path: Path | None = None) -> Keywords:
    path = path or CONFIG_DIR / "keywords.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    legacy = raw.get("legacy") or {}
    return Keywords(
        core_uk=list(raw.get("core_uk") or []),
        core_ru=list(raw.get("core_ru") or []),
        core_lat=list(raw.get("core_lat") or []),
        hashtags=[h.lstrip("#") for h in raw.get("hashtags") or []],
        legacy_enabled=bool(legacy.get("enabled", False)),
        legacy_terms=list(legacy.get("terms") or []),
        context=[c.lower() for c in raw.get("context") or []],
    )


# ---------------------------------------------------------------- usernames / links

_INVITE_RE = re.compile(r"t\.me/(\+|joinchat/)", re.I)
_TME_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
_AT_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_username(link: str) -> str | None:
    """'@name', 't.me/name', 'https://t.me/s/name/123' → 'name'. Інвайт-лінки → None."""
    link = (link or "").strip()
    if not link or _INVITE_RE.search(link):
        return None
    m = _TME_RE.search(link)
    if m:
        return m.group(1)
    m = _AT_RE.match(link)
    return m.group(1) if m else None


# ---------------------------------------------------------------- seeds / labels

def load_seeds(path: Path | None = None) -> list[tuple[str, str]]:
    """[(username, note)]; інвайт-лінки та сміття пропускаються."""
    path = path or CONFIG_DIR / "seeds.csv"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            uname = parse_username(row.get("link", ""))
            if uname:
                out.append((uname, (row.get("note") or "").strip()))
    return out


_TRUE = {"true", "1", "yes", "y", "так", "да", "+"}
_FALSE = {"false", "0", "no", "n", "ні", "нет", "-"}
SIDES = ("ua", "occupation", "neutral", "unknown")


def _parse_bool(v: str | None) -> bool | None:
    v = (v or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def load_labels(path: Path | None = None) -> dict[str, dict]:
    """username(lower) → {side, relevant, local, note}. Порожня клітинка = не перекривати."""
    path = path or CONFIG_DIR / "channel_labels.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            uname = parse_username(row.get("username", ""))
            if not uname:
                continue
            side = (row.get("side") or "").strip().lower() or None
            out[uname.lower()] = {
                "side": side if side in SIDES else None,
                "relevant": _parse_bool(row.get("relevant")),
                "local": _parse_bool(row.get("local")),
                "note": (row.get("note") or "").strip(),
            }
    return out


# ---------------------------------------------------------------- env settings

@dataclass
class Account:
    api_id: int
    api_hash: str
    session: str


def load_accounts() -> list[Account]:
    accounts = []
    for i in range(1, 10):
        api_id, api_hash = os.getenv(f"TG_API_ID_{i}"), os.getenv(f"TG_API_HASH_{i}")
        if not (api_id and api_hash):
            continue
        session = os.getenv(f"TG_SESSION_{i}") or f"data/sessions/oleshky_{i}"
        session_path = Path(session)
        if not session_path.is_absolute():
            session_path = ROOT / session_path
        session_path.parent.mkdir(parents=True, exist_ok=True)
        accounts.append(Account(int(api_id), api_hash, str(session_path)))
    return accounts


def sender_salt() -> str:
    salt = os.getenv("SENDER_HASH_SALT", "")
    if len(salt) < 16:
        raise SystemExit(
            "❌ SENDER_HASH_SALT у .env порожня або закоротка.\n"
            '   Згенеруй: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return salt


def ollama_url() -> str:
    url = (os.getenv("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
    return url if url.endswith("/api/chat") else url + "/api/chat"


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL") or "gemma3:12b"
