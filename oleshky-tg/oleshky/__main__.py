"""CLI: python -m oleshky {login,discover,collect,classify,report,run,status}."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db

log = logging.getLogger("oleshky")


def setup_logging(verbose: bool = False):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(config.LOGS_DIR / f"oleshky_{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # консоль Windows
    except Exception:
        pass


def kyiv_date(s: str, end: bool = False) -> datetime:
    """'2026-01-01' → початок (або кінець, end=True) дня за Києвом, у UTC."""
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=ZoneInfo(config.KYIV_TZ_NAME))
    if end:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc)


# ---------------------------------------------------------------- commands

async def cmd_login(_args):
    from .tg_client import login_all
    await login_all()


def _discover_opts(args):
    from .discover import DiscoverOptions
    return DiscoverOptions(depth=args.depth, recommendations=not args.no_recommendations,
                           hashtags=not args.no_hashtags, fulltext=args.fulltext,
                           min_participants=args.min_participants, max_resolve=args.max_resolve)


def _collect_opts(args, only_new=False):
    from .collect import CollectOptions
    return CollectOptions(since=kyiv_date(args.since), until=kyiv_date(args.until, end=True) if args.until else None,
                          with_comments=args.with_comments, refresh_days=args.refresh_days,
                          only_new_channels=only_new)


async def cmd_discover(args):
    from .discover import run_discover
    from .tg_client import AccountPool
    kw, labels, salt = config.load_keywords(), config.load_labels(), config.sender_salt()
    conn = db.connect()
    async with AccountPool() as pool:
        st = await run_discover(conn, pool, kw, labels, salt, _discover_opts(args))
    log.info("Discover готово: %s", st)


async def cmd_collect(args):
    from .collect import download_media, run_collect
    from .discover import DiscoverOptions, Discoverer
    from .tg_client import AccountPool
    conn = db.connect()
    async with AccountPool() as pool:
        if args.download_media:
            path = await download_media(pool, conn, args.download_media)
            log.info("Медіа: %s", path or "немає медіа в пості")
            return
        kw, labels, salt = config.load_keywords(), config.load_labels(), config.sender_salt()
        only = None
        if args.channel:
            only = config.parse_username(args.channel)
            if not only:
                raise SystemExit(f"Невірний канал: {args.channel} (інвайт-лінки не підтримуються)")
            d = Discoverer(conn, pool, kw, labels, salt, DiscoverOptions())
            await d.resolve(only, "manual", 0, force=True)
        st = await run_collect(conn, pool, kw, labels, salt, _collect_opts(args), only=only)
    log.info("Collect готово: %s", st)


def cmd_classify(args):
    from .classify import LLMError, run_classify
    conn = db.connect()
    try:
        st = run_classify(conn, config.load_keywords(), config.load_labels(), args.llm, args.rerun, args.limit)
    except LLMError as e:
        raise SystemExit(f"❌ {e}")
    log.info("Classify готово: %s", st)


def cmd_report(args):
    from .report import run_report
    conn = db.connect()
    since = kyiv_date(args.from_date).isoformat() if args.from_date else None
    out = run_report(conn, config.load_labels(), since_last=args.since_last, mask=not args.no_mask, since=since)
    print(f"\n✅ Звіт: {out / 'report.html'}")


async def cmd_run(args):
    from .classify import LLMError, run_classify
    from .collect import run_collect
    from .discover import run_discover
    from .report import run_report
    from .tg_client import AccountPool
    kw, labels, salt = config.load_keywords(), config.load_labels(), config.sender_salt()
    conn = db.connect()
    async with AccountPool() as pool:
        log.info("=== 1/4 discover")
        await run_discover(conn, pool, kw, labels, salt, _discover_opts(args))
        log.info("=== 2/4 collect")
        await run_collect(conn, pool, kw, labels, salt, _collect_opts(args))
        if args.depth > 0:
            log.info("=== 2b collect нових каналів зі сніжного кому")
            await run_discover(conn, pool, kw, labels, salt, _discover_opts(args), snowball_only=True)
            await run_collect(conn, pool, kw, labels, salt, _collect_opts(args, only_new=True))
    log.info("=== 3/4 classify (%s)", args.llm)
    try:
        run_classify(conn, kw, labels, args.llm)
    except LLMError as e:
        log.error("LLM недоступна (%s) — звіт буде лише за правилами", e)
    log.info("=== 4/4 report")
    out = run_report(conn, labels, since_last=args.since_last, mask=not args.no_mask)
    print(f"\n✅ Звіт: {out / 'report.html'}")


def cmd_status(_args):
    conn = db.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"БД: {config.DB_PATH}")
    print(f"Каналів: {q('SELECT COUNT(*) FROM channels')} "
          f"(релевантних авто: {q('SELECT COUNT(*) FROM channels WHERE auto_relevant=1')}, "
          f"недоступних: {q('SELECT COUNT(*) FROM channels WHERE inaccessible=1')})")
    print(f"Постів: {q('SELECT COUNT(*) FROM posts')} (коментарів: {q('SELECT COUNT(*) FROM posts WHERE is_comment=1')})")
    for r in conn.execute("SELECT relevance_rule, COUNT(*) FROM posts GROUP BY 1"):
        print(f"  правило {r[0]}: {r[1]}")
    print(f"Класифіковано LLM: {q('SELECT COUNT(*) FROM classifications')}")
    for r in conn.execute("SELECT relevant, COUNT(*) FROM classifications GROUP BY 1"):
        print(f"  LLM relevant={r[0]}: {r[1]}")
    print(f"Останній збір: {q('SELECT MAX(last_run_at) FROM sync_state')}")


# ---------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m oleshky", description="OSINT-парсер Telegram «Олешки»")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="перший логін Telegram-сесій з .env")
    sub.add_parser("status", help="коротка статистика БД")

    def add_discover(sp):
        sp.add_argument("--depth", type=int, default=1, help="глибина сніжного кому (default 1)")
        sp.add_argument("--no-recommendations", action="store_true")
        sp.add_argument("--no-hashtags", action="store_true")
        sp.add_argument("--fulltext", action="store_true",
                        help="SearchPosts(query=…) — лише безкоштовні спроби, без зірок")
        sp.add_argument("--min-participants", type=int, default=0)
        sp.add_argument("--max-resolve", type=int, default=50, help="ліміт резолву username за запуск")

    def add_collect(sp):
        sp.add_argument("--since", default="2026-01-01", help="YYYY-MM-DD (Київ)")
        sp.add_argument("--until", help="YYYY-MM-DD включно (курсор інкрементальності не зсувається)")
        sp.add_argument("--with-comments", action="store_true")
        sp.add_argument("--refresh-days", type=int, default=3, help="перечитати пости за N днів (правки)")

    def add_report(sp):
        sp.add_argument("--since-last", action="store_true", help="лише пости, яких не було в минулих звітах")
        sp.add_argument("--no-mask", action="store_true", help="без маскування PII — тільки локально!")

    add_discover(sub.add_parser("discover", help="етап 1: пошук каналів"))

    c = sub.add_parser("collect", help="етап 2: збір постів")
    add_collect(c)
    c.add_argument("--channel", help="@username — зібрати лише цей канал")
    c.add_argument("--download-media", metavar="CHANNEL_ID:MSG_ID", help="завантажити медіа одного поста")

    k = sub.add_parser("classify", help="етап 3: правила + LLM")
    k.add_argument("--llm", choices=["ollama", "openai", "none"], default="ollama")
    k.add_argument("--rerun", action="store_true")
    k.add_argument("--limit", type=int, help="не більше N текстів (для пробного запуску)")

    r = sub.add_parser("report", help="етап 4: звіти")
    add_report(r)
    r.add_argument("--from", dest="from_date", help="лише пости з цієї дати (YYYY-MM-DD)")

    a = sub.add_parser("run", help="discover → collect → classify → report")
    add_discover(a)
    add_collect(a)
    add_report(a)
    a.add_argument("--llm", choices=["ollama", "openai", "none"], default="ollama")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    handlers = {"login": cmd_login, "discover": cmd_discover, "collect": cmd_collect, "classify": cmd_classify,
                "report": cmd_report, "run": cmd_run, "status": cmd_status}
    fn = handlers[args.cmd]
    if asyncio.iscoroutinefunction(fn):
        asyncio.run(fn(args))
    else:
        fn(args)


if __name__ == "__main__":
    main()
