# Деплой CS2 BETS на Render + Neon — чек-лист

Бесплатно, без карты. Один сервис = бот (webhook) + мини-приложение, база в Neon.
Время: ~10 минут. После этого ссылка на приложение постоянная и ничего не отваливается.

---

## 1. База данных — Neon

1. Открыть <https://neon.tech> → **Sign up** (проще через GitHub).
2. **Create project**: имя `cs2-bets`, регион — ближайший (Frankfurt / eu-central-1).
3. На странице проекта: **Connection string** → скопировать целиком. Выглядит так:

   ```
   postgresql://neondb_owner:AbC123@ep-cool-name-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```

4. Ничего создавать руками не нужно: таблицы бот сделает сам при первом старте.
   (Если строка содержит `channel_binding=require` — это нормально, psycopg её понимает.)

## 2. Код — GitHub

```bash
cd telegram-bot
git init
git add .
git commit -m "cs2 bets: bot + mini app"
# создать пустой репозиторий на github.com и затем:
git remote add origin https://github.com/<ваш-логин>/cs2-bets.git
git push -u origin main
```

Проверить, что `.env` **не** попал в коммит (он в `.gitignore`): `git status --short`.

## 3. Сервис — Render

1. <https://render.com> → **Sign up** через GitHub (карта не нужна).
2. **New → Blueprint** → выбрать репозиторий `cs2-bets`. Render прочитает `render.yaml`.
3. Вписать три секрета:

   | Ключ | Что вписать |
   |---|---|
   | `BOT_TOKEN` | токен из @BotFather |
   | `ADMIN_IDS` | ваш telegram id (число) |
   | `DATABASE_URL` | строка подключения из Neon (шаг 1.3) |

4. **Apply** → ждать сборку образа (~3–5 минут).
5. Остальное Render подставит сам:
   `RUN_MODE=webhook`, `WEBHOOK_BASE_URL`, `WEBHOOK_SECRET`, `MINIAPP_URL`.

## 4. Проверка (2 минуты)

| Что | Ожидаемо |
|---|---|
| `https://<сервис>.onrender.com/health` | `{"ok": true, "mode": "webhook"}` |
| `https://<сервис>.onrender.com/` | экран мини-приложения с матчами |
| `/start` в боте | меню, сверху кнопка **📱 Открыть приложение** |
| Ставка в приложении | баланс меняется и в боте тоже (одна база) |

## 5. Перенести балансы (необязательно)

```bash
cd telegram-bot
export DATABASE_URL='postgresql://...neon.tech/neondb?sslmode=require'
python scripts/migrate_to_postgres.py --dry-run   # посмотреть, что переедет
python scripts/migrate_to_postgres.py             # игроки, чаты, история
```

Ставки и экспрессы не переносятся: матчи на новом хостинге заново приедут из линии Fonbet.

## 6. Чтобы сервис не засыпал

Free-план Render выключает сервис после ~15 минут без запросов (первый заход — 30–50 с).

1. <https://cron-job.org> (или UptimeRobot) → бесплатный аккаунт.
2. Новый job: URL `https://<сервис>.onrender.com/health`, интервал **каждые 10 минут**.
3. Бонус: пинг не даёт пропускать синк линии, так что кэфы остаются свежими.

## Если что-то не так

| Симптом | Причина и лечение |
|---|---|
| Render: `Exited with status 3` | не задан `BOT_TOKEN` или `DATABASE_URL` — смотрите **Logs** |
| `409 Conflict` в логах | где-то ещё запущен polling с тем же токеном — убейте локальный `bot.py` |
| Приложение открывается, но пусто | `MINIAPP_URL` не совпадает с адресом сервиса — поправьте переменную и нажмите **Manual Deploy** |
| `relation "users" does not exist` | база недоступна (неверная строка Neon, истёк пароль) — пересоздайте Connection string |
| Долгая первая загрузка | сервис спал, это норм; поставьте пинг из шага 6 |

## Локально ничего не меняется

```bash
python bot.py        # SQLite, polling
python miniapp.py    # мини-приложение на http://localhost:8081

pytest -q                                        # 62 теста на SQLite
TEST_DATABASE_URL='postgresql://...' pytest -q   # те же 62 на Postgres
```
