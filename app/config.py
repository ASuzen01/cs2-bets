from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
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
    min_bet: int = 10
    max_express_legs: int = 6      # сколько исходов можно взять в экспресс
    max_express_odds: float = 40.0  # потолок итогового кэфа экспресса

    # --- Режим запуска ---
    run_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""      # https://my-bot.onrender.com
    webhook_path: str = "/webhook"
    webhook_secret: str = ""        # A-Z a-z 0-9 _ -
    host: str = "0.0.0.0"
    port: int = 8080

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
    sync_interval: int = 90              # период обновления линии, сек
    autopost_matches: bool = True        # постить новые матчи в чаты
    autopost_limit: int = 3              # не больше N новых карточек за один цикл
    autopost_horizon_hours: int = 24     # постить только матчи, что начнутся в ближайшие N часов
    result_check_after_minutes: int = 45  # через сколько минут после старта добиваться результата

    # --- Мини-приложение (Telegram WebApp) ---
    miniapp_url: str = ""        # публичный https-адрес приложения, напр. https://cs2-bets.onrender.com
    miniapp_port: int = 8081
    miniapp_dev_user: int = 0    # локальная разработка: id игрока, если Telegram initData нет
    miniapp_title: str = "CS2 BETS"

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
    def webhook_url(self) -> str:
        base = self.webhook_base_url.strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        return f"{base}{self.webhook_path}"

    @property
    def tier1_include_list(self) -> list[str]:
        return [part.strip() for part in self.tier1_include.split(",") if part.strip()]

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
    def miniapp_enabled(self) -> bool:
        return self._normalize_url(self.miniapp_url).startswith("https://")

    @property
    def webhook_url(self) -> str:
        base = self.webhook_base_url.strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        return f"{base}{self.webhook_path}"

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def use_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))

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
