from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Все настройки читаются из переменных окружения или файла .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = ""
    admin_ids: str = ""  # "123456789,987654321"

    # --- Экономика ---
    currency: str = "🪙"
    start_balance: int = 1000
    daily_bonus: int = 250
    bonus_cooldown_hours: int = 24
    wheel_cooldown_hours: int = 24   # колесо: одна бесплатная крутка в сутки
    wheel_cost: int = 150            # цена платной крутки сверх бесплатной
    tease_cooldown_hours: int = 24   # пасхалка за 10 нажатий — тоже раз в сутки
    backup_enabled: bool = True      # ежедневный дамп базы в личку админу
    backup_interval_hours: int = 1   # как часто снимать копию (1 = каждый час)
    backup_chat_id: int = 0          # 0 — слать в личку админу, иначе в этот чат/канал
    backup_max_mb: int = 25          # предел размера файла, который примем на восстановление
    min_bet: int = 10
    max_express_legs: int = 0      # 0 — без лимита: сколько исходов, столько и бери
    max_express_odds: float = 0.0   # 0 — без потолка: итоговый кэф экспресса не ограничен

    # --- Режим запуска ---
    run_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""      # https://my-bot.onrender.com
    webhook_path: str = "/webhook"
    webhook_secret: str = ""        # A-Z a-z 0-9 _ -
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Перепроверка расчётов по расписанию ---
    recheck_hours: int = 48              # окно перепроверки: за сколько часов сверяем
    recheck_interval_minutes: int = 60   # как часто перепроверять (0 — только при старте)
    recheck_ok_digest_hours: int = 24    # «всё ок» — одной сводкой в сутки

    # --- Не даём бесплатному инстансу Render заснуть ---
    keepalive_enabled: bool = True
    keepalive_interval: int = 600   # секунд между пингами (Render спит после 15 мин тишины)
    keepalive_url: str = ""         # пусто → WEBHOOK_BASE_URL или RENDER_EXTERNAL_URL

    # --- Источник данных: реальная линия Fonbet (CS2 тир-1) ---
    fonbet_base_url: str = "https://line-lb51.bk6bba-resources.com"
    fonbet_scope_market: int = 1600
    # белый список брендов турниров (подстрока в названии, регистр не важен)
    tier1_include: str = (
        "IEM,ESL Pro League,ESL One,BLAST Premier,BLAST Rivals,BLAST Bounty,"
        "BLAST Open,PGL,Major,World Cup,Thunderpick World Championship"
    )
    # чёрный список: квалифайеры, академии, женщины, шоу-матчи, 1x1/2x2
    tier1_exclude: str = (
        "Qualifier,Квалификац,Отбор,Academy,Women,Женск,H2H,1x1,2x2,3x3,"
        "Showmatch,Клуб,1х1,2х2,CCT"
    )
    # real — кэфы как у букмекера; fair — без маржи (маржа убирается, банк живёт дольше)
    odds_mode: Literal["real", "fair"] = "real"
    football_enabled: bool = True
    football_include: str = "лига чемпионов, лига европы, лига конференций"
    football_exclude: str = (
        "азии, персидск, конкакаф, женщ, до 19, до 21, до 17, юнош, молодеж, юфл, "
        "кибер, efootball, fifa, пляж, мини-футбол, футзал, товарищеск"
    )
    sync_interval: int = 45              # период обновления линии, сек (лайв — кэфы свежее)
    # ставки по ходу матча: кэф берётся текущий, приём идёт до конца игры
    live_betting: bool = True
    autopost_matches: bool = True        # постить новые матчи в чаты
    autopost_limit: int = 3              # не больше N новых карточек за один цикл
    autopost_horizon_hours: int = 24     # постить только матчи, что начнутся в ближайшие N часов
    result_check_after_minutes: int = 45  # через сколько минут после старта добиваться результата
    stale_match_hours: int = 12          # через сколько часов без результата матч отменяем с возвратом

    # --- Мини-приложение (Telegram WebApp) ---
    miniapp_url: str = ""        # публичный https-адрес приложения, напр. https://cs2-bets.onrender.com
    miniapp_port: int = 8081
    miniapp_dev_user: int = 0    # локальная разработка: id игрока, если Telegram initData нет
    miniapp_title: str = "Добрая лудка"
    bot_username: str = ""       # заполняется на старте: нужен для ссылки-входа из групп
    miniapp_startapp: bool = False  # true → в группах ссылка t.me/бот?startapp=… (нужен Main Mini App в BotFather)

    # --- Прочее ---
    db_path: str = "data/bot.db"
    # Postgres/Neon. Если задан — используется он, иначе локальный SQLite из DB_PATH.
    database_url: str = ""
    database_schema: str = ""   # схема в Postgres (нужно только для тестов/мультибазы)
    redis_url: str = ""  # если задан, FSM-состояния живут в Redis (нужен aiogram[redis])
    throttle_seconds: float = 0.4
    drop_pending_updates: bool = False
    polling_timeout: int = 10  # сек; меньше 30 — устойчивее через прокси/NAT
    log_level: str = "INFO"

    @property
    def admin_ids_set(self) -> set[int]:
        ids: set[int] = set()
        for chunk in self.admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids

    @property
    def admin_ids_list(self) -> list[int]:
        """Админы в том порядке, в каком заданы: первый — главный (ему идут бэкапы)."""
        ordered: list[int] = []
        for chunk in self.admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit() and int(chunk) not in ordered:
                ordered.append(int(chunk))
        return ordered

    @property
    def webhook_url(self) -> str:
        base = self.webhook_base_url.strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        return f"{base}{self.webhook_path}"

    @property
    def tier1_include_list(self) -> list[str]:
        return [part.strip() for part in self.tier1_include.split(",") if part.strip()]

    @property
    def football_include_list(self) -> list[str]:
        return [part.strip() for part in self.football_include.split(",") if part.strip()]

    @property
    def football_exclude_list(self) -> list[str]:
        return [part.strip() for part in self.football_exclude.split(",") if part.strip()]

    @property
    def tier1_exclude_list(self) -> list[str]:
        return [part.strip() for part in self.tier1_exclude.split(",") if part.strip()]

    @staticmethod
    def _normalize_url(value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else BASE_DIR / path

    def validate_for_run(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise SystemExit(
                "BOT_TOKEN не задан или выглядит неверно.\n"
                "Получите токен у @BotFather и впишите в .env: BOT_TOKEN=123456:AA..."
            )
        if self.run_mode == "webhook":
            if not self.webhook_base_url:
                raise SystemExit(
                    "RUN_MODE=webhook, но WEBHOOK_BASE_URL пустой.\n"
                    "Укажите публичный адрес сервиса, например "
                    "WEBHOOK_BASE_URL=https://my-bot.onrender.com"
                )
            if not self.webhook_path.startswith("/"):
                raise SystemExit("WEBHOOK_PATH должен начинаться с '/'")
            if not self.webhook_secret:
                raise SystemExit(
                    "Для webhook нужен WEBHOOK_SECRET (Telegram проверяет его в заголовке).\n"
                    "Сгенерируйте: openssl rand -hex 32"
                )
            bad = set(self.webhook_secret) - set(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            )
            if bad or len(self.webhook_secret) > 256:
                raise SystemExit(
                    "WEBHOOK_SECRET может содержать только A-Z a-z 0-9 _ - (до 256 символов)"
                )
        if self.min_bet < 1:
            raise SystemExit("MIN_BET должен быть >= 1")
        if self.start_balance < 0 or self.daily_bonus < 0:
            raise SystemExit("START_BALANCE и DAILY_BONUS не могут быть отрицательными")


settings = Settings()
