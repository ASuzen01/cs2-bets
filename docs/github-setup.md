# GitHub: что и как делать

Репозиторий **уже создан локально** и в него сделан первый коммит (57 файлов).
Секреты не попали: `.env`, базы `.db`, `.venv` — в `.gitignore`.

Осталось загрузить на GitHub. Два пути — выбери удобный.

---

## Путь A. В терминале (5 команд)

### 1. Создать пустой репозиторий на GitHub

1. <https://github.com> → войти/зарегистрироваться.
2. Кнопка **+** справа сверху → **New repository**.
3. Заполнить:
   - **Repository name**: `cs2-bets`
   - **Public** (проще для Render) или **Private** — тогда на шаге Render дашь доступ.
   - **НЕ** ставить галочки `Add a README`, `.gitignore`, `license` — репозиторий должен быть пустым.
4. **Create repository** → GitHub покажет страницу с командами. Нужны две последние.

### 2. Отправить код

```bash
cd telegram-bot

git remote add origin https://github.com/<твой-логин>/cs2-bets.git
git push -u origin main
```

**Про пароль:** GitHub не принимает пароль от аккаунта. Нужен токен:

1. <https://github.com/settings/tokens> → **Generate new token (classic)**.
2. Note: `render deploy`, Expiration: 90 days, галочка **repo**.
3. **Generate token** → скопировать `ghp_...` (показывается один раз).
4. При `git push` в поле **Password** вставить этот токен, в **Username** — свой логин.
   Сохранить токен в менеджере паролей — понадобится при следующих push.

Или, если стоит GitHub CLI: `gh auth login` → `gh repo create cs2-bets --public --source=. --push`.

---

## Путь B. GitHub Desktop (без терминала)

1. Скачать <https://desktop.github.com> и войти своим аккаунтом.
2. **File → Add local repository** → выбрать папку `telegram-bot`.
3. Вверху нажать **Publish repository**, имя `cs2-bets`, снять галочку «Keep this code private», если хочешь публичный.
4. **Publish** — код уедет на GitHub вместе с коммитом.

---

## 3. Проверить, что всё правильно

На странице репозитория должно быть:

- файлы `bot.py`, `miniapp.py`, `app/`, `docs/`, `render.yaml`, `Dockerfile`, `.env.example`;
- **нет** файла `.env` (если он там есть — сразу удалить и перевыпустить токен бота);
- 1 коммит «CS2 BETS: бот ставок на Fonbet + мини-приложение».

Быстрая проверка из терминала:

```bash
git ls-files | grep -iE "\.env$|\.db" ; echo "пусто = секретов нет"
```

---

## 4. Дальше — Render

1. <https://render.com> → **New → Blueprint** → **Connect GitHub** → выбрать `cs2-bets`.
2. Render прочитает `render.yaml` и попросит три секрета:
   - `BOT_TOKEN` — токен бота из @BotFather;
   - `ADMIN_IDS` — твой telegram id;
   - `DATABASE_URL` — строка из Neon (см. `docs/deploy-render-neon.md`).
3. **Apply** → ждать 3–5 минут сборки.

Если репозиторий **приватный**, Render попросит установить GitHub App и дать доступ
именно к этому репозиторию — это одна кнопка в его интерфейсе.

---

## 5. Как обновлять код потом

```bash
cd telegram-bot
git add -A
git commit -m "что поменял"
git push
```

Render подхватит пуш автоматически (`autoDeploy: true` в `render.yaml`) и перезапустит сервис.
Локальные `.env` и база при этом не уезжают — они в `.gitignore`.

---

## Частые грабли

| Симптом | Что делать |
|---|---|
| `remote origin already exists` | `git remote set-url origin https://github.com/<логин>/cs2-bets.git` |
| `Authentication failed` | вместо пароля — Personal Access Token (шаг 2) |
| `src refspec main does not match any` | ты на ветке `master`: `git branch -M main` и повторить push |
| `Updates were rejected` | на GitHub уже есть коммиты: `git pull --rebase origin main`, потом push |
| Render не видит репозиторий | он приватный → дать доступ GitHub App в Render, либо сделать публичным |
| В репозитории оказался `.env` | удалить файл, `git commit`, затем **перевыпустить токен** у @BotFather |
