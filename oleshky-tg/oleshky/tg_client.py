"""Акаунти з .env, ротація, FloodWait-ретраї.

Клієнт працює лише на читання: модуль не містить жодних методів відправки,
вступу в чати, реакцій чи підписок.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .config import Account, load_accounts

log = logging.getLogger("oleshky.tg")
T = TypeVar("T")

SWITCH_THRESHOLD = 300   # FloodWait довше — перемикаємось на інший акаунт
AUTO_SLEEP = 60          # коротші FloodWait Telethon відсипає сам


class NotAuthorized(RuntimeError):
    pass


class AccountPool:
    def __init__(self, accounts: list[Account] | None = None, interactive: bool = False):
        self.accounts = accounts if accounts is not None else load_accounts()
        if not self.accounts:
            raise SystemExit("❌ Немає TG_API_ID_1 / TG_API_HASH_1 у oleshky-tg/.env (див. .env.example)")
        self.interactive = interactive
        self.clients: dict[int, TelegramClient] = {}
        self.blocked_until: dict[int, float] = {}
        self.current = 0

    async def __aenter__(self) -> "AccountPool":
        await self._client(0)
        return self

    async def __aexit__(self, *exc):
        for c in self.clients.values():
            await c.disconnect()

    async def _client(self, idx: int) -> TelegramClient:
        if idx in self.clients:
            return self.clients[idx]
        acc = self.accounts[idx]
        client = TelegramClient(acc.session, acc.api_id, acc.api_hash,
                                flood_sleep_threshold=AUTO_SLEEP, receive_updates=False)
        await client.connect()
        if not await client.is_user_authorized():
            if not self.interactive:
                await client.disconnect()
                raise NotAuthorized(
                    f"Сесія {acc.session} не авторизована. Запусти: python -m oleshky login")
            await client.start()
        me = await client.get_me()
        log.info("Акаунт %d: увійшли як id=%s", idx + 1, me.id)
        self.clients[idx] = client
        return client

    async def client(self) -> TelegramClient:
        return await self._client(self.current)

    def _pick_other(self) -> int | None:
        now = time.time()
        for i in range(len(self.accounts)):
            if i != self.current and self.blocked_until.get(i, 0) <= now:
                return i
        return None

    async def run(self, fn: Callable[[TelegramClient], Awaitable[T]], label: str = "") -> T:
        """Виконує fn(client). FloodWait → sleep + 1 ретрай; > 300 с → інший акаунт.

        fn має бути ідемпотентною (повтор не створює дублів — БД дедуплікує).
        """
        slept = False
        for _ in range(len(self.accounts) + 2):
            client = await self.client()
            try:
                return await fn(client)
            except FloodWaitError as e:
                wait = e.seconds + 3
                if wait > SWITCH_THRESHOLD:
                    self.blocked_until[self.current] = time.time() + wait
                    other = self._pick_other()
                    if other is not None:
                        log.warning("FloodWait %ds (%s) на акаунті %d → перемикаюсь на акаунт %d",
                                    e.seconds, label, self.current + 1, other + 1)
                        self.current = other
                        continue
                if slept:
                    raise
                log.warning("FloodWait %ds (%s) — чекаю", e.seconds, label)
                await asyncio.sleep(wait)
                slept = True
        raise RuntimeError(f"FloodWait не минає ({label})")


async def login_all():
    """Перший інтерактивний логін усіх акаунтів з .env (телефон + код + 2FA)."""
    pool = AccountPool(interactive=True)
    try:
        for i in range(len(pool.accounts)):
            print(f"\n=== Акаунт {i + 1}: {pool.accounts[i].session}")
            client = await pool._client(i)
            me = await client.get_me()
            print(f"✓ Авторизовано: id={me.id}")
    finally:
        await pool.__aexit__(None, None, None)
