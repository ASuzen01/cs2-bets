"""Лимиты запросов и счётчики для `/metrics`.

Одна память на процесс, без внешних зависимостей: задача — чтобы один игрок
(или скрипт) не мог положить мини-апп шквалом запросов, а не в том, чтобы
строить биллинг. Для нескольких инстансов лимит придётся переносить в Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Final

# путь → (сколько запросов, за сколько секунд)
RULES: Final[dict[str, tuple[int, int]]] = {
    "/api/bet": (20, 60),
    "/api/express": (12, 60),
    "/api/casino/play": (40, 60),
    "/api/wheel": (8, 60),
    "/api/bonus": (8, 60),
    "/api/tease": (30, 60),
    "/api/quest": (30, 60),
    "/api/share": (12, 60),
    "/api/cashout": (20, 60),
    "/api/referral": (10, 60),
    "/api/quest/chest": (10, 60),
    "/api/achievements": (30, 60),
    "/api/honesty": (30, 60),
    "/api/state": (120, 60),
    "/api/stats": (60, 60),
    "/api/bets": (60, 60),
    "/api/history": (60, 60),
    "/api/top": (60, 60),
}
# всё остальное под /api/ — общий потолок
DEFAULT_RULE: Final[tuple[int, int]] = (180, 60)


class RateLimiter:
    """Скользящее окно по (игрок, путь). Памяти на игрока — единицы байт."""

    def __init__(self) -> None:
        self._hits: dict[tuple[int, str], deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, path: str, now: float | None = None) -> bool:
        limit, window = RULES.get(path, DEFAULT_RULE)
        moment = time.monotonic() if now is None else now
        hits = self._hits[(user_id, path)]
        while hits and moment - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(moment)
        return True

    def reset(self) -> None:
        self._hits.clear()


class Metrics:
    """Простые счётчики: их достаточно, чтобы увидеть рост ошибок и объём ставок."""

    def __init__(self) -> None:
        self.started_at = int(time.time())
        self.requests = 0
        self.errors = 0
        self.rejected = 0
        self.by_path: dict[str, int] = defaultdict(int)
        self.errors_by_path: dict[str, int] = defaultdict(int)
        self.bets = 0
        self.express = 0
        self.casino = 0
        self.bonuses = 0

    def count_request(self, path: str) -> None:
        self.requests += 1
        self.by_path[path] += 1

    def count_error(self, path: str) -> None:
        self.errors += 1
        self.errors_by_path[path] += 1

    def count_bet(self) -> None:
        self.bets += 1

    def count_express(self) -> None:
        self.express += 1

    def count_casino(self) -> None:
        self.casino += 1

    def count_bonus(self) -> None:
        self.bonuses += 1

    @staticmethod
    def _label(value: str) -> str:
        """Escape a Prometheus text-format label value."""
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def text(self) -> str:
        """Формат Prometheus-подобный: читается глазами и парсится мониторингом."""
        lines = [
            "# HELP app_up 1 — процесс жив",
            "# TYPE app_up gauge",
            "app_up 1",
            f"app_uptime_seconds {int(time.time()) - self.started_at}",
            f"app_requests_total {self.requests}",
            f"app_errors_total {self.errors}",
            f"app_rate_limited_total {self.rejected}",
            f"app_bets_total {self.bets}",
            f"app_expresses_total {self.express}",
            f"app_casino_rounds_total {self.casino}",
            f"app_bonuses_total {self.bonuses}",
        ]
        for path, count in sorted(self.by_path.items()):
            lines.append(f'app_requests_by_path_total{{path="{self._label(path)}"}} {count}')
        for path, count in sorted(self.errors_by_path.items()):
            lines.append(f'app_errors_by_path_total{{path="{self._label(path)}"}} {count}')
        return "\n".join(lines) + "\n"


limiter = RateLimiter()
metrics = Metrics()

__all__ = ["DEFAULT_RULE", "RULES", "Metrics", "RateLimiter", "limiter", "metrics"]
