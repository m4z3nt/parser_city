"""Критерії приймання: заборонені виклики, відсутність секретів."""
import re
import subprocess
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "oleshky"
REPO = PKG.parent.parent

FORBIDDEN = ["iter_participants", "GetParticipantsRequest", "GetMessageReactionsListRequest",
             "iter_dialogs", "allow_paid_stars", "send_message", "send_file", "JoinChannelRequest",
             "ImportChatInviteRequest", "SendReactionRequest", "forward_messages"]


def _code_files():
    return list(PKG.rglob("*.py"))


def test_no_forbidden_calls():
    for f in _code_files():
        src = f.read_text(encoding="utf-8")
        for name in FORBIDDEN:
            assert name not in src, f"{name} у {f.name}"


def test_no_hardcoded_api_credentials():
    pat = re.compile(r"api_hash\s*=\s*['\"][0-9a-f]{32}['\"]|api_id\s*=\s*\d{5,}", re.I)
    for f in list(_code_files()) + list(REPO.glob("*.py")):
        assert not pat.search(f.read_text(encoding="utf-8")), f.name


def test_git_does_not_track_secrets():
    try:
        files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except Exception:
        return
    for line in files.splitlines():
        assert not line.endswith((".env", ".session", ".session-journal", ".db")), line
        assert not line.startswith("oleshky-tg/data/"), line
