import pytest

from oleshky.config import load_keywords, parse_username
from oleshky.filters import (extract_usernames, find_core, mask_pii, normalize_text,
                             relevance_rule, text_hash, PHONE_MASK, EMAIL_MASK, USER_MASK)

KW = load_keywords()


@pytest.mark.parametrize("text", [
    "В Олешках третій день без води",
    "Алешки: ситуация с гуманитаркой",
    "Oleshky under blockade",
    "#олешки",
    "Олешківська громада повідомляє",
    "Алёшки, Херсонская область: обстрел",
    "Ситуация в Алешках", # лише ru, без контексту — але не ім'я
])
def test_relevant(text):
    assert find_core(text, KW)
    assert relevance_rule(text, KW) in ("yes", "unsure")


@pytest.mark.parametrize("text", [
    "В Олешках третій день без води",
    "Алешки: ситуация с гуманитаркой",
    "Oleshky under blockade",
    "#олешки",
    "Олешківська громада повідомляє",
])
def test_relevant_rule_yes(text):
    assert relevance_rule(text, KW) == "yes"


@pytest.mark.parametrize("text", [
    "Подарунок для Алешки",
    "У Алешки сегодня день рождения",
    "Алешка, позвони",
    "Привіт, сьогодні гарна погода",
])
def test_not_relevant(text):
    assert relevance_rule(text, KW) == "no"


def test_ru_only_without_context_is_unsure():
    assert relevance_rule("Ситуация в Алешках", KW) == "unsure"
    assert relevance_rule("Ситуация в Алешках", KW, channel_relevant=True) == "yes"


def test_name_with_context_is_unsure():
    assert relevance_rule("У Алешки день рождения, он из Херсона", KW) == "unsure"


def test_local_channel_without_mention():
    assert relevance_rule("Сьогодні знову немає світла", KW, channel_local=True) == "yes"
    assert relevance_rule("Сьогодні знову немає світла", KW) == "no"


@pytest.mark.parametrize("raw", [
    "+380 67 123 45 67",
    "+7 (978) 123-45-67",
    "067-123-45-67",
])
def test_mask_phone(raw):
    out = mask_pii(f"Дзвоніть {raw} будь-коли")
    assert raw not in out and PHONE_MASK in out


def test_mask_email_and_user():
    out = mask_pii("Пишіть на help.me@gmail.com або @private_user")
    assert "help.me@gmail.com" not in out and EMAIL_MASK in out
    assert "@private_user" not in out and USER_MASK in out


def test_mask_keeps_links_and_public_channels():
    text = "Джерело: https://t.me/some_channel/123 і t.me/other_chan, канал @public_chan"
    out = mask_pii(text, public_usernames={"public_chan"})
    assert "https://t.me/some_channel/123" in out
    assert "t.me/other_chan" in out
    assert "@public_chan" in out


def test_mask_keeps_dates_and_numbers():
    text = "14.09.2026 12:30 — 1500 переглядів, 2026-09-14 10:00"
    assert mask_pii(text) == text


def test_text_hash_normalization():
    a = "В Олешках   немає води!!! 😢 https://t.me/x/1"
    b = "в олешках немає води"
    assert normalize_text(a) == normalize_text(b)
    assert text_hash(a) == text_hash(b)


def test_extract_usernames():
    text = "Репост з https://t.me/oleshky_news/55 та @kherson_info, пишіть a@b.com, інвайт t.me/+AbCdEf"
    assert extract_usernames(text) == {"oleshky_news", "kherson_info"}


@pytest.mark.parametrize("link,expected", [
    ("https://t.me/oleshky_news", "oleshky_news"),
    ("t.me/s/oleshky_news/12", "oleshky_news"),
    ("@oleshky_news", "oleshky_news"),
    ("https://t.me/+AbCdEfGh", None),
    ("https://t.me/joinchat/AbCd", None),
])
def test_parse_username(link, expected):
    assert parse_username(link) == expected
