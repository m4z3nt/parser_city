# oleshky-tg — OSINT-парсер Telegram «Олешки»

Збирає **публічні** Telegram-канали та групи, де згадуються Олешки (Херсонщина), класифікує пости
локальним LLM і видає таблиці, HTML-звіт і markdown для NotebookLM. Запуск інкрементальний:
щоразу береться лише нове.

```
discover → collect → classify → report
 канали     пости     правила    CSV / XLSX / HTML / NotebookLM
                      + Ollama
```

## Безпека: що збирається і що ні

- Лише публічні канали/групи (з `@username`). Інструмент не вступає в чати, не переходить за інвайт-лінками
  і не працює з приватними чатами.
- **Не збираються** учасники, адміни, реакції, діалоги акаунта. В групах автор зберігається лише як
  `sender_kind` (`channel/user/anonymous`) + `sender_hash = sha256(SALT + id)[:16]`.
- Медіа не завантажується: зберігається тільки `media_type`. Окремий пост можна завантажити вручну:
  `collect --download-media <channel_id:msg_id>`.
- У CSV/XLSX/HTML/MD телефони, email і `@username` приватних акаунтів **замасковані**.
  Сирі тексти лежать тільки в локальній `data/oleshky.db`.
- LLM за замовчуванням — **локальний Ollama**. OpenAI вмикається лише прапорцем `--llm openai`
  (тексти перед відправкою маскуються).
- Від імені акаунта нічого не робиться: він не пише, не репостить, не ставить реакцій і не підписується.
- Для роботи потрібен **окремий робочий TG-акаунт**, не особистий.

## Встановлення (Windows / macOS / Linux, Python 3.11+)

```bash
cd oleshky-tg
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### Локальний LLM (Ollama)

1. Встанови Ollama: https://ollama.com/download
2. Завантаж модель:
   ```bash
   ollama pull gemma3:12b
   ```
   Їй потрібно приблизно 8–10 ГБ RAM/VRAM. На слабкому ноутбуці візьми `gemma3:4b` і впиши її
   в `OLLAMA_MODEL`.
3. Ollama працює у фоні на `http://localhost:11434`. Перевірка: `ollama list`.

## Налаштування `.env`

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Заповни такі змінні:

| змінна | що це |
|---|---|
| `TG_API_ID_1`, `TG_API_HASH_1` | https://my.telegram.org → API development tools (робочий акаунт) |
| `TG_SESSION_1` | шлях до файлу сесії, за замовчуванням `data/sessions/oleshky_1` |
| `TG_*_2` | опційно, другий акаунт: на нього перемикаємось при FloodWait > 300 с |
| `OLLAMA_URL`, `OLLAMA_MODEL` | `http://localhost:11434`, `gemma3:12b` |
| `SENDER_HASH_SALT` | `python -c "import secrets; print(secrets.token_hex(32))"`. Згенеруй **один раз** і не змінюй |

`.env`, `*.session` і `data/` прописані в `.gitignore` і в git не потрапляють.

## Перший логін сесії

```bash
python -m oleshky login
```

Telegram попросить номер телефону, код і пароль 2FA (якщо він є). Сесія збережеться в `data/sessions/`.
Логін потрібен один раз на кожен акаунт.

## Запуск

Повний цикл:

```bash
python -m oleshky run --since 2026-01-01
```

Етапи окремо:

```bash
python -m oleshky discover [--depth 1] [--no-recommendations] [--no-hashtags] [--fulltext]
python -m oleshky collect --since 2026-01-01 [--until 2026-09-30] [--channel @username] [--with-comments]
python -m oleshky classify [--llm ollama|openai|none] [--rerun] [--limit 50]
python -m oleshky report [--since-last] [--no-mask]
python -m oleshky status
```

Для щоденного запуску (тільки нове з минулого разу):

```bash
python -m oleshky run --since 2026-01-01 --since-last
```

Windows: щоденний запуск налаштовується через Планувальник завдань. Дія:
`C:\шлях\oleshky-tg\.venv\Scripts\python.exe -m oleshky run --since 2026-01-01 --since-last`, робоча папка — `oleshky-tg`.

### Результати

`data/exports/<YYYY-MM-DD>/`:

- `report.html` — відкривається без інтернету. Містить динаміку по днях, розбивку за типом і стороною,
  блоки «Звернення і свідчення», «Блокада», «Нові канали» і «Перевірити вручну».
- `posts.csv`, `posts.xlsx` — усі знайдені пости. Колонка `relevance` має значення `true/false/unsure`.
- `channels.csv` — канали з мітками.
- `notebooklm/oleshky_tg_<YYYY-MM>.md` — лише релевантні пости, хронологічно, кожен репост один раз.
  Ці файли завантажуються як джерела в блокнот «Олешки — блокада 2026».

Логи пишуться в `data/logs/`.

## Як додати seed-канал

`config/seeds.csv`:

```csv
link,note
https://t.me/some_channel,місцеві новини
@another_channel,
```

Інвайт-лінки (`t.me/+…`, `joinchat`) ігноруються навмисно.

## Як поставити ручну мітку каналу

`config/channel_labels.csv` (ручна мітка завжди перекриває автоматичну; порожня клітинка означає «не чіпати»):

```csv
username,side,relevant,local,note
oleshky_news,ua,true,true,місцевий канал
some_propaganda,occupation,true,false,окупаційні новини Херсонщини
random_channel,,false,,шум
```

- `side`: `ua` / `occupation` / `neutral` / `unknown`
- `relevant=false` — канал більше не збирається
- `local=true` — збирається вся стрічка, а не лише пости зі згадкою міста

## Як це працює

- **discover** шукає кандидатів у кількох джерелах:
  - `seeds.csv`;
  - `contacts.Search` по кожній формі назви (знаходить канали за назвою або username);
  - глобальний пошук постів за хештегами `#олешки #алешки #oleshky`;
  - рекомендації Telegram («схожі канали») для релевантних каналів;
  - «сніжний ком»: джерела репостів, посилання `t.me/…` і згадки `@…` з уже зібраних постів, до `--depth`.
  Кешування резолву username: щоб не впертися в ліміт Telegram, за один запуск резолвиться не більше
  `--max-resolve` (за замовчуванням 50).
- **collect** для кожного каналу шукає кожну форму назви окремо (пошук Telegram погано справляється
  з морфологією), а для локальних каналів бере всю стрічку. Альбоми склеюються в один запис, відредаговані
  пости оновлюються, репости групуються в кластери. Повторний запуск бере тільки нове (`sync_state`).
- **classify** спершу застосовує правила (`yes/unsure/no`), щоб відсіяти «подарунок для Алешки»,
  потім передає пост у LLM, яка повертає JSON із типом, стороною, резюме і цитатою. Якщо цитати немає
  в тексті, пост отримує `unsure`. Результат кешується за хешем тексту, тож репости не проганяються повторно.
- **report** формує файли, описані вище. З `--since-last` у звіт ідуть лише пости, яких не було в минулих звітах.

## Тести

```bash
pytest -q
```

Тести покривають:
- фільтри і маскування;
- відсутність дублів при повторному collect;
- офлайновість HTML;
- відсутність заборонених викликів (`iter_participants`, `iter_dialogs`, реакції, платний пошук тощо);
- відсутність секретів у git.

## Поза скоупом

Інші платформи (у таблиці `posts` вже є `source_platform`), OCR, транскрипція відео,
автопостинг, моніторинг у реальному часі.
