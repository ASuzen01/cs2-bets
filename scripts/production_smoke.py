"""Проверяет, что ожидаемый коммит появился и production отвечает исправно.

CI запускает скрипт после push в main. Переменные: PRODUCTION_BASE_URL и EXPECTED_COMMIT.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("PRODUCTION_BASE_URL", "https://cs2-bets-bot.onrender.com").rstrip("/")
EXPECTED_COMMIT = os.getenv("EXPECTED_COMMIT", "")[:7]
TIMEOUT_SECONDS = int(os.getenv("PRODUCTION_SMOKE_TIMEOUT", "900"))


def get(url: str) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": "cs2-bets-production-smoke/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    if not EXPECTED_COMMIT:
        print("EXPECTED_COMMIT не задан", file=sys.stderr)
        return 2

    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error = "деплой ещё не появился"
    next_notice = time.monotonic()
    while time.monotonic() < deadline:
        try:
            status, raw = get(f"{BASE_URL}/health")
            health = json.loads(raw)
            running_commit = str(health.get("commit") or "")
            if status == 200 and running_commit == EXPECTED_COMMIT:
                if health.get("ok") is not True:
                    last_error = f"health ok={health.get('ok')!r}"
                elif health.get("db") != "ok":
                    last_error = f"БД не готова: {health.get('db')!r}"
                elif health.get("feed_stale") is not False:
                    last_error = f"лента устарела: feed_stale={health.get('feed_stale')!r}"
                else:
                    metrics_status, metrics_raw = get(f"{BASE_URL}/metrics")
                    metrics_text = metrics_raw.decode("utf-8", errors="replace")
                    if metrics_status == 200 and "app_up 1" in metrics_text:
                        print(
                            f"OK: production commit {running_commit}; health ok, db ok, "
                            "feed fresh, /metrics отвечает"
                        )
                        return 0
                    last_error = f"/metrics status={metrics_status}, ответ={metrics_text[:200]!r}"
            else:
                last_error = (
                    f"/health status={status}, commit={running_commit!r}, ожидается {EXPECTED_COMMIT!r}"
                )
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"HTTP-проверка не прошла: {type(exc).__name__}: {exc}"

        if time.monotonic() >= next_notice:
            print(f"Ожидание production smoke: {last_error}", flush=True)
            next_notice = time.monotonic() + 60
        time.sleep(10)

    print(f"FAIL: production smoke не прошёл за {TIMEOUT_SECONDS} сек.: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
