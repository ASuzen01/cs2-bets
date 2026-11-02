# 🎯 CS2 BETS — ставки на реальные матчи CS2 внутри Telegram

Python 3.10+ / aiogram 3 / aiohttp. Матчи и кэфы берутся из **реальной линии Fonbet** и проходят
фильтр **только тир-1** (IEM, ESL Pro League, BLAST, PGL, мейджоры, Esports World Cup) — квалифайеры,
академии, `H2H 2x2` и прочий мусорный CS2 отсекаются. Ставки — виртуальными монетами, кэф
фиксируется в момент ставки, приём закрывается по старту, результат считается автоматически.

**Бот в Telegram — это только вход.** В нём одна кнопка «📱 Открыть приложение» и служебные
команды; все ставки, экспрессы, баланс, бонус, история и топ живут в мини-приложении.
Меню-кнопка «Ставки» слева в строке ввода открывает приложение откуда угодно.

Живёт на бесплатном Render + Neon: **https://cs2-bets-bot.onrender.com**

---

## Как это работает

```
Fonbet listBase (gzip JSON)
        │  app/services/fonbet.py — парсинг, фильтр тир-1, кэфы 921/923
        ▼
   matches (Postgres / SQLite)  ◄──  бот (webhook) + мини-приложение, один процесс, одна база
        ▲                        │
        │  app/services/sync.py  │ POST /api/bet  → ставка с зафиксированным кэфом
        │  каждые 90 сек:        ▼
        └─ закрытие по старту, расчёт по счёту, выплата «ставка × кэф»
```

## Что где живёт

| Интерфейс | Что делает бот | Что делает приложение |
|---|---|---|
| `/start` | приветствие, баланс, кнопка «Открыть приложение» | — |
| Матчи | — | линия тир-1, тап по кэфу → сумма → ставка |
| Экспресс | — | корзина 2–6 исходов, кэф пересчитывается на лету |
| Я | — | баланс, бонус раз в 24 ч, ставки в игре, правила |
| История / Топ | — | операции, таблица игроков |
| Результаты | админ подтверждает спорный исход кнопками в личке | — |

Команды бота (больше ничего не нужно):

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | все | кнопка «Открыть приложение» |
| `/help` | все | правила, лимиты, математика кэфов |
| `/me` | все | свой telegram id и есть ли права админа |
| `/results` | админ | матчи, которые ждут подтверждения исхода |
| `/stats` | админ | сводка по базе |
| `/players` | админ | последние игроки с id и балансом — кому начислять |
| `/grant <id\|@ник> <сумма>` | админ | начислить монеты игроку (500 или -500) |

Права админа выдаются переменной `ADMIN_IDS` (id через запятую) — на Render это
**Environment** сервиса. Кто админ, видно командой `/me` в боте.

## Мини-приложение (Telegram WebApp)

Один файл `app/web/index.html`: инлайн CSS/JS, без внешних библиотек, тема Telegram, haptic.

| Что важно | Как устроено |
|---|---|
| Одна база | бот и приложение читают/пишут один и тот же Postgres (или SQLite локально) |
| Авторизация | `initData` от Telegram, HMAC-SHA256 (`app/services/miniapp.py`), просрочка > 24 ч отсекается |
| API | `GET /health`, `GET /`, `GET /api/state`, `POST /api/bet`, `POST /api/express`, `POST /api/bonus`, `GET /api/top`, `GET /api/history` |
| Защита | без валидной подписи любой `/api/*` отдаёт `401` |
| Локальная отладка | `MINIAPP_DEV_USER=<telegram_id> python miniapp.py` → `http://localhost:8081` в браузере |

## Экспрессы

Несколько исходов в одной ставке, кэфы перемножаются — как в букмекерской линии.

| Ситуация | Итог |
|---|---|
| все исходы зашли | выплата `ставка × кэф1 × кэф2 × …` |
| хотя бы один не зашёл | экспресс проигран, выплата 0 (считается сразу, не дожидаясь остальных) |
| матч отменён | его кэф = 1.0, остальные пересчитываются |
| отменены все матчи | полный возврат ставки |

Лимиты: минимум 2 исхода, максимум 6 (`MAX_EXPRESS_LEGS`), потолок итогового кэфа 40
(`MAX_EXPRESS_ODDS`). Один матч нельзя взять дважды в один экспресс.

## Деньги и кэфы

- Ставка: `выплата = ставка × кэф` при угаданном исходе, `0` при ошибке.
- Кэф фиксируется в момент ставки — линия может сдвинуться, ваша ставка уже не меняется.
- Приём ставок закрывается автоматически в момент начала матча.
- `ODDS_MODE=real` — кэфы как у букмекера (с маржой ≈5 %). `ODDS_MODE=fair` — маржа убирается.
- Матч отменён → админ отменяет его в боте, ставки возвращаются полностью.

---

## Структура проекта

```
telegram-bot/
├── bot.py                    # точка входа бота: polling/webhook, команды, меню-кнопка, синк
├── miniapp.py                # точка входа мини-приложения отдельным процессом (порт 8081)
├── app/
│   ├── config.py             # настройки из .env
│   ├── texts.py              # все тексты
│   ├── keyboards.py          # две клавиатуры: кнопка приложения + подтверждение исхода
│   ├── middlewares.py        # регистрация игроков и групповых чатов, анти-спам
│   ├── web/
│   │   ├── server.py         # маршруты и обработчики API (тонкий слой)
│   │   ├── auth.py           # авторизация по initData, единый формат ошибок
│   │   ├── payloads.py       # сборка JSON для интерфейса
│   │   ├── index.html        # разметка экранов
│   │   └── static/
│   │       ├── app.css       # все стили
│   │       └── app.js        # вся логика интерфейса
│   ├── db/
│   │   ├── database.py       # матчи/ставки/экспрессы/балансы, транзакции
│   │   └── backends.py       # SQLite и Postgres за одним интерфейсом
│   ├── services/
│   │   ├── fonbet.py         # клиент линии Fonbet + тир-1 фильтр
│   │   ├── miniapp.py        # проверка initData (HMAC-SHA256)
│   │   └── sync.py           # фон: обновление линии, закрытие, расчёт, постинг
│   └── handlers/
│       ├── common.py         # /start /help — единственная кнопка приложения
│       ├── admin.py          # /results /stats /grant + подтверждение исхода
│       ├── misc.py           # фолбэк на текст в личке
│       └── errors.py
├── tests/                    # 69 тестов: парсер, экономика, экспрессы, API, лаунчер
├── pyproject.toml            # настройки pytest и ruff
├── scripts/
│   ├── smoke_check.py        # быстрая проверка сборки
│   ├── integration_check.py  # сквозной прогон: бот-лаунчер + HTTP API
│   └── migrate_to_postgres.py
├── docs/deploy-render-neon.md · docs/github-setup.md · docs/upload-via-browser.md
├── Dockerfile · render.yaml · deploy/cs2-bets.service
└── .env.example
```

---

## Локальный запуск

```bash
cd telegram-bot
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env      # вписать BOT_TOKEN и ADMIN_IDS
python miniapp.py         # приложение на http://localhost:8081 (в браузере, без Telegram)
python bot.py             # бот в Telegram: /start → кнопка приложения
```

Для браузера без Telegram задайте `MINIAPP_DEV_USER=<telegram_id>` и `MINIAPP_URL=http://localhost:8081`.

### Тесты и линтер

```bash
make check                                       # линтер + тесты + smoke + интеграция
ruff check app bot.py miniapp.py scripts tests   # только линтер
pytest -q                                        # 69 тестов
python scripts/smoke_check.py                    # роутеры, БД, карточка матча
python scripts/integration_check.py              # /start, /results, API, ставка
TEST_DATABASE_URL='postgresql://...' pytest -q   # тот же набор на Postgres
```

---

## Настройка тир-1 и кэфов

```env
TIER1_INCLUDE=IEM,ESL Pro League,ESL One,BLAST Premier,BLAST Rivals,BLAST Bounty,BLAST Open,PGL,Major,World Cup
TIER1_EXCLUDE=Qualifier,Квалификац,Academy,Women,H2H,1x1,2x2,Showmatch,CCT
ODDS_MODE=real          # real | fair
SYNC_INTERVAL=45
LIVE_BETTING=true       # ставки по ходу матча (false — приём только до старта)
```

Отсекаются, например: `Counter-Strike. H2H CS. 2X2. Москва. Bo1`, `Counter-Strike. European Pro
League. Bo3`, `Counter-Strike. IEM Пекин. Open Qualifier. Bo3`; остаются
`Counter-Strike. IEM Пекин. Групповой этап. Bo3`, `Counter-Strike. BLAST Rivals. Групповой этап. Bo3`.

Расчёт результата:

1. Пока матч в линии, бот читает счёт (`eventMiscs.score1/score2`, флаг `finished`).
2. Счёт серии (2:0, 2:1, 3:2 для Bo5) → автоматическая выплата и уведомление игрокам.
3. Если счёт неоднозначный или матч ушёл из линии без результата — бот присылает админу
   карточку с кнопками `🏆 Aurora / 🏆 G2 / 🚫 не состоялся`. Список таких матчей — `/results`.

---

## Деплой: Render + Neon

Прод сейчас работает по этой схеме: один сервис (`render.yaml`, Docker) поднимает бота в
режиме webhook и мини-приложение на одном адресе, база — Postgres в Neon.

```bash
# 1. база: neon.tech → Create project → скопировать Connection string
# 2. код:  git push в GitHub
# 3. Render: New → Blueprint → выбрать репозиторий → вписать BOT_TOKEN, ADMIN_IDS, DATABASE_URL
```

Три переменные, которые задаются руками (`sync: false` в `render.yaml`):

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен из @BotFather |
| `ADMIN_IDS` | ваш telegram id |
| `DATABASE_URL` | строка подключения Neon целиком |

`WEBHOOK_BASE_URL` и `MINIAPP_URL` должны быть **полными адресами** —
`https://<сервис>.onrender.com` (не имя сервиса), `WEBHOOK_SECRET` — только `A-Z a-z 0-9 _ -`.
Подробный чек-лист с реальными граблями: `docs/deploy-render-neon.md`.

Перенос балансов из локальной SQLite:

```bash
DATABASE_URL='postgresql://...' python scripts/migrate_to_postgres.py --source data/bot.db --dry-run
DATABASE_URL='postgresql://...' python scripts/migrate_to_postgres.py --source data/bot.db
```

**Ограничения free-плана:** сервис засыпает через ~15 минут без запросов, первый заход после
сна — 30–50 секунд (лечится пингом `/health` раз в 10 минут через cron-job.org или UptimeRobot).

---

## Источник данных: важные оговорки

- Fonbet **не публикует официальный контракт API**. Эндпоинт `events/listBase` — публичная линия
  сайта, он может измениться в любой момент. Весь доступ изолирован в `app/services/fonbet.py`:
  заменить источник = переписать один класс, логика ставок и выплат не трогается.
- Автоматический парсинг чужого сайта может нарушать правила букмекера. Бот использует только
  публичные данные линии, ничего не ставит и не авторизуется; ответственность за использование
  — на владельце бота. Для приватной игры с друзьями на виртуальные монеты риск минимальный.
- Кэфы — интеллектуальная собственность букмекера: не выводите их в публичный продукт.
- Вся игра — на виртуальные монеты: пополнений, выводов и реальных ставок нет.

## Переменные окружения

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `BOT_TOKEN` | — | токен от @BotFather |
| `ADMIN_IDS` | пусто | id админов через запятую (они же судьи результатов) |
| `START_BALANCE` / `DAILY_BONUS` / `MIN_BET` | 1000 / 250 / 10 | экономика |
| `MAX_EXPRESS_LEGS` / `MAX_EXPRESS_ODDS` | 6 / 40 | лимиты экспрессов |
| `DATABASE_URL` | пусто | строка Postgres; пусто → SQLite из `DB_PATH` |
| `DATABASE_SCHEMA` | пусто | схема Postgres (по умолчанию `public`) |
| `DB_PATH` | `data/bot.db` | файл SQLite, если Postgres не задан |
| `FONBET_BASE_URL` | `https://line-lb51.bk6bba-resources.com` | хост линии |
| `TIER1_INCLUDE` / `TIER1_EXCLUDE` | см. выше | фильтр турниров |
| `ODDS_MODE` | `real` | `real` (с маржой) или `fair` (без маржи) |
| `SYNC_INTERVAL` | 45 | период обновления линии, сек (в лайве кэфы свежее) |
| `LIVE_BETTING` | `true` | `true` — ставки по ходу матча, `false` — только до старта |
| `RUN_MODE` | `polling` | `polling` или `webhook` |
| `WEBHOOK_BASE_URL` / `WEBHOOK_PATH` / `WEBHOOK_SECRET` | — | для webhook |
| `MINIAPP_URL` | пусто | публичный https-адрес приложения (кнопка и меню-кнопка) |
| `MINIAPP_PORT` | 8081 | порт `python miniapp.py` |
| `MINIAPP_DEV_USER` | 0 | id игрока для входа в браузере без Telegram (только отладка) |
| `PORT` | 8080 | порт HTTP-сервера |
| `POLLING_TIMEOUT` | 10 | таймаут long polling, сек |
| `THROTTLE_SECONDS` / `LOG_LEVEL` | 0.4 / INFO | анти-спам, логи |

---

## Если что-то не работает

1. **В боте нет кнопки приложения** → пустой `MINIAPP_URL`: `/start` пришлёт текст без кнопки,
   `/help` прямо скажет, чего не хватает. Задайте `https://<сервис>.onrender.com` и перезапустите.
2. **«Нет матчей» в приложении** → смотрите логи: строка `Линия: N матчей тир-1`. `N=0` — линия
   пуста, фильтр `TIER1_INCLUDE` слишком строгий или хостинг не видит Fonbet.
3. **Приложение висит на загрузке** → `curl -s $MINIAPP_URL/health` должен вернуть `{"ok": true}`.
   После сна free-плана первый запрос занимает 30–50 с — это нормально.
4. **`401` на `/api/state`** → нет подписи `initData`: приложение открыто не из Telegram
   (в браузере нужен `MINIAPP_DEV_USER`).
5. **Бот «не откликается», хотя процесс жив** → запущено несколько экземпляров: Telegram отдаёт
   апдейты только одному, остальные получают `409 Conflict`. Бот берёт `flock` на `data/bot.lock`,
   второй экземпляр сам завершается с кодом 2.
6. **Результат не пришёл сам** → `/results` покажет матчи, ждущие подтверждения, с кнопками.
7. **Данные пропали после перезапуска** → не задан `DATABASE_URL`, приложение пишет в SQLite
   внутри контейнера, а он пересоздаётся. Проверьте переменную на Render.
